#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ponte entre o orquestrador e o teu run_backtest.py.

    python orq_runner.py --params p.json --start 2024-01-01 --end 2024-06-30 --out m.json

O teu run_backtest.py NAO e alterado. Este ficheiro:

  1. importa-o como modulo (nao como __main__, por isso o bot do Telegram
     nao arranca e nada se liga a cTrader);
  2. cala o Telegram e desliga tudo o que escreve em disco entre ensaios;
  3. le os candles do cache data/bars_<SIMBOLO>_M1.pkl e corta a janela pedida;
  4. aplica os parametros do ensaio nas variaveis globais do teu script;
  5. chama run_backtest() em SINCRONO e espera pelo fim;
  6. escreve o JSON de metricas que o gate do orquestrador sabe ler.

Porque e que o passo 2 e o mais importante
------------------------------------------
O teu script aprende enquanto corre: ONLINE_LEARNING_ENABLED alimenta o
context_memory.json, e o LSTM/CatBoost retreinam-se ao fim do run. Isso e
otimo quando es tu a carregar no /run. E veneno para uma bateria de ensaios
automaticos, por duas razoes:

  - o ensaio nº2 ja nao corre contra o mesmo modelo que o nº1, logo a
    diferenca entre eles deixa de ser a hipotese que se queria testar;
  - a memoria acumula trades das janelas seguintes, e ao chegar ao holdout
    o modelo ja "viu" o futuro.

Um Sharpe assim nao esta so inflacionado — nao significa nada, e nem o
Deflated Sharpe o apanha, porque o DSR corrige o numero de tentativas e
nao a contaminacao entre elas. Por isso este ficheiro forca a aprendizagem
a OFF e recusa-se a correr se alguem a voltar a ligar por parametro.

Modos extra
-----------
    python orq_runner.py --listar-params    catalogo de parametros afinaveis
    python orq_runner.py --verificar        confirma que da para correr offline
    python orq_runner.py --diagnostico      onde e que os sinais morrem
    python orq_runner.py --contexto         leitura do mercado agora, em JSON

O `--contexto` nao corre backtest nenhum: le candles (do cTrader pela TUA
ligacao, se o CONTEXTO_AO_VIVO estiver ligado, senao do cache) e devolve
maximas, minimas, ATR, percentis e a regua de pontos ate ao teu take e stop.
E o que alimenta o /contexto do orquestrador. Nao escreve nada no teu projeto.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import math
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent

# Capital de referencia. So mexe na escala do drawdown e do retorno total —
# o Sharpe e invariante. Mantem-no fixo entre ensaios, senao deixas de poder
# comparar dois resultados.
CAPITAL_BASE = 10_000.0

# Abaixo disto o Sharpe da janela e ruido com casas decimais.
MINIMO_CANDLES = 50

# Aprendizagem e escrita em disco: tudo desligado. Ver o cabecalho.
FORCAR_DESLIGADO = {
    "ONLINE_LEARNING_ENABLED": False,
    "LSTM_TRAIN_AFTER_RUN": False,
    "POLICY_TRAIN_AFTER_RUN": False,
    "AI_BACKTEST_AUTO_LEARN": False,
    "FAST_BACKTEST": True,          # sem export CSV/JSON por mes
}

# Estes dois so mexem no que o Telegram recebe, nao no que e medido, por isso
# sao os unicos que MENSAGENS_TELEGRAM pode reabrir.
FORCAR_NOTIFICACOES = ("BT_DETAILED_NOTIFICATIONS",
                       "BT_MONTHLY_SUMMARY_NOTIFICATIONS")

# O que o TEU bot te manda enquanto o orquestrador corre os ensaios.
#
#   "resumo"   uma mensagem por ensaio: janela, parametros e resultado. Prova
#              de vida sem te encher o chat.
#   "todas"    tudo o que o teu run_backtest() diria num /run a mao. Sao ~19
#              mensagens por ensaio, mais a lista de trades aos blocos de 30 —
#              vezes dois (treino e validacao), vezes o numero de ensaios.
#   "nenhuma"  silencio total (era o que estava, e foi um erro meu: tirou-te a
#              unica janela que tinhas para o que se passava).
MENSAGENS_TELEGRAM = "resumo"
LIMITE_MENSAGEM_TG = 3500          # o Telegram corta acima de ~4096

# Se um destes aparecer nos parametros do ensaio, paramos. Nao e um aviso.
PROIBIDOS = {
    "ONLINE_LEARNING_ENABLED",
    "LSTM_TRAIN_AFTER_RUN",
    "POLICY_TRAIN_AFTER_RUN",
    "AI_BACKTEST_AUTO_LEARN",
}


class ErroRunner(Exception):
    """Falha que o utilizador consegue arranjar."""


# ---------------------------------------------------------------------------
#  Importar o script do utilizador sem o acordar
# ---------------------------------------------------------------------------
def carregar_alvo(caminho: Path):
    """Importa run_backtest.py como modulo normal.

    O bot do Telegram vive todo dentro de `if __name__ == "__main__":`, e o
    nome que damos aqui nao e "__main__". Nada arranca, nada se liga.
    """
    if not caminho.exists():
        raise ErroRunner(
            f"nao encontrei {caminho}.\n"
            f"Poe o orq_runner.py na MESMA pasta do teu run_backtest.py, "
            f"ou usa --alvo C:\\caminho\\para\\run_backtest.py"
        )

    pasta = str(caminho.parent)
    if pasta not in sys.path:
        sys.path.insert(0, pasta)

    spec = importlib.util.spec_from_file_location("alvo_backtest", caminho)
    if spec is None or spec.loader is None:
        raise ErroRunner(f"nao consegui preparar o import de {caminho}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alvo_backtest"] = mod
    try:
        # O import imprime coisas (chcp, avisos). Nao queremos isso no stdout
        # que o orquestrador le.
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
    except ImportError as e:
        raise ErroRunner(
            f"o teu run_backtest.py precisa de uma biblioteca que falta: {e}\n"
            f"Corre-o a mao uma vez para veres o que instalar."
        ) from e
    except Exception as e:
        raise ErroRunner(f"o teu run_backtest.py rebentou ao ser importado: "
                         f"{type(e).__name__}: {e}") from e
    return mod


def calar(mod, modo: str = MENSAGENS_TELEGRAM):
    """Regula o que o teu bot diz, e devolve por onde se fala com ele.

    Calar tudo foi o que estava aqui, e foi um erro: durante um estudo o teu
    codigo corre dezenas de vezes e nao dizia nada, por isso parecia que nem
    estava a correr. A ausencia de mensagens era a prova de que ESTAVA — e nao
    ha nada mais parecido com um sistema partido do que um sistema mudo.

    Devolve a funcao ORIGINAL do teu bot, para o runner poder mandar o resumo
    no fim pelo teu proprio canal, com o teu token e o teu chat.
    """
    original = getattr(mod, "tg_send", None)
    if modo != "todas":
        mod.tg_send = lambda *a, **k: None
        if hasattr(mod, "tg_chunked"):
            mod.tg_chunked = lambda *a, **k: None
    # last_signals.json chega a centenas de MB e e reescrito a cada run.
    # Isto nao e Telegram, e escrita em disco: fica desligado sempre.
    if hasattr(mod, "_save_last_ai_signals"):
        mod._save_last_ai_signals = lambda *a, **k: None
    if hasattr(mod, "_state"):
        mod._state["offline_mode"] = True
    return original if callable(original) else None


def avisar(canal, texto: str, modo: str = MENSAGENS_TELEGRAM) -> None:
    """Manda uma mensagem pelo bot DO UTILIZADOR. Nunca rebenta o ensaio.

    Um ensaio que morresse por falha de rede ao mandar um aviso seria o
    proprio aviso a estragar aquilo que devia estar so a relatar.
    """
    if modo == "nenhuma" or canal is None:
        return
    try:
        canal(texto[:LIMITE_MENSAGEM_TG])
    except Exception as e:
        print(f"[tg] nao consegui avisar: {e}", file=sys.stderr)


def semear(mod, semente: int) -> None:
    """Mesmos parametros -> mesmo numero. Sem isto o gate compara ruido."""
    try:
        mod.random.seed(semente)
    except Exception:
        pass
    try:
        mod.np.random.seed(semente)
    except Exception:
        pass
    try:
        import torch
        torch.manual_seed(semente)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Parametros
# ---------------------------------------------------------------------------
def afinaveis(mod) -> dict:
    """Globais em MAIUSCULAS com valor simples: o catalogo de parametros."""
    out = {}
    for nome, valor in vars(mod).items():
        if not nome.isupper() or nome.startswith("_"):
            continue
        if isinstance(valor, bool) or isinstance(valor, (int, float, str)):
            out[nome] = valor
    return dict(sorted(out.items()))


def aplicar_params(mod, params: dict) -> dict:
    """Escreve os parametros nas globais do teu script.

    Um nome que nao existe e ERRO, nao um encolher de ombros. Se deixassemos
    passar um `EMA_FASTT` mal escrito, o ensaio corria com os valores de
    origem e entrava no estudo como se fosse uma hipotese nova: gastava uma
    tentativa do orcamento e ainda por cima ficava a mentir sobre o que
    testou.
    """
    catalogo = afinaveis(mod)
    aplicados = {}
    for nome, valor in (params or {}).items():
        if nome in PROIBIDOS:
            raise ErroRunner(
                f"o parametro {nome} nao pode ser mexido por um ensaio.\n"
                f"E o que liga a aprendizagem entre ensaios — com ela ligada, "
                f"cada ensaio corre contra um modelo diferente do anterior e "
                f"os resultados deixam de ser comparaveis."
            )
        if nome not in catalogo:
            perto = [k for k in catalogo if nome.upper() in k or k in nome.upper()][:5]
            dica = f" Talvez: {', '.join(perto)}." if perto else ""
            raise ErroRunner(
                f"o parametro {nome} nao existe no teu run_backtest.py.{dica}\n"
                f"Ve a lista toda com: python orq_runner.py --listar-params"
            )
        antigo = catalogo[nome]
        try:
            if isinstance(antigo, bool):
                novo = bool(valor)
            elif isinstance(antigo, int):
                # O JSON nao distingue 12 de 12.0, e o agente escreve as duas
                # coisas. Um periodo de EMA em float rebenta la dentro, num
                # range() ou num indice, longe daqui e sem dizer porque.
                f = float(valor)
                if f != int(f):
                    raise ErroRunner(
                        f"{nome} e um numero inteiro no teu script "
                        f"(esta a {antigo}), e {valor!r} tem casas decimais."
                    )
                novo = int(f)
            elif isinstance(antigo, float):
                novo = float(valor)
            else:
                novo = str(valor)
        except ErroRunner:
            raise
        except (TypeError, ValueError) as e:
            raise ErroRunner(f"{nome}={valor!r} nao serve: {e}") from e
        setattr(mod, nome, novo)
        aplicados[nome] = novo

    for nome, valor in FORCAR_DESLIGADO.items():
        if hasattr(mod, nome):
            setattr(mod, nome, valor)
    # As notificacoes so decidem o que sai para o chat; nao mudam um numero
    # que seja. Por isso seguem o MENSAGENS_TELEGRAM em vez de estarem sempre
    # desligadas como o resto.
    for nome in FORCAR_NOTIFICACOES:
        if hasattr(mod, nome):
            setattr(mod, nome, MENSAGENS_TELEGRAM == "todas")
    return aplicados


# ---------------------------------------------------------------------------
#  Candles
# ---------------------------------------------------------------------------
def para_minutos(data: str) -> int:
    """YYYY-MM-DD -> minutos desde a epoch (UTC), como o teu cache guarda."""
    try:
        d = datetime.strptime(data.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ErroRunner(f"data invalida {data!r}, esperava YYYY-MM-DD: {e}") from e
    return int(d.timestamp()) // 60


def carregar_barras(mod, simbolo: str, inicio: str, fim: str):
    """Le o cache e corta a janela. Devolve (barras, digits)."""
    barras, digits = mod._load_bars_cache(simbolo)
    if not barras:
        caminho = mod._bars_cache_path(simbolo)
        raise ErroRunner(
            f"nao ha candles em cache para {simbolo}.\n"
            f"Procurei em: {caminho}\n"
            f"Arranca o teu bot uma vez e faz /run (ou /importcsv) para "
            f"encher o cache. Depois disso este runner nunca mais precisa de rede."
        )
    if digits is None:
        digits = int(mod._state.get("symbol_digits") or 2)

    m0, m1 = para_minutos(inicio), para_minutos(fim) + 1440   # fim inclusive
    if m0 >= m1:
        raise ErroRunner(f"janela vazia: {inicio} nao e antes de {fim}")

    recorte = [b for b in barras if m0 <= mod._bar_ts_min(b) < m1]
    if len(recorte) < MINIMO_CANDLES:
        t0 = mod._bar_ts_min(barras[0]) * 60
        t1 = mod._bar_ts_min(barras[-1]) * 60
        cobertura = (f"O cache cobre {datetime.utcfromtimestamp(t0):%Y-%m-%d} a "
                     f"{datetime.utcfromtimestamp(t1):%Y-%m-%d} "
                     f"({len(barras)} candles).")
        if not recorte:
            # Fora do cache e uma coisa; dentro mas curta e outra. Dizer a
            # mesma frase nos dois casos mandava-te procurar o problema errado.
            raise ErroRunner(
                f"a janela {inicio}..{fim} nao tem um unico candle no cache.\n"
                f"{cobertura}\nAjusta as janelas do orquestrador para dentro "
                f"deste intervalo."
            )
        raise ErroRunner(
            f"a janela {inicio}..{fim} esta no cache mas so tem {len(recorte)} "
            f"candles, e o minimo sao {MINIMO_CANDLES}.\n{cobertura}\n"
            f"Com tao poucos candles o Sharpe seria ruido com casas decimais. "
            f"Alarga a janela."
        )
    return recorte, int(digits)


# ---------------------------------------------------------------------------
#  Metricas
# ---------------------------------------------------------------------------
def dias_do_mercado(mod, barras) -> list[str]:
    """Dias UTC em que houve candles. E o calendario real do instrumento:
    cripto da 365 dias/ano, um indice da ~261. Assim nao ha que adivinhar."""
    dias = {datetime.utcfromtimestamp(mod._bar_ts_min(b) * 60).strftime("%Y-%m-%d")
            for b in barras}
    return sorted(dias)


def serie_de_retornos(trades, por_ponto: float, dias: list[str], capital: float):
    """Retornos diarios a partir dos trades fechados.

    Compoe sobre o capital corrente, para que a curva de equity e a serie de
    retornos digam exatamente a mesma coisa — se fossem calculadas por vias
    diferentes, o drawdown e o Sharpe podiam contradizer-se.
    """
    por_dia: dict[str, float] = {}
    fechados = 0
    for t in trades or []:
        ts = int(t.get("close_ts") or 0)
        if ts <= 0:
            continue          # trade ainda aberto no fim da janela
        fechados += 1
        dia = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        por_dia[dia] = por_dia.get(dia, 0.0) + float(t.get("pnl") or 0.0) * por_ponto

    equity = capital
    rets, curva, ruina = [], [equity], False
    for dia in dias:
        lucro = por_dia.get(dia, 0.0)
        if equity <= 0:
            ruina = True
            rets.append(0.0)
            curva.append(equity)
            continue
        rets.append(lucro / equity)
        equity += lucro
        if equity <= 0:
            ruina = True
            equity = 0.0
        curva.append(equity)
    return rets, curva, fechados, ruina


def outros_simbolos(mod, atual: str) -> list[tuple[str, int, str, str]]:
    """Que mercados ja tens em disco, alem do que estas a usar.

    Vale a pena porque a estrategia certa para um mercado pode nao ser a certa
    para outro, e descobrir isso a olhar para a pasta e mais rapido do que
    descobri-lo a gastar ensaios.
    """
    achados = []
    try:
        pasta = Path(mod._bars_cache_dir())
    except Exception:
        return achados
    for f in sorted(pasta.glob("bars_*_M1.pkl")):
        nome = f.name[len("bars_"):-len("_M1.pkl")]
        if nome.upper() == str(atual).upper():
            continue
        try:
            barras, _ = mod._load_bars_cache(nome)
        except Exception:
            continue
        if not barras:
            continue
        t0 = mod._bar_ts_min(barras[0]) * 60
        t1 = mod._bar_ts_min(barras[-1]) * 60
        achados.append((nome, len(barras),
                        f"{datetime.utcfromtimestamp(t0):%Y-%m-%d}",
                        f"{datetime.utcfromtimestamp(t1):%Y-%m-%d}"))
    return achados


def orcamento_de_tempo(segundos: float, dias: int, teto: int = 1800) -> str:
    """Quantos dias de janela cabem no timeout, medidos e nao adivinhados.

    "Da timeout" nao diz o que fazer. "A tua janela e 13x maior do que cabe"
    diz. E o custo nao e linear apenas no numero de barras: enquanto um filtro
    bloqueia tudo nao ha trade nenhum para simular, e o backtest parece rapido.
    Assim que ele abre, cada trade passa a ser percorrido barra a barra ate
    fechar — e o tempo salta de repente, sem nada no codigo ter mudado.
    """
    por_dia = segundos / max(1, dias)
    cabem = int(teto * 0.8 / por_dia) if por_dia > 0 else 99999
    linhas = [f"⏱  {segundos:.0f}s para {dias} dias  ({por_dia:.2f}s/dia)",
              f"   No timeout de {teto}s cabem ~{cabem} dias por janela "
              f"(~{cabem / 365:.1f} anos), com folga."]
    if cabem < 365:
        linhas.append("   ⚠ Isto e pouco. Cada ensaio corre DUAS janelas "
                      "(treino e validacao),")
        linhas.append("     por isso o custo real de um ensaio e o dobro. "
                      "Ou encurtas as")
        linhas.append("     janelas, ou sobes TIMEOUT_BACKTEST, ou corres em "
                      "barras mais largas.")
    return "\n".join(linhas)


def porque_zero(res: dict, barras, mod, inicio: str, fim: str) -> str:
    """Um backtest sem trades nao e uma medicao. Diz porque.

    Zero trades chegava ao gate como Sharpe 0.00, que parece um resultado e
    nao e: e o motor a nao correr. Pior, esse 0.00 entrava na variancia dos
    ensaios anteriores, que e o que alimenta o Deflated Sharpe — um punhado
    de corridas vazias estragava a conta de todas as seguintes.

    O teu script distingue tres casos e eu sei le-los:
      - `no_entry_diagnostics` presente: avaliou sinais e bloqueou-os todos,
        e diz qual filtro os matou;
      - ausente e sem trades: nem chegou a avaliar, faltaram candles validos;
      - trades abertos mas nenhum fechado: a janela acabou a meio.
    """
    t0 = mod._bar_ts_min(barras[0]) * 60
    t1 = mod._bar_ts_min(barras[-1]) * 60
    onde = (f"Janela pedida: {inicio} a {fim}\n"
            f"Candles que la estavam: {len(barras)}, de "
            f"{datetime.utcfromtimestamp(t0):%Y-%m-%d} a "
            f"{datetime.utcfromtimestamp(t1):%Y-%m-%d}")

    funil = {**(res.get("context_blocks") or {}),
             **(res.get("no_entry_diagnostics") or {})}
    if funil:
        total = sum(int(v or 0) for v in funil.values())
        itens = sorted(funil.items(), key=lambda kv: -int(kv[1] or 0))

        # O funil e SEQUENCIAL, e por isso ordenar por contagem engana: os
        # numeros gigantes sao os primeiros filtros, que rejeitam quase tudo
        # POR DESENHO (uma estrategia de sessao ignora 3/4 do dia de proposito).
        # Mandar afrouxar o maior era mandar desligar a propria estrategia.
        #
        # Quem tem contagem pequena esta no FIM do funil: sao as barras que
        # sobreviveram a tudo o resto e morreram a um passo da entrada. E ai
        # que esta o que se pode arranjar.
        corte = max(1, total // 100)          # 1% das barras
        estrutural = [(r, n) for r, n in itens if n > corte]
        terminais = [(r, n) for r, n in itens if n <= corte]

        partes = [f"o motor avaliou os sinais e bloqueou-os todos "
                  f"({total} barras no total).\n\n{onde}\n"]
        if estrutural:
            partes.append("\nPor desenho (a estrategia so opera em parte do dia):")
            partes += [f"\n  {n:>9}  {r}" for r, n in estrutural[:6]]
        if terminais:
            sobreviventes = sum(n for _, n in terminais)
            partes.append(
                f"\n\n>>> {sobreviventes} sinais chegaram ao fim do funil, e "
                f"morreram aqui:")
            partes += [f"\n  {n:>9}  {r}" for r, n in terminais[:6]]
            partes.append(
                f"\n\nE em `{terminais[0][0]}` que tens de mexer. Se ele rejeita "
                f"100% dos sinais que la chegam, nenhum valor de nenhum parametro "
                f"o vai desbloquear — confirma se o limiar dele e compativel com "
                f"a escala deste mercado.")
        else:
            partes.append("\n\nNenhum sinal chegou sequer ao fim do funil: o que "
                          "esta acima e por desenho, e a estrategia nunca gerou "
                          "uma entrada nesta janela.")
        return "".join(partes)

    if res.get("trades"):
        return (f"houve {len(res['trades'])} trades, mas nenhum fechou dentro da "
                f"janela.\n\n{onde}\n\nAlarga a janela, ou encurta o alvo.")

    return (
        f"o motor nem chegou a avaliar sinais.\n\n{onde}\n\n"
        f"O teu run_backtest.py desiste em silencio quando fica com menos de 50 "
        f"candles VALIDOS — e ele descarta candles com preco de fecho <= 0.1, por "
        f"isso o numero dele pode ser bem menor do que os {len(barras)} que eu lhe "
        f"dei.\n\nO mais provavel e as janelas do orquestrador (TREINO, VALIDACAO, "
        f"HOLDOUT) nao baterem certo com o que ha em cache. Confirma o que tens:\n"
        f"    python orq_runner.py --verificar"
    )


def drawdown_maximo(curva: list[float]) -> float:
    pico, pior = curva[0] if curva else 0.0, 0.0
    for v in curva:
        pico = max(pico, v)
        if pico > 0:
            pior = max(pior, (pico - v) / pico)
    return pior


def periodos_por_ano(dias: list[str]) -> int:
    """Quantos dias de mercado cabem num ano, medido nos proprios dados."""
    if len(dias) < 2:
        return 365
    d0 = datetime.strptime(dias[0], "%Y-%m-%d")
    d1 = datetime.strptime(dias[-1], "%Y-%m-%d")
    vao = (d1 - d0).days + 1
    if vao <= 0:
        return 365
    return max(1, round(len(dias) * 365.25 / vao))


# ===========================================================================
#  CONTEXTO DE MERCADO
#
#  O modelo nao le candles: le a saida disto. Tudo o que sai daqui e
#  aritmetica sobre OHLC, sem uma unica opiniao — porque do outro lado ha um
#  guarda que rejeita qualquer numero que o modelo escreva e que nao tenha
#  vindo destas contas. Se ele vai precisar de um numero, e aqui que nasce.
# ===========================================================================

# Ligar isto abre uma SEGUNDA sessao no teu broker, com as tuas credenciais,
# possivelmente enquanto o teu bot tem a primeira aberta. Ha brokers que
# aceitam, ha quem recuse a segunda, e ha quem derrube a que ja la estava. Ser
# desligado a meio de uma posicao aberta e caro de mais por uma leitura de
# contexto — por isso isto comeca DESLIGADO e es tu que o ligas, de
# preferencia uma primeira vez com o bot parado, para saberes o que a tua
# conta faz.
CONTEXTO_AO_VIVO = False

# Uma ligacao pendurada devolve o cache com a idade a frente. Nunca fica a
# espera: um /contexto que nao responde e pior que um que responde velho.
TIMEOUT_DESCARGA = 30

# Quantas horas de M1 pedir ao broker. Chega para toda a tabela; o percentil
# de 90 dias vem sempre do cache, que e onde esta a historia.
HORAS_AO_VIVO = 48

# Se souberes o nome da funcao que descarrega candles no teu ficheiro, poe-o
# aqui e eu deixo de adivinhar.
FUNCAO_DESCARGA = ""

# Wilder. A definicao vai escrita ao lado do numero na saida, para ninguem
# comparar este ATR com um de outra definicao sem dar por isso.
ATR_PERIODOS = 14

# Abaixo disto nao se diz uma taxa. Dizer "acontece em 8% dos casos" com
# quinze casos e inventar precisao — e e exatamente assim que uma leitura
# ganha uma confianca que nao merece.
MINIMO_AMOSTRA = 200

# Minimos por tipo de amostra. Um decil diario com 30 dias diz alguma coisa;
# uma serie de velas com 30 posicoes nao diz nada.
MINIMO_DIAS = 30
MINIMO_SERIE = 200

# Faixa da Asia, em horas UTC, quando o teu ficheiro nao tiver constantes
# proprias. A saida diz sempre de onde vieram as horas que usou.
ASIA_OMISSAO = (0, 7)

# Quantos dias de historia para os percentis.
DIAS_HISTORIA = 90

# Nomes com cara de "vai buscar candles" e que NAO escrevem nada.
CANDIDATOS_DESCARGA = (
    "fetch_bars", "_fetch_bars", "fetch_trendbars", "_fetch_trendbars",
    "get_bars", "_get_bars", "get_trendbars", "_get_trendbars",
    "download_bars", "_download_bars", "request_trendbars", "_request_trendbars",
    "fetch_candles", "_fetch_candles", "get_candles", "_get_candles",
    "fetch_m1", "_fetch_m1", "fetch_history", "_fetch_history",
    "get_history", "_get_history", "baixar_barras", "_baixar_barras",
    "descarregar_barras", "_descarregar_barras",
)

# Uma funcao chamada _update_bars_cache bem pode ser a que descarrega — mas
# escolher sozinho uma funcao que mexe no teu cache seria eu a decidir por ti.
# Se for essa, poe-lhe o nome no FUNCAO_DESCARGA e eu uso-a.
_ESCRITA = ("update", "sync", "save", "write", "refresh", "ensure",
            "atualiz", "grav")
_SOBRE_CANDLES = ("bar", "trendbar", "candle", "vela", "ohlc", "m1", "histor")
_DESCARGA = ("fetch", "get", "download", "request", "load", "pull",
             "baixar", "descarreg")

# Funcoes que convertem um candle em (open, high, low, close).
CANDIDATOS_OHLC = ("_bar_ohlc", "bar_ohlc", "_ohlc_de_bar", "_bar_to_ohlc",
                   "_decode_bar", "_bar_values", "_bar_prices", "_ohlc")


# ---------------------------------------------------------------------------
#  De onde vem um candle
# ---------------------------------------------------------------------------
def candidatos_descarga(mod) -> list[str]:
    """Funcoes do teu modulo com cara de irem buscar candles.

    So serve para a mensagem de erro: quando nao encontro a certa, e melhor
    dizer-te o que la esta do que mandar-te procurar.
    """
    achados = []
    for nome, valor in vars(mod).items():
        if not callable(valor):
            continue
        baixo = nome.lower()
        if not any(p in baixo for p in _SOBRE_CANDLES):
            continue
        if any(p in baixo for p in _DESCARGA) or any(p in baixo for p in _ESCRITA):
            achados.append(nome)
    return sorted(achados)


def achar_descarga(mod):
    """Descobre a funcao do TEU ficheiro que vai buscar candles ao broker.

    Devolve (nome, funcao). Nao adivinho e sigo em frente: um candle mal lido
    produz numeros que PARECEM uma leitura, e isso e pior do que um erro.
    """
    if FUNCAO_DESCARGA:
        f = getattr(mod, FUNCAO_DESCARGA, None)
        if not callable(f):
            raise ErroRunner(
                f"FUNCAO_DESCARGA = {FUNCAO_DESCARGA!r}, mas isso nao e uma funcao "
                f"do teu ficheiro.\nCandidatas que la encontrei: "
                f"{', '.join(candidatos_descarga(mod)) or '(nenhuma)'}")
        return FUNCAO_DESCARGA, f

    for nome in CANDIDATOS_DESCARGA:
        f = getattr(mod, nome, None)
        if callable(f):
            return nome, f

    houve = candidatos_descarga(mod)
    escrevem = [n for n in houve if any(p in n.lower() for p in _ESCRITA)]
    dica = ""
    if escrevem:
        dica = ("\nEstas tem cara de descarregar, mas o nome diz que tambem "
                f"ESCREVEM, e eu nao escolho sozinho nenhuma que mexa no teu\n"
                f"cache: {', '.join(escrevem)}\n"
                "Se for uma delas, poe-lhe o nome no FUNCAO_DESCARGA aqui em cima.")
    raise ErroRunner(
        "nao encontrei no teu ficheiro nenhuma funcao que va buscar candles ao "
        f"broker.\nProcurei por: {', '.join(CANDIDATOS_DESCARGA[:8])}, ...\n"
        f"Funcoes com cara disso que la estao: {', '.join(houve) or '(nenhuma)'}"
        f"{dica}")


def _chamar_descarga(f, simbolo: str, horas: int):
    """Chama a funcao com os argumentos que ela aceitar, e mais nenhum."""
    nome = getattr(f, "__name__", "descarga")
    try:
        params = dict(inspect.signature(f).parameters)
    except (TypeError, ValueError):
        params = {}

    kwargs = {}
    for n, p in params.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        b = n.lower()
        if b in ("symbol", "simbolo", "symbol_name", "sym", "instrumento", "ativo"):
            kwargs[n] = simbolo
        elif b in ("hours", "horas"):
            kwargs[n] = horas
        elif b in ("days", "dias", "bt_days"):
            kwargs[n] = max(1, math.ceil(horas / 24))
        elif b in ("count", "n", "limit", "quantos", "n_bars", "num_bars", "bars"):
            kwargs[n] = horas * 60
        elif b in ("tf", "timeframe", "period", "periodo", "periodicidade"):
            kwargs[n] = "M1"

    em_falta = [n for n, p in params.items()
                if n not in kwargs
                and p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if em_falta:
        raise ErroRunner(
            f"encontrei {nome}({', '.join(params)}) mas nao sei o que lhe passar: "
            f"faltam {', '.join(em_falta)}.\nSe esta nao e a funcao certa, poe a "
            f"certa no FUNCAO_DESCARGA.")
    return f(**kwargs)


def _barras_de(saida):
    """Aceita o que a funcao devolver: lista, (lista, digits), ou dict."""
    if isinstance(saida, tuple) and saida:
        saida = saida[0]
    if isinstance(saida, dict):
        saida = saida.get("bars") or saida.get("barras") or saida.get("trendbars") or []
    try:
        return list(saida or [])
    except TypeError:
        return []


def descarregar(mod, simbolo: str, horas: int = HORAS_AO_VIVO,
                timeout: int = TIMEOUT_DESCARGA) -> list:
    """Pede candles ao broker, pela ligacao do TEU ficheiro.

    Numa thread daemon, de proposito: uma ligacao pendurada nao pode prender o
    processo. Prefiro dados do cache com a idade a frente do que um /contexto
    que nunca responde.
    """
    nome, f = achar_descarga(mod)
    caixa: dict = {}

    def _ir():
        try:
            caixa["r"] = _chamar_descarga(f, simbolo, horas)
        except BaseException as e:           # noqa: BLE001 — vai para o cache
            caixa["e"] = e

    t = threading.Thread(target=_ir, daemon=True, name="orq-descarga")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ErroRunner(f"{nome}() passou de {timeout}s sem responder")
    if "e" in caixa:
        e = caixa["e"]
        raise ErroRunner(f"{nome}() rebentou: {type(e).__name__}: {e}")
    barras = _barras_de(caixa.get("r"))
    if not barras:
        raise ErroRunner(f"{nome}() nao devolveu candle nenhum")
    return barras


# ---------------------------------------------------------------------------
#  Como se le um candle
# ---------------------------------------------------------------------------
def _escala_ctrader(b0: dict) -> float:
    """Um ponto da API do cTrader vale 1e-5.

    Mas ha ficheiros que ja guardam o candle convertido, e ai dividir outra vez
    daria um ETHUSD a 0,03. Decide-se pela ordem de grandeza: um preco real
    guardado em pontos e sempre um numero grande.
    """
    baixo = abs(float(b0.get("low") or 0))
    return 1e-5 if baixo > 1e5 else 1.0


def achar_ohlc(mod, barras):
    """Como se le um candle. Devolve (funcao, descricao).

    Tres formas, por esta ordem: uma funcao do teu proprio ficheiro; a forma
    da API do cTrader (low + deltas inteiros em pontos); OHLC direto. Se nao
    for nenhuma, o erro diz que chaves encontrou — nao "formato invalido".
    """
    if not barras:
        raise ErroRunner("nao ha candles para descobrir o formato")
    b0 = barras[0]

    for nome in CANDIDATOS_OHLC:
        f = getattr(mod, nome, None)
        if not callable(f):
            continue
        try:
            v = f(b0)
        except Exception:
            continue
        if (isinstance(v, (tuple, list)) and len(v) == 4
                and all(isinstance(x, (int, float)) for x in v)):
            return ((lambda b, _f=f: tuple(float(x) for x in _f(b))),
                    f"{nome}() do teu ficheiro")

    if isinstance(b0, dict) and "low" in b0 and "deltaClose" in b0:
        escala = _escala_ctrader(b0)

        def _ct(b, _e=escala):
            baixo = float(b.get("low") or 0)
            return ((baixo + float(b.get("deltaOpen") or 0)) * _e,
                    (baixo + float(b.get("deltaHigh") or 0)) * _e,
                    baixo * _e,
                    (baixo + float(b.get("deltaClose") or 0)) * _e)

        return _ct, f"low+deltas do cTrader (escala {escala:g})"

    if isinstance(b0, dict) and all(k in b0 for k in ("open", "high", "low", "close")):
        return ((lambda b: (float(b["open"]), float(b["high"]),
                            float(b["low"]), float(b["close"]))),
                "open/high/low/close direto")

    chaves = (", ".join(sorted(str(k) for k in b0)) if isinstance(b0, dict)
              else f"nao e um dicionario, e um {type(b0).__name__}")
    raise ErroRunner(
        "nao reconheco o formato do candle.\n"
        f"O primeiro candle tem: {chaves}\n"
        "Esperava uma destas tres: uma funcao no teu ficheiro que converta um "
        "candle em (open, high, low, close); as chaves low + deltaOpen/"
        "deltaHigh/deltaClose da API do cTrader; ou open/high/low/close.\n"
        "Diz-me qual e e resolve-se numa linha.")


# ---------------------------------------------------------------------------
#  Estatistica basica sobre velas  (ts_minutos, open, high, low, close)
# ---------------------------------------------------------------------------
def agrupar(barras, ohlc, ts, minutos: int) -> list[tuple]:
    """Junta M1 em velas de `minutos`, alinhadas ao relogio UTC.

    Alinhadas ao relogio, e nao ao ultimo candle, porque "a maxima das ultimas
    4 horas" e a maxima de quatro horas de relogio para toda a gente que olha
    para o mesmo grafico que tu.
    """
    out: list[list] = []
    atual: list | None = None
    for b in barras:
        t = ts(b)
        balde = (t // minutos) * minutos
        o, h, l, c = ohlc(b)
        if atual is None or atual[0] != balde:
            if atual is not None:
                out.append(atual)
            atual = [balde, o, h, l, c]
        else:
            atual[2] = max(atual[2], h)
            atual[3] = min(atual[3], l)
            atual[4] = c
    if atual is not None:
        out.append(atual)
    return [tuple(v) for v in out]


def atr(velas, periodos: int = ATR_PERIODOS) -> float | None:
    """ATR de Wilder sobre as velas dadas. None se nao houver historia."""
    if len(velas) < periodos + 1:
        return None
    trs = []
    for i in range(1, len(velas)):
        _, _, alto, baixo, _c = velas[i]
        fecho_anterior = velas[i - 1][4]
        trs.append(max(alto - baixo,
                       abs(alto - fecho_anterior),
                       abs(baixo - fecho_anterior)))
    if len(trs) < periodos:
        return None
    valor = sum(trs[:periodos]) / periodos
    for tr in trs[periodos:]:
        valor = (valor * (periodos - 1) + tr) / periodos
    return valor


def extremos(velas):
    """(maxima, minima) das velas dadas, ou (None, None) se nao houver."""
    if not velas:
        return None, None
    return max(v[2] for v in velas), min(v[3] for v in velas)


def percentil(valor: float, amostra, minimo: int = MINIMO_AMOSTRA):
    """Que percentagem da amostra fica abaixo deste valor, e o n.

    Devolve o n SEMPRE, mesmo quando se recusa a dar a percentagem — porque a
    unica coisa pior do que nao ter amostra e ter e nao saber que e pouca. O
    minimo e explicito em cada chamada: 30 dias chegam para falar de decis
    diarios, 200 velas nao chegam para nada que se diga em percentagem.
    """
    dados = [float(x) for x in amostra if x is not None]
    if len(dados) < max(1, int(minimo)):
        return None, len(dados)
    return 100.0 * sum(1 for x in dados if x < valor) / len(dados), len(dados)


def maior_movimento(velas, limiar: float) -> float:
    """A maior perna continua: o maior movimento sem uma correcao de `limiar`.

    NAO e maxima menos minima, e a diferenca importa. Uma sessao que sobe 40,
    corrige 35 e sobe outra vez 38 tem amplitude 43 e nenhuma perna maior que
    40: a amplitude diz onde o preco ANDOU, a perna diz ate onde ele FOI de uma
    vez. Quem opera tendencia quer a segunda; a primeira e compativel com um
    dia inteiro a andar as voltas.

    (Reparo de um erro meu: max(subida, descida) medido a partir do minimo e do
    maximo correntes e identico a amplitude, sempre — nao mede perna nenhuma.
    E preciso um limiar de reversao, e o limiar tem de ser dito.)
    """
    if not velas:
        return 0.0
    if not limiar or limiar <= 0:
        alta, baixa = extremos(velas)
        return (alta - baixa) if alta is not None else 0.0

    inicio = extremo = velas[0][4]
    direcao = 0
    maior = 0.0
    for _t, _o, alto, baixo, _c in velas:
        if direcao == 0:
            if alto - inicio >= limiar:
                direcao, extremo = 1, alto
            elif inicio - baixo >= limiar:
                direcao, extremo = -1, baixo
            elif abs(alto - inicio) > abs(extremo - inicio):
                extremo = alto
            elif abs(baixo - inicio) > abs(extremo - inicio):
                extremo = baixo
        elif direcao == 1:
            if alto > extremo:
                extremo = alto
            elif extremo - baixo >= limiar:
                maior = max(maior, extremo - inicio)
                inicio, extremo, direcao = extremo, baixo, -1
        else:
            if baixo < extremo:
                extremo = baixo
            elif alto - extremo >= limiar:
                maior = max(maior, inicio - extremo)
                inicio, extremo, direcao = extremo, alto, 1
    return max(maior, abs(extremo - inicio))


def seguidas(velas) -> int:
    """Quantas velas seguidas fecharam do mesmo lado, contando do fim.

    Positivo a subir, negativo a descer. Uma vela doji (fecho igual a abertura)
    corta a serie, que e o que ela faz mesmo.
    """
    if not velas:
        return 0
    direcao = 0
    n = 0
    for _t, abre, _h, _l, fecha in reversed(velas):
        d = 1 if fecha > abre else (-1 if fecha < abre else 0)
        if d == 0 or (direcao and d != direcao):
            break
        direcao = d
        n += 1
    return n * (direcao or 0)


def frequencia_de_serie(velas, comprimento: int) -> tuple[float | None, int]:
    """Com que frequencia aparecem `comprimento` velas seguidas do mesmo lado.

    Conta posicoes, nao series: a pergunta que interessa a quem esta a olhar
    para o grafico e "estando eu aqui, quantas vezes isto ja tinha acontecido".
    """
    k = abs(int(comprimento))
    if k < 2 or len(velas) <= k:
        return None, 0
    sinais = [1 if v[4] > v[1] else (-1 if v[4] < v[1] else 0) for v in velas]
    posicoes = len(sinais) - k + 1
    if posicoes < MINIMO_SERIE:
        return None, max(0, posicoes)
    casos = 0
    for i in range(posicoes):
        janela = sinais[i:i + k]
        if janela[0] != 0 and all(s == janela[0] for s in janela):
            casos += 1
    return 100.0 * casos / posicoes, posicoes


def reagrupar(velas, minutos: int) -> list[tuple]:
    """Junta velas ja formadas em velas maiores, alinhadas ao relogio UTC."""
    out: list[tuple] = []
    atual: list | None = None
    for t, o, h, l, c in velas:
        balde = (t // minutos) * minutos
        if atual is None or atual[0] != balde:
            if atual is not None:
                out.append(tuple(atual))
            atual = [balde, o, h, l, c]
        else:
            atual[2] = max(atual[2], h)
            atual[3] = min(atual[3], l)
            atual[4] = c
    if atual is not None:
        out.append(tuple(atual))
    return out


# ---------------------------------------------------------------------------
#  Sessoes e horizontes
# ---------------------------------------------------------------------------
def horas_da_asia(mod) -> tuple[int, int, str]:
    """As horas da faixa da Asia, das constantes do TEU ficheiro se existirem.

    O setup e a varrida de Londres sobre a faixa da Asia; "ultimas 4h" nao e o
    horizonte que decide nada. Se as horas nao vierem do teu ficheiro, a saida
    diz que foram por omissao — para nunca haver duvida sobre o que se mediu.
    """
    for a_nome, b_nome in (("ASIA_INICIO", "ASIA_FIM"),
                           ("SESSAO_ASIA_INICIO", "SESSAO_ASIA_FIM"),
                           ("ASIA_START_H", "ASIA_END_H"),
                           ("ASIA_H_INI", "ASIA_H_FIM"),
                           ("ASIA_HORA_INICIO", "ASIA_HORA_FIM")):
        a, b = getattr(mod, a_nome, None), getattr(mod, b_nome, None)
        if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool):
            return a % 24, b % 24, f"{a_nome}/{b_nome} do teu ficheiro"
    return ASIA_OMISSAO[0], ASIA_OMISSAO[1], "por omissao (nao achei constantes tuas)"


def janela_asia(agora_min: int, ini_h: int, fim_h: int) -> tuple[int, int]:
    """A faixa da Asia mais recente que ja comecou, em minutos desde a epoca."""
    dia = (agora_min // 1440) * 1440
    if ini_h < fim_h:
        a, b = dia + ini_h * 60, dia + fim_h * 60
    else:                                   # atravessa a meia-noite
        a, b = dia - 1440 + ini_h * 60, dia + fim_h * 60
    if agora_min < a:
        a, b = a - 1440, b - 1440
    return a, b


def _curto(nome: str) -> str:
    """Nome do horizonte na regua. Cabe numa linha de telemovel."""
    return nome[5:] if nome.startswith("ult. ") else nome


def _entre(velas, t0: int, t1: int) -> list[tuple]:
    return [v for v in velas if t0 <= v[0] < t1]


def _horizonte(nome: str, velas, preco: float, atr_ref, atr_tf: str) -> dict:
    alta, baixa = extremos(velas)
    if alta is None:
        return {"nome": nome, "velas": 0, "maxima": None, "minima": None,
                "amplitude_pts": None, "amplitude_atr": None,
                "posicao_pct": None, "atr_ref": atr_ref, "atr_tf": atr_tf}
    amplitude = alta - baixa
    return {
        "nome": nome,
        "velas": len(velas),
        "maxima": alta,
        "minima": baixa,
        "amplitude_pts": amplitude,
        "amplitude_atr": (amplitude / atr_ref) if atr_ref else None,
        "posicao_pct": (100.0 * (preco - baixa) / amplitude) if amplitude > 0 else None,
        "atr_ref": atr_ref,
        "atr_tf": atr_tf,
    }


def _rompeu(depois, alta, baixa) -> tuple[str, bool]:
    """A faixa da Asia foi varrida depois de fechar? E o preco voltou la para dentro?

    E a perna de manipulacao do AMD, medida em vez de vista: o preco sair da
    faixa e voltar e uma coisa; sair e ficar la fora e outra completamente
    diferente, e o grafico mostra as duas da mesma maneira ao fim de uma hora.
    """
    if not depois or alta is None:
        return "nao", False
    acima = any(v[2] > alta for v in depois)
    abaixo = any(v[3] < baixa for v in depois)
    onde = ("ambos" if acima and abaixo else
            "acima" if acima else "abaixo" if abaixo else "nao")
    fecho = depois[-1][4]
    return onde, (onde != "nao" and baixa <= fecho <= alta)


# ---------------------------------------------------------------------------
#  A regua de pontos
# ---------------------------------------------------------------------------
def spread_do_modulo(mod, simbolo: str):
    """O spread que o TEU ficheiro usa, para o acerto de equilibrio ser o real."""
    especs = getattr(mod, "MARKET_SPECS", None)
    if isinstance(especs, dict):
        e = especs.get(simbolo) or especs.get(str(simbolo).upper())
        if isinstance(e, dict) and isinstance(e.get("spread"), (int, float)):
            return float(e["spread"]), f"MARKET_SPECS[{simbolo!r}]['spread']"
    v = getattr(mod, "MAX_SPREAD_PIPS", None)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v), "MAX_SPREAD_PIPS"
    return None, ""


def regua(niveis, preco: float, atr_ref, stop_pts, take_pts,
          lado: str, faixas, spread=None) -> dict:
    """Os niveis e o teu take/stop na MESMA escada, ordenados por preco.

    A regua responde a pergunta que a tabela nao responde: quantos pontos ha
    daqui ate cada sitio que interessa, e onde e que o teu alvo e o teu risco
    caem no meio deles.
    """
    sinal = -1 if str(lado).lower().startswith("v") else 1
    fora = {
        "lado": "venda" if sinal < 0 else "compra",
        "preco": preco,
        "atr_ref": atr_ref,
        "stop_pts": stop_pts,
        "take_pts": take_pts,
        "stop_preco": None,
        "take_preco": None,
        "R": None,
        "equilibrio_pct": None,
        "spread_pts": spread,
        "equilibrio_com_spread_pct": None,
        "degraus": [],
        "take_alem_de": None,
        "stop_dentro_de": [],
    }

    escada = [{"etiqueta": nome, "preco": float(p), "marca": None}
              for nome, p in niveis if p is not None]

    if stop_pts and take_pts:
        fora["take_preco"] = preco + sinal * float(take_pts)
        fora["stop_preco"] = preco - sinal * float(stop_pts)
        fora["R"] = float(take_pts) / float(stop_pts)
        fora["equilibrio_pct"] = 100.0 * float(stop_pts) / (float(stop_pts) + float(take_pts))
        if spread:
            fora["equilibrio_com_spread_pct"] = (
                100.0 * (float(stop_pts) + float(spread))
                / (float(stop_pts) + float(take_pts)))
        escada.append({"etiqueta": "TAKE", "preco": fora["take_preco"], "marca": "TAKE"})
        escada.append({"etiqueta": "STOP", "preco": fora["stop_preco"], "marca": "STOP"})

    for d in escada:
        pontos = d["preco"] - preco
        d["pontos"] = pontos
        d["atr"] = (abs(pontos) / atr_ref) if atr_ref else None
        d["R"] = (sinal * pontos / float(stop_pts)) if stop_pts else None
    fora["degraus"] = sorted(escada, key=lambda d: -d["preco"])

    if stop_pts and take_pts:
        # O take fica para la de algum nivel? Entao o preco tem de o romper
        # antes de te pagar — e isso decide-se aqui, nao no olho.
        a_frente = [(sinal * (d["preco"] - preco), d["etiqueta"])
                    for d in escada if d["marca"] is None]
        atras_do_take = [(d, nome) for d, nome in a_frente if 0 < d < float(take_pts)]
        if atras_do_take:
            dist, nome = max(atras_do_take)
            fora["take_alem_de"] = {"nivel": nome, "pontos": float(take_pts) - dist}

        # O stop cai dentro de alguma faixa? Um stop no meio da faixa da Asia
        # esta na zona que a manipulacao varre — que e o que o AMD preve.
        for nome, alta, baixa in faixas:
            if alta is None or baixa is None:
                continue
            if baixa <= fora["stop_preco"] <= alta:
                fora["stop_dentro_de"].append(
                    {"faixa": nome, "maxima": alta, "minima": baixa})

    return fora


# ---------------------------------------------------------------------------
#  Montar o contexto
# ---------------------------------------------------------------------------
def _juntar(velhas, novas, ts) -> list:
    """Cache mais frescos, sem duplicados, ordenados. O fresco ganha.

    Os frescos dao o "agora"; o cache da os 90 dias de que os percentis
    precisam. Nenhum dos dois chega sozinho.
    """
    por_ts = {ts(b): b for b in (velhas or [])}
    por_ts.update({ts(b): b for b in (novas or [])})
    return [por_ts[k] for k in sorted(por_ts)]


def candles_para_contexto(mod, simbolo: str, ao_vivo: bool,
                          horas: int = HORAS_AO_VIVO,
                          timeout: int = TIMEOUT_DESCARGA):
    """Devolve (barras, fonte, motivo).

    Uma falha de rede NUNCA mata o comando: cai para o cache e traz a razao,
    que vai escrita no cabecalho da leitura. Ler dados velhos sabendo que sao
    velhos e util; ler dados velhos a pensar que sao de agora e o unico
    resultado verdadeiramente mau aqui.
    """
    ts = getattr(mod, "_bar_ts_min", None) or (lambda b: int(b.get("utcTimestampInMinutes") or 0))
    do_cache, _digits = mod._load_bars_cache(simbolo)

    if not ao_vivo:
        if not do_cache:
            raise ErroRunner(
                f"nao ha candles em cache para {simbolo}, e o CONTEXTO_AO_VIVO "
                f"esta desligado.\nProcurei em: {mod._bars_cache_path(simbolo)}\n"
                f"Enche o cache com /run no teu bot, ou liga o CONTEXTO_AO_VIVO.")
        return do_cache, "cache", "CONTEXTO_AO_VIVO desligado"

    try:
        frescos = descarregar(mod, simbolo, horas, timeout)
    except ErroRunner as e:
        if not do_cache:
            raise
        return do_cache, "cache", str(e)
    return _juntar(do_cache, frescos, ts), "cTrader", ""


def contexto(mod, simbolo: str, *, ao_vivo: bool | None = None,
             stop=None, take=None, lado: str = "compra",
             horas: int = HORAS_AO_VIVO, timeout: int = TIMEOUT_DESCARGA) -> dict:
    """Todos os numeros que a leitura precisa, e nem um julgamento.

    O que sai daqui e o universo de numeros que o modelo pode usar do outro
    lado. Se ele escrever um que nao esteja aqui, e rejeitado — por isso tudo
    o que ele possa precisar tem de nascer nesta funcao.
    """
    vivo = CONTEXTO_AO_VIVO if ao_vivo is None else bool(ao_vivo)
    ts = getattr(mod, "_bar_ts_min", None) or (lambda b: int(b.get("utcTimestampInMinutes") or 0))

    barras, fonte, motivo = candles_para_contexto(mod, simbolo, vivo, horas, timeout)
    ohlc, formato = achar_ohlc(mod, barras)
    m1 = agrupar(barras, ohlc, ts, 1)
    if not m1:
        raise ErroRunner("nao consegui montar uma unica vela a partir dos candles")

    agora_min = m1[-1][0]
    preco = m1[-1][4]
    idade = max(0, int(time.time()) // 60 - agora_min)

    v15 = reagrupar(m1, 15)
    v1h = reagrupar(m1, 60)
    v4h = reagrupar(m1, 240)
    vd = reagrupar(m1, 1440)

    a15, a1h, a4h, ad = (atr(v15), atr(v1h), atr(v4h), atr(vd))

    ini_h, fim_h, origem_horas = horas_da_asia(mod)
    t_asia0, t_asia1 = janela_asia(agora_min, ini_h, fim_h)
    velas_asia = _entre(m1, t_asia0, t_asia1)
    alta_asia, baixa_asia = extremos(velas_asia)
    rompeu, voltou = _rompeu(_entre(m1, t_asia1, agora_min + 1), alta_asia, baixa_asia)

    dia0 = (agora_min // 1440) * 1440
    horizontes = [
        _horizonte("ult. 15m", _entre(m1, agora_min - 14, agora_min + 1), preco, a15, "15m"),
        _horizonte("ult. 1h", _entre(m1, agora_min - 59, agora_min + 1), preco, a1h, "1h"),
        _horizonte("ult. 4h", _entre(m1, agora_min - 239, agora_min + 1), preco, a4h, "4h"),
        _horizonte("Asia", velas_asia, preco, a1h, "1h"),
        _horizonte("hoje", _entre(m1, dia0, agora_min + 1), preco, ad, "D1"),
        _horizonte("ontem", _entre(m1, dia0 - 1440, dia0), preco, ad, "D1"),
    ]
    por_nome = {h["nome"]: h for h in horizontes}

    # Fechos: o que o modelo recebe como "as ultimas horas".
    fechos_1h = []
    for i in range(max(1, len(v1h) - 6), len(v1h)):
        anterior = v1h[i - 1][4]
        if anterior:
            fechos_1h.append(100.0 * (v1h[i][4] - anterior) / anterior)
    fechos_15m = []
    for i in range(max(1, len(v15) - 8), len(v15)):
        anterior = v15[i - 1][4]
        if anterior:
            fechos_15m.append(100.0 * (v15[i][4] - anterior) / anterior)

    # "Subiu forte" so quer dizer alguma coisa contra o que e normal.
    mov_2h = mov_2h_pct = mov_2h_atr = normal_2h_atr = None
    if len(v1h) >= 3:
        antes = v1h[-3][4]
        mov_2h = preco - antes
        mov_2h_pct = (100.0 * mov_2h / antes) if antes else None
        mov_2h_atr = (abs(mov_2h) / a1h) if a1h else None
        saltos = [abs(v1h[i][4] - v1h[i - 2][4]) for i in range(2, len(v1h))]
        if saltos and a1h:
            normal_2h_atr = statistics.median(saltos) / a1h

    serie = seguidas(v1h)
    freq_serie, n_serie = frequencia_de_serie(v1h, serie) if abs(serie) >= 2 else (None, 0)

    amplitudes_diarias = [v[2] - v[3] for v in vd[:-1]][-DIAS_HISTORIA:]
    amp_hoje = por_nome["hoje"]["amplitude_pts"]
    pct_amp, n_amp = ((None, 0) if amp_hoje is None
                      else percentil(amp_hoje, amplitudes_diarias, MINIMO_DIAS))

    mov24 = maior_movimento(_entre(m1, agora_min - 1439, agora_min + 1), a1h or 0.0)

    spread, origem_spread = spread_do_modulo(mod, simbolo)
    niveis = [(f"max {_curto(h['nome'])}", h["maxima"])
              for h in horizontes if h["maxima"] is not None]
    niveis += [(f"min {_curto(h['nome'])}", h["minima"])
               for h in horizontes if h["minima"] is not None]
    faixas = [(h["nome"], h["maxima"], h["minima"]) for h in horizontes]

    dados = {
        "simbolo": simbolo,
        "preco": preco,
        "agora_utc": datetime.utcfromtimestamp(agora_min * 60).strftime("%Y-%m-%d %H:%M"),
        "ts_min": agora_min,
        "fonte": fonte,
        "motivo_da_fonte": motivo,
        "idade_min": idade,
        "formato_candle": formato,
        "candles": len(m1),
        "atr": {"15m": a15, "1h": a1h, "4h": a4h, "D1": ad,
                "definicao": f"Wilder, {ATR_PERIODOS} periodos"},
        "asia": {"inicio_h": ini_h, "fim_h": fim_h, "origem_horas": origem_horas,
                 "maxima": alta_asia, "minima": baixa_asia,
                 "rompeu": rompeu, "voltou_para_dentro": voltou},
        "horizontes": horizontes,
        "fechos_1h_pct": fechos_1h,
        "fechos_15m_pct": fechos_15m,
        "movimento_2h": {"pontos": mov_2h, "pct": mov_2h_pct, "atr": mov_2h_atr,
                         "normal_atr": normal_2h_atr},
        "velas_seguidas_1h": serie,
        "frequencia_da_serie_pct": freq_serie,
        "frequencia_da_serie_n": n_serie,
        "amplitude_hoje_percentil": pct_amp,
        "amplitude_hoje_n": n_amp,
        "maior_movimento_24h": {"pontos": mov24,
                                "atr": (mov24 / a1h) if a1h else None,
                                "limiar_pts": a1h},
        # Fechos horarios recentes. Nao sao para o modelo ler: sao para o
        # placar poder ir ver, mais tarde, o que o preco fez mesmo depois de
        # uma leitura — sem isto uma hipotese nunca chega a ser corrigida.
        "historico_1h": [[int(v[0]), float(v[4])] for v in v1h[-48:]],
        "spread_pts": spread,
        "origem_spread": origem_spread,
    }
    dados["regua"] = regua(niveis, preco, a1h, stop, take, lado, faixas, spread)
    dados["notavel"] = notavel(dados)
    return dados


def notavel(d: dict) -> list[str]:
    """O que salta a vista — por percentil, nao por opiniao.

    Cada frase traz o n de onde saiu. Uma taxa sem amostra e a maneira mais
    simples de dar a uma leitura uma confianca que ela nao ganhou.
    """
    fora = []
    pct, n = d.get("amplitude_hoje_percentil"), d.get("amplitude_hoje_n") or 0
    if pct is not None and pct >= 90:
        fora.append(f"amplitude de hoje no decil superior dos ultimos {n} dias")
    elif pct is not None and pct <= 10:
        fora.append(f"amplitude de hoje no decil inferior dos ultimos {n} dias")

    serie = d.get("velas_seguidas_1h") or 0
    freq, n_serie = d.get("frequencia_da_serie_pct"), d.get("frequencia_da_serie_n") or 0
    if abs(serie) >= 3:
        rumo = "a subir" if serie > 0 else "a descer"
        if freq is not None:
            fora.append(f"{abs(serie)} velas de 1h seguidas {rumo} "
                        f"({freq:.0f}% dos casos, n={n_serie})")
        else:
            fora.append(f"{abs(serie)} velas de 1h seguidas {rumo} "
                        f"(sem amostra para dizer se e raro: n={n_serie})")

    mov = d.get("movimento_2h") or {}
    if mov.get("atr") is not None and mov.get("normal_atr"):
        if mov["atr"] >= 2 * mov["normal_atr"]:
            fora.append(f"movimento de 2h a {mov['atr']:.1f} ATR, contra "
                        f"{mov['normal_atr']:.1f} ATR do costume")

    asia = d.get("asia") or {}
    if asia.get("rompeu") not in (None, "nao"):
        if asia.get("voltou_para_dentro"):
            fora.append(f"a faixa da Asia foi rompida {asia['rompeu']} e o preco "
                        f"voltou para dentro — varrida")
        else:
            fora.append(f"a faixa da Asia foi rompida {asia['rompeu']} e o preco "
                        f"ficou la fora")

    for h in d.get("horizontes") or []:
        if h["nome"] == "hoje" and h.get("posicao_pct") is not None:
            if h["posicao_pct"] >= 95:
                fora.append("preco no topo do dia")
            elif h["posicao_pct"] <= 5:
                fora.append("preco no fundo do dia")
    return fora


# ---------------------------------------------------------------------------
#  Correr
# ---------------------------------------------------------------------------
def correr(mod, barras, digits: int, params: dict, etiqueta: str, registo: Path | None):
    """Chama run_backtest() em sincrono, com o barulho todo desviado."""
    cfg = mod._snapshot_runtime_config()
    for chave, valor in (("run_uid", etiqueta), ("run_id", 0),
                         ("run_mode", "orq"), ("run_started_utc",
                          datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))):
        cfg[chave] = valor
    # Cinto e suspensorios: o snapshot le as globais, mas se uma versao futura
    # do teu script passar a ler o cfg diretamente, isto continua a valer.
    cfg["online_learning_enabled"] = False
    cfg["lstm_train_after_run"] = False
    cfg["policy_train_after_run"] = False
    cfg["fast_backtest"] = True
    tagarela = MENSAGENS_TELEGRAM == "todas"
    cfg["bt_detailed_notifications"] = tagarela
    cfg["bt_monthly_summary_notifications"] = tagarela

    if registo:
        with registo.open("w", encoding="utf-8", errors="replace") as f:
            with contextlib.redirect_stdout(f):
                res = mod.run_backtest(barras, digits, label=etiqueta, cfg=cfg)
    else:
        with contextlib.redirect_stdout(io.StringIO()):
            res = mod.run_backtest(barras, digits, label=etiqueta, cfg=cfg)

    if not isinstance(res, dict):
        raise ErroRunner(f"run_backtest devolveu {type(res).__name__}, esperava dict")
    return res, cfg


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Corre o teu run_backtest.py sem Telegram e escreve as metricas.")
    ap.add_argument("--params", help="JSON com os parametros do ensaio")
    ap.add_argument("--start", help="inicio da janela (YYYY-MM-DD)")
    ap.add_argument("--end", help="fim da janela (YYYY-MM-DD)")
    ap.add_argument("--out", help="onde gravar o JSON de metricas")
    ap.add_argument("--alvo", default=str(AQUI / "run_backtest.py"),
                    help="caminho do teu run_backtest.py")
    ap.add_argument("--simbolo", default=None, help="por omissao, o SYMBOL_NAME do teu script")
    ap.add_argument("--capital", type=float, default=CAPITAL_BASE)
    ap.add_argument("--semente", type=int, default=12345)
    ap.add_argument("--registo", default=None,
                    help="ficheiro onde despejar o relatorio completo do backtest")
    ap.add_argument("--listar-params", action="store_true",
                    help="mostra os parametros afinaveis e sai")
    ap.add_argument("--verificar", action="store_true",
                    help="confirma que da para correr offline e sai")
    ap.add_argument("--diagnostico", type=int, metavar="DIAS", nargs="?", const=90,
                    help="corre uma janela curta (por omissao 90 dias) so para "
                         "ver o funil de bloqueios, e sai")
    ap.add_argument("--contexto", action="store_true",
                    help="escreve o JSON com a leitura do mercado agora, e sai")
    ap.add_argument("--stop", type=float, default=None,
                    help="stop em pontos, para a regua")
    ap.add_argument("--take", type=float, default=None,
                    help="take em pontos, para a regua")
    ap.add_argument("--lado", default="compra", choices=("compra", "venda"))
    ap.add_argument("--ao-vivo", dest="ao_vivo", action="store_true", default=None,
                    help="pede candles ao broker (ve o aviso no CONTEXTO_AO_VIVO)")
    ap.add_argument("--do-cache", dest="ao_vivo", action="store_false",
                    help="forca a leitura do cache, sem tocar na rede")
    a = ap.parse_args()

    # Definidos antes do try: se a falha for logo no arranque, o tratamento de
    # erro nao pode ser ele proprio a rebentar com um NameError.
    canal, simbolo, aplicados = None, a.simbolo or "?", {}

    try:
        mod = carregar_alvo(Path(a.alvo).resolve())
        canal = calar(mod)
        simbolo = a.simbolo or str(getattr(mod, "SYMBOL_NAME", "ETHUSD"))

        if a.listar_params:
            print(json.dumps(afinaveis(mod), indent=2, ensure_ascii=False))
            return 0

        if a.verificar:
            barras, digits = mod._load_bars_cache(simbolo)
            print(f"alvo      : {a.alvo}")
            print(f"simbolo   : {simbolo}")
            print(f"estrategia: {getattr(mod, 'ESTRATEGIA', '?')}")
            print(f"cache     : {mod._bars_cache_path(simbolo)}")
            outros = outros_simbolos(mod, simbolo)
            if outros:
                print("\noutros mercados que ja tens em cache:")
                for nome, n, ini, fim in outros:
                    print(f"  {nome:<12} {n:>9} candles  {ini} a {fim}")
                print()
            if not barras:
                print("candles   : NENHUM — enche o cache com /run ou /importcsv")
                return 1
            t0 = mod._bar_ts_min(barras[0]) * 60
            t1 = mod._bar_ts_min(barras[-1]) * 60
            d0 = datetime.utcfromtimestamp(t0)
            d1 = datetime.utcfromtimestamp(t1)
            print(f"candles   : {len(barras)} ({d0:%Y-%m-%d} a {d1:%Y-%m-%d}), "
                  f"digits={digits}")
            print(f"params    : {len(afinaveis(mod))} afinaveis")

            # As duas incognitas do /contexto, ditas AQUI em vez de em cima da
            # hora: e melhor descobrir que nao reconheco o teu candle antes de
            # precisares da leitura do que a meio dela.
            try:
                _, formato_candle = achar_ohlc(mod, barras)
            except ErroRunner as exc:
                formato_candle = f"NAO RECONHECIDO — {exc}"
            print(f"candle    : {formato_candle}")
            try:
                nome_descarga, _ = achar_descarga(mod)
                estado_vivo = ("ligada" if CONTEXTO_AO_VIVO
                               else "disponivel (CONTEXTO_AO_VIVO esta desligado)")
                print(f"descarga  : {nome_descarga}() — {estado_vivo}")
            except ErroRunner as exc:
                print("descarga  : NAO ENCONTRADA — o /contexto vai ler o cache")
                print("            " + str(exc).replace("\n", "\n            "))
            _ini_h, _fim_h, _origem_asia = horas_da_asia(mod)
            print(f"faixa Asia: {_ini_h:02d}:00-{_fim_h:02d}:00 UTC, {_origem_asia}")

            # Sugerir as janelas em vez de deixar adivinhar: janelas que nao
            # batem certo com o cache dao zero trades, e zero trades parece um
            # resultado mau quando e so o motor a nao correr.
            vao = (d1 - d0).days
            if vao < 90:
                print(f"\n⚠️  So ha {vao} dias em cache. Da para um teste, nao da "
                      f"para treino/validacao/holdout separados.")
            else:
                from datetime import timedelta
                c1 = d0 + timedelta(days=int(vao * 0.5))
                c2 = d0 + timedelta(days=int(vao * 0.75))
                print("\nPoe isto no orquestrador.py (metade treino, "
                      "um quarto validacao, um quarto holdout):")
                print(f'    TREINO    = ("{d0:%Y-%m-%d}", "{c1:%Y-%m-%d}")')
                print(f'    VALIDACAO = ("{c1 + timedelta(days=1):%Y-%m-%d}", '
                      f'"{c2:%Y-%m-%d}")')
                print(f'    HOLDOUT   = ("{c2 + timedelta(days=1):%Y-%m-%d}", '
                      f'"{d1:%Y-%m-%d}")')
            print("\nDa para correr offline.")
            return 0

        if a.diagnostico:
            # Uma janela curta no FIM do cache: chega para ver onde os sinais
            # morrem, e custa um minuto em vez dos trinta que a janela inteira
            # gasta. Descobrir um filtro impossivel nao devia exigir um estudo.
            from datetime import timedelta
            barras_todas, _ = mod._load_bars_cache(simbolo)
            if not barras_todas:
                raise ErroRunner(f"nao ha candles em cache para {simbolo}.")
            fim = datetime.utcfromtimestamp(mod._bar_ts_min(barras_todas[-1]) * 60)
            ini = fim - timedelta(days=int(a.diagnostico))
            print(f"Diagnostico: {simbolo}, {ini:%Y-%m-%d} a {fim:%Y-%m-%d} "
                  f"({a.diagnostico} dias)\n")
            semear(mod, a.semente)
            barras, digits = carregar_barras(mod, simbolo,
                                             f"{ini:%Y-%m-%d}", f"{fim:%Y-%m-%d}")
            t_ini = time.time()
            res, cfg = correr(mod, barras, digits, {}, "diagnostico", None)
            demorou = time.time() - t_ini
            fechados = sum(1 for t in (res.get("trades") or [])
                           if int(t.get("close_ts") or 0) > 0)
            print(orcamento_de_tempo(demorou, int(a.diagnostico)))
            if fechados:
                print(f"\n✅ {fechados} trades fecharam nesta janela. O motor opera.")
                funil = {**(res.get("context_blocks") or {}),
                         **(res.get("no_entry_diagnostics") or {})}
                if funil:
                    print("\nFunil de bloqueios:")
                    for r, n in sorted(funil.items(), key=lambda kv: -int(kv[1] or 0))[:10]:
                        print(f"  {int(n or 0):>9}  {r}")
                return 0
            print(porque_zero(res, barras, mod, f"{ini:%Y-%m-%d}", f"{fim:%Y-%m-%d}"))
            return 1

        if a.contexto:
            dados = contexto(mod, simbolo, ao_vivo=a.ao_vivo, stop=a.stop,
                             take=a.take, lado=a.lado)
            texto = json.dumps(dados, indent=2, ensure_ascii=False, default=str)
            if a.out:
                Path(a.out).write_text(texto, encoding="utf-8")
                print(f"contexto escrito em {a.out} "
                      f"({dados['fonte']}, candle de ha {dados['idade_min']} min)")
            else:
                print(texto)
            return 0

        em_falta = [n for n in ("params", "start", "end", "out") if not getattr(a, n)]
        if em_falta:
            raise ErroRunner("faltam argumentos: " + ", ".join("--" + n for n in em_falta))

        try:
            params = json.loads(Path(a.params).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ErroRunner(f"nao consegui ler {a.params}: {e}") from e
        if not isinstance(params, dict):
            raise ErroRunner(f"{a.params} tem de ser um objeto JSON")

        semear(mod, a.semente)
        aplicados = aplicar_params(mod, params)
        barras, digits = carregar_barras(mod, simbolo, a.start, a.end)

        t_inicio = time.time()
        res, cfg = correr(mod, barras, digits, aplicados, f"orq {a.start}..{a.end}",
                          Path(a.registo) if a.registo else None)
        demorou = time.time() - t_inicio

        por_ponto = float(mod._point_usd_value(simbolo, cfg.get("lots")))
        dias = dias_do_mercado(mod, barras)
        rets, curva, fechados, ruina = serie_de_retornos(
            res.get("trades") or [], por_ponto, dias, a.capital)

        if fechados == 0:
            raise ErroRunner(porque_zero(res, barras, mod, a.start, a.end))

        saida = {
            "returns": rets,
            "trades": int(fechados),
            "max_drawdown": drawdown_maximo(curva),
            "total_return": (curva[-1] / curva[0] - 1.0) if curva and curva[0] else 0.0,
            "periods_per_year": periodos_por_ano(dias),
            "janela": [a.start, a.end],
            # informativo, o gate nao le nada disto
            "barras": len(barras),
            "dias": len(dias),
            "sinais_totais": int(res.get("total") or 0),
            "winrate": float(res.get("winrate") or 0.0),
            "profit_factor": float(res.get("profit_factor") or 0.0),
            "pnl_usd": float(res.get("pnl_usd") or 0.0),
            "pontos": float(res.get("total_pips") or 0.0),
            "drawdown_do_script": float(res.get("drawdown") or 0.0),
            # Onde os sinais morreram. Com trades a mais isto e curiosidade;
            # quando a contagem cai a pique e a primeira coisa que se quer ver.
            "bloqueios": {str(k): int(v or 0) for k, v in
                          (res.get("context_blocks") or {}).items()},
            "usd_por_ponto": por_ponto,
            "capital_base": a.capital,
            "ruina": ruina,
            "segundos": round(demorou, 1),
            "params": aplicados,
        }
        Path(a.out).write_text(json.dumps(saida), encoding="utf-8")

        sr = 0.0
        if len(rets) > 1:
            media = sum(rets) / len(rets)
            var = sum((r - media) ** 2 for r in rets) / (len(rets) - 1)
            if var > 0:
                sr = media / math.sqrt(var) * math.sqrt(saida["periods_per_year"])
        resumo = (f"{a.start} a {a.end} | {len(barras)} candles | {fechados} trades | "
                  f"Sharpe {sr:.2f} | drawdown {saida['max_drawdown'] * 100:.1f}% | "
                  f"PnL ${saida['pnl_usd']:.2f} | {demorou:.0f}s"
                  + ("  ⚠ RUINA" if ruina else ""))
        print(resumo)
        # Pelo TEU bot, com o teu token e o teu chat: e o teu codigo a dizer
        # que correu. Os parametros vao juntos porque uma linha de resultado
        # sem saber o que foi tentado nao serve para acompanhar nada.
        avisar(canal, f"🧪 {simbolo} — ensaio\n{resumo}\n"
                      + "\n".join(f"  {k} = {v}" for k, v in sorted(aplicados.items())))
        return 0

    except ErroRunner as e:
        print(f"erro: {e}", file=sys.stderr)
        # O caso em que MAIS se quer saber. Foi por nao chegar nada aqui que
        # dezenas de ensaios pareceram nao ter corrido de todo.
        avisar(canal, f"⚠️ {simbolo} — ensaio falhou\n{e}")
        return 2
    except KeyboardInterrupt:
        print("interrompido", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
