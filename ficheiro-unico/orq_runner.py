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
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import sys
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
    "BT_DETAILED_NOTIFICATIONS": False,
    "BT_MONTHLY_SUMMARY_NOTIFICATIONS": False,
    "FAST_BACKTEST": True,          # sem export CSV/JSON por mes
}

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


def calar(mod) -> None:
    """Telegram mudo e sem escritas de estado entre ensaios."""
    mod.tg_send = lambda *a, **k: None
    if hasattr(mod, "tg_chunked"):
        mod.tg_chunked = lambda *a, **k: None
    # last_signals.json chega a centenas de MB e e reescrito a cada run.
    if hasattr(mod, "_save_last_ai_signals"):
        mod._save_last_ai_signals = lambda *a, **k: None
    if hasattr(mod, "_state"):
        mod._state["offline_mode"] = True


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
    cfg["bt_detailed_notifications"] = False
    cfg["bt_monthly_summary_notifications"] = False

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
    a = ap.parse_args()

    try:
        mod = carregar_alvo(Path(a.alvo).resolve())
        calar(mod)
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
            res, cfg = correr(mod, barras, digits, {}, "diagnostico", None)
            fechados = sum(1 for t in (res.get("trades") or [])
                           if int(t.get("close_ts") or 0) > 0)
            if fechados:
                print(f"✅ {fechados} trades fecharam nesta janela. O motor opera.")
                funil = {**(res.get("context_blocks") or {}),
                         **(res.get("no_entry_diagnostics") or {})}
                if funil:
                    print("\nFunil de bloqueios:")
                    for r, n in sorted(funil.items(), key=lambda kv: -int(kv[1] or 0))[:10]:
                        print(f"  {int(n or 0):>9}  {r}")
                return 0
            print(porque_zero(res, barras, mod, f"{ini:%Y-%m-%d}", f"{fim:%Y-%m-%d}"))
            return 1

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
        print(f"{a.start} a {a.end} | {len(barras)} candles | {fechados} trades | "
              f"Sharpe {sr:.2f} | drawdown {saida['max_drawdown'] * 100:.1f}% | "
              f"PnL ${saida['pnl_usd']:.2f} | {demorou:.0f}s"
              + ("  ⚠ RUINA" if ruina else ""))
        return 0

    except ErroRunner as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrompido", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
