#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do orq_runner.py — a ponte para o run_backtest.py do utilizador.

    python3 -m pytest ficheiro-unico/test_orq_runner.py -q

O alvo real depende de cTrader, twisted e protobuf, por isso os testes correm
contra um DUPLO com a mesma superficie de API. O duplo rebenta de proposito se
o runner falhar em calar o Telegram ou em desligar a aprendizagem: assim as
garantias que interessam sao verificadas, e nao apenas afirmadas.
"""
from __future__ import annotations

import importlib.util
import json
import pickle
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
RUNNER = AQUI / "orq_runner.py"


def carregar_runner():
    spec = importlib.util.spec_from_file_location("orq_runner_sut", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orq_runner_sut"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = carregar_runner()


# ---------------------------------------------------------------------------
#  O duplo do run_backtest.py
# ---------------------------------------------------------------------------
DUPLO = '''
# -*- coding: utf-8 -*-
"""Duplo do run_backtest.py: mesma superficie, sem cTrader."""
import os, pickle

print("BANNER que nao pode escapar para o stdout do runner")

SYMBOL_NAME = "ETHUSD"
EMA_FAST = 9
FIXED_TP_POINTS = 100.0
FIXED_SL_POINTS = 50.0
LOTS = 0.01
ONLINE_LEARNING_ENABLED = True
LSTM_TRAIN_AFTER_RUN = True
POLICY_TRAIN_AFTER_RUN = True
AI_BACKTEST_AUTO_LEARN = True
BT_DETAILED_NOTIFICATIONS = True
BT_MONTHLY_SUMMARY_NOTIFICATIONS = True
FAST_BACKTEST = False
BOT_ARRANCOU = False

_state = {"symbol_digits": 2, "offline_mode": False}

DENTRO_DO_RUN = False

def tg_send(text):
    # Falar DENTRO do backtest e o que enche o chat; falar no fim e a prova
    # de vida. O duplo tem de saber distinguir as duas.
    if DENTRO_DO_RUN:
        raise AssertionError("tg_send foi chamado DENTRO do run_backtest")
    with open(os.path.join(_script_dir(), "tg.log"), "a", encoding="utf-8") as f:
        f.write(text + "\\n===\\n")

def tg_chunked(*a, **k):
    raise AssertionError("tg_chunked foi chamado")

def _save_last_ai_signals(rows):
    raise AssertionError("_save_last_ai_signals escreveu em disco")

def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))

def _bars_cache_path(symbol, tf="M1"):
    d = os.path.join(_script_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "bars_%s_%s.pkl" % (symbol, tf))

def _bar_ts_min(b):
    return int(b.get("utcTimestampInMinutes") or 0)

def _load_bars_cache(symbol, tf="M1"):
    p = _bars_cache_path(symbol, tf)
    if not os.path.exists(p):
        return [], None
    with open(p, "rb") as f:
        payload = pickle.load(f)
    return payload["bars"], payload.get("digits")

def _point_usd_value(symbol=None, lots=None):
    return 0.01 * float(LOTS if lots is None else lots) * 100.0

def _snapshot_runtime_config():
    return {"symbol_name": SYMBOL_NAME, "lots": float(LOTS),
            "ema_fast": int(EMA_FAST), "tp_points": float(FIXED_TP_POINTS),
            "online_learning_enabled": bool(ONLINE_LEARNING_ENABLED),
            "lstm_train_after_run": bool(LSTM_TRAIN_AFTER_RUN),
            "policy_train_after_run": bool(POLICY_TRAIN_AFTER_RUN),
            "fast_backtest": bool(FAST_BACKTEST),
            "bt_detailed_notifications": bool(BT_DETAILED_NOTIFICATIONS)}

def run_backtest(bars_raw, digits, label="", cfg=None):
    global DENTRO_DO_RUN
    DENTRO_DO_RUN = True
    try:
        return _run_backtest(bars_raw, digits, label, cfg)
    finally:
        DENTRO_DO_RUN = False

def _run_backtest(bars_raw, digits, label="", cfg=None):
    cfg = cfg or {}
    if cfg.get("online_learning_enabled"):
        raise AssertionError("aprendizagem ligada num ensaio")
    if cfg.get("lstm_train_after_run") or cfg.get("policy_train_after_run"):
        raise AssertionError("treino ligado num ensaio")
    if BT_DETAILED_NOTIFICATIONS or ONLINE_LEARNING_ENABLED:
        raise AssertionError("as globais nao foram forcadas a off")

    print("RELATORIO ENORME " * 200)

    trades = []
    passo = 1440 * max(1, int(EMA_FAST))
    for i in range(0, len(bars_raw), passo):
        ts = _bar_ts_min(bars_raw[i]) * 60
        pnl = FIXED_TP_POINTS if (i // passo) % 3 else -FIXED_SL_POINTS
        trades.append({"side": "BUY", "pnl": float(pnl),
                       "result": "TP" if pnl > 0 else "SL",
                       "open_ts": ts, "close_ts": ts + 3600})
    if bars_raw:                       # um trade que fica ABERTO no fim
        trades.append({"side": "SELL", "pnl": 999999.0, "result": "ABERTO",
                       "open_ts": _bar_ts_min(bars_raw[-1]) * 60, "close_ts": 0})
    fechados = [t for t in trades if t["close_ts"]]
    return {"label": label, "total": len(trades), "closed": len(fechados),
            "wins": 0, "losses": 0, "winrate": 50.0, "profit_factor": 1.5,
            "total_pips": sum(t["pnl"] for t in fechados),
            "pnl_usd": sum(t["pnl"] for t in fechados) * _point_usd_value(),
            "drawdown": 0.12, "trades": trades, "signals": []}

if __name__ == "__main__":
    BOT_ARRANCOU = True
    print("BOT pronto! Use /run")
    while True:
        pass
'''


def escrever_cache(pasta: Path, dias: int, primeiro: str = "2024-01-01",
                   passo_min: int = 1, simbolo: str = "ETHUSD",
                   so_dias_uteis: bool = False) -> None:
    inicio = int(datetime.strptime(primeiro, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp()) // 60
    barras = []
    for d in range(dias):
        if so_dias_uteis:
            data = datetime.utcfromtimestamp((inicio + d * 1440) * 60)
            if data.weekday() >= 5:
                continue
        for k in range(0, 1440, passo_min):
            barras.append({"utcTimestampInMinutes": inicio + d * 1440 + k,
                           "low": 200000, "deltaOpen": 10, "deltaHigh": 20,
                           "deltaClose": 15, "volume": 3})
    destino = pasta / "data"
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / f"bars_{simbolo}_M1.pkl").open("wb") as f:
        pickle.dump({"v": 2, "symbol": simbolo, "tf": "M1", "digits": 2,
                     "bars": barras}, f, protocol=4)


@pytest.fixture
def alvo(tmp_path: Path) -> Path:
    (tmp_path / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    escrever_cache(tmp_path, dias=200, passo_min=60)
    return tmp_path


def invocar(alvo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUNNER), "--alvo",
                           str(alvo / "run_backtest.py"), *args],
                          capture_output=True, text=True, cwd=str(alvo))


def ensaio(alvo: Path, params: dict, inicio: str, fim: str,
           saida: str = "m.json", *extra: str) -> subprocess.CompletedProcess:
    (alvo / "p.json").write_text(json.dumps(params), encoding="utf-8")
    return invocar(alvo, "--params", "p.json", "--start", inicio,
                   "--end", fim, "--out", saida, *extra)


# ---------------------------------------------------------------------------
#  O bot nao pode arrancar, e nao pode falar
# ---------------------------------------------------------------------------
def test_importar_o_alvo_nao_arranca_o_bot(alvo):
    mod = runner.carregar_alvo(alvo / "run_backtest.py")
    assert mod.BOT_ARRANCOU is False


def test_o_banner_do_alvo_nao_suja_o_stdout(alvo):
    r = ensaio(alvo, {}, "2024-02-01", "2024-03-31")
    assert r.returncode == 0, r.stderr
    assert "BANNER" not in r.stdout
    assert "RELATORIO ENORME" not in r.stdout


def test_o_relatorio_gigante_vai_para_o_registo(alvo):
    r = ensaio(alvo, {}, "2024-02-01", "2024-03-31", "m.json",
               "--registo", "rel.txt")
    assert r.returncode == 0, r.stderr
    assert "RELATORIO ENORME" in (alvo / "rel.txt").read_text(encoding="utf-8")


def test_telegram_calado_e_sem_escritas_de_estado(alvo):
    """O duplo levanta AssertionError se tg_send for chamado DENTRO do
    backtest, ou se _save_last_ai_signals escrever. Passar aqui e a prova
    de que nao aconteceu."""
    r = ensaio(alvo, {}, "2024-02-01", "2024-03-31")
    assert r.returncode == 0, r.stderr


def mensagens(pasta: Path) -> list[str]:
    f = pasta / "tg.log"
    if not f.exists():
        return []
    return [m for m in f.read_text(encoding="utf-8").split("===") if m.strip()]


def test_o_teu_bot_manda_uma_mensagem_por_ensaio(alvo):
    """Calar tudo tirou-lhe a unica janela para o que se passava, e dezenas
    de ensaios pareceram nao ter corrido. Uma linha por ensaio resolve isso
    sem devolver as 19 mensagens que enchiam o chat."""
    assert ensaio(alvo, {"EMA_FAST": 7}, "2024-02-01", "2024-03-31").returncode == 0
    msgs = mensagens(alvo)
    assert len(msgs) == 1
    assert "trades" in msgs[0] and "Sharpe" in msgs[0]
    assert "EMA_FAST = 7" in msgs[0]        # o que foi tentado, nao so o resultado


def test_um_ensaio_falhado_tambem_avisa(tmp_path):
    """E o caso em que mais se quer saber."""
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': [], "
                         "'no_entry_diagnostics': {'spread_excedido': 900}}")
    assert ensaio(p, {}, "2024-02-01", "2024-03-31").returncode != 0
    msgs = mensagens(p)
    assert len(msgs) == 1
    assert "falhou" in msgs[0] and "spread_excedido" in msgs[0]


def test_dois_ensaios_dao_duas_mensagens(alvo):
    assert ensaio(alvo, {"EMA_FAST": 5}, "2024-02-01", "2024-03-31", "a.json").returncode == 0
    assert ensaio(alvo, {"EMA_FAST": 9}, "2024-02-01", "2024-03-31", "b.json").returncode == 0
    assert len(mensagens(alvo)) == 2


def test_modo_todas_nao_amordaca_o_teu_bot(alvo):
    mod = runner.carregar_alvo(alvo / "run_backtest.py")
    antes = mod.tg_send
    assert runner.calar(mod, "todas") is antes
    assert mod.tg_send is antes             # intacta: fala como num /run a mao


def test_modo_nenhuma_e_resumo_amordacam_durante_o_ensaio(alvo):
    for modo in ("nenhuma", "resumo"):
        mod = runner.carregar_alvo(alvo / "run_backtest.py")
        antes = mod.tg_send
        assert runner.calar(mod, modo) is antes    # devolve o canal original
        assert mod.tg_send is not antes            # mas o backtest fica mudo


def test_o_disco_fica_desligado_em_qualquer_modo(alvo):
    """MENSAGENS_TELEGRAM regula o chat. Escrever estado entre ensaios e
    outra coisa, e essa fica desligada sempre."""
    for modo in ("nenhuma", "resumo", "todas"):
        mod = runner.carregar_alvo(alvo / "run_backtest.py")
        runner.calar(mod, modo)
        mod._save_last_ai_signals([{"x": 1}])      # nao levanta
        assert mod._state["offline_mode"] is True


def test_uma_falha_de_rede_no_aviso_nao_mata_o_ensaio():
    """O aviso a estragar aquilo que devia estar so a relatar seria o pior
    dos mundos."""
    def rebenta(_):
        raise ConnectionError("sem rede")
    runner.avisar(rebenta, "ola", "resumo")        # nao levanta


def test_avisar_nao_fala_quando_o_modo_e_nenhuma():
    ditas = []
    runner.avisar(ditas.append, "ola", "nenhuma")
    assert ditas == []
    runner.avisar(ditas.append, "ola", "resumo")
    assert ditas == ["ola"]


# ---------------------------------------------------------------------------
#  Aprendizagem entre ensaios: desligada, e nao negociavel
# ---------------------------------------------------------------------------
def test_aprendizagem_forcada_a_off(alvo):
    """O duplo rebenta se as globais ou o cfg chegarem com aprendizagem ligada."""
    r = ensaio(alvo, {}, "2024-02-01", "2024-03-31")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("nome", sorted(runner.PROIBIDOS))
def test_ensaio_nao_pode_religar_a_aprendizagem(alvo, nome):
    r = ensaio(alvo, {nome: True}, "2024-02-01", "2024-03-31")
    assert r.returncode == 2
    assert nome in r.stderr
    assert "comparaveis" in r.stderr


# ---------------------------------------------------------------------------
#  Parametros
# ---------------------------------------------------------------------------
def test_parametro_inexistente_e_erro_nao_silencio(alvo):
    """Deixar passar um nome mal escrito gastava uma tentativa do orcamento
    a correr os valores de origem, e o estudo ficava a mentir sobre o que
    testou."""
    r = ensaio(alvo, {"EMA_FASTT": 12}, "2024-02-01", "2024-03-31")
    assert r.returncode == 2
    assert "EMA_FASTT" in r.stderr
    assert "EMA_FAST" in r.stderr           # sugere o nome parecido


def test_parametro_com_valor_impossivel_e_erro(alvo):
    r = ensaio(alvo, {"EMA_FAST": "abc"}, "2024-02-01", "2024-03-31")
    assert r.returncode == 2
    assert "EMA_FAST" in r.stderr


def test_parametro_e_mesmo_aplicado(alvo):
    """EMA_FAST muda o passo entre trades no duplo: valores diferentes tem
    de dar contagens diferentes, senao o parametro nao chegou la."""
    assert ensaio(alvo, {"EMA_FAST": 1}, "2024-01-02", "2024-04-30", "a.json").returncode == 0
    assert ensaio(alvo, {"EMA_FAST": 9}, "2024-01-02", "2024-04-30", "b.json").returncode == 0
    a = json.loads((alvo / "a.json").read_text())
    b = json.loads((alvo / "b.json").read_text())
    assert a["trades"] > b["trades"]
    assert a["params"] == {"EMA_FAST": 1}


def test_o_tipo_do_parametro_segue_o_da_global(alvo):
    """O JSON nao distingue 12 de 12.0. Um periodo de EMA em float rebenta
    la dentro, num range() ou num indice, longe do sitio onde entrou."""
    mod = runner.carregar_alvo(alvo / "run_backtest.py")
    aplicados = runner.aplicar_params(mod, {"EMA_FAST": 12.0, "FIXED_TP_POINTS": 150})
    assert isinstance(aplicados["EMA_FAST"], int) and aplicados["EMA_FAST"] == 12
    assert isinstance(aplicados["FIXED_TP_POINTS"], float)


def test_inteiro_com_casas_decimais_e_recusado(alvo):
    """12.0 e o mesmo inteiro escrito de outra maneira; 12.7 nao e inteiro
    nenhum, e arredondar em silencio dava um ensaio que testou outra coisa."""
    mod = runner.carregar_alvo(alvo / "run_backtest.py")
    with pytest.raises(runner.ErroRunner, match="casas decimais"):
        runner.aplicar_params(mod, {"EMA_FAST": 12.7})


def test_listar_params_mostra_o_catalogo(alvo):
    r = invocar(alvo, "--listar-params")
    assert r.returncode == 0
    catalogo = json.loads(r.stdout)
    assert catalogo["EMA_FAST"] == 9
    assert "SYMBOL_NAME" in catalogo


# ---------------------------------------------------------------------------
#  Janelas
# ---------------------------------------------------------------------------
def test_a_janela_e_respeitada(alvo):
    assert ensaio(alvo, {}, "2024-02-01", "2024-02-29", "curta.json").returncode == 0
    assert ensaio(alvo, {}, "2024-02-01", "2024-04-30", "longa.json").returncode == 0
    curta = json.loads((alvo / "curta.json").read_text())
    longa = json.loads((alvo / "longa.json").read_text())
    assert curta["dias"] == 29                       # 2024 e bissexto
    assert longa["dias"] == 90
    assert curta["barras"] < longa["barras"]


def test_o_fim_da_janela_e_inclusive(alvo):
    """O ultimo dia conta. Se fosse exclusivo, cada janela do protocolo perdia
    um dia em silencio e o holdout comecava um dia antes do que esta escrito."""
    assert ensaio(alvo, {}, "2024-02-01", "2024-02-05", "a.json").returncode == 0
    assert ensaio(alvo, {}, "2024-02-01", "2024-02-06", "b.json").returncode == 0
    assert json.loads((alvo / "a.json").read_text())["dias"] == 5
    assert json.loads((alvo / "b.json").read_text())["dias"] == 6


def test_janela_curta_de_mais_nao_se_confunde_com_fora_do_cache(alvo):
    curta = ensaio(alvo, {}, "2024-02-01", "2024-02-01", "c.json")
    assert curta.returncode == 2
    assert "esta no cache" in curta.stderr and "ruido" in curta.stderr

    fora = ensaio(alvo, {}, "2030-01-01", "2030-03-01", "f.json")
    assert fora.returncode == 2
    assert "nao tem um unico candle" in fora.stderr


def test_janela_fora_do_cache_diz_o_que_ha(alvo):
    r = ensaio(alvo, {}, "2030-01-01", "2030-03-01")
    assert r.returncode == 2
    assert "2024-01-01" in r.stderr and "2024-07" in r.stderr


def test_datas_ao_contrario_sao_erro(alvo):
    r = ensaio(alvo, {}, "2024-04-30", "2024-02-01")
    assert r.returncode == 2


def test_data_mal_escrita_diz_o_formato(alvo):
    r = ensaio(alvo, {}, "01/02/2024", "2024-04-30")
    assert r.returncode == 2
    assert "YYYY-MM-DD" in r.stderr


def test_sem_cache_manda_encher_o_cache(tmp_path):
    (tmp_path / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    r = ensaio(tmp_path, {}, "2024-02-01", "2024-03-31")
    assert r.returncode == 2
    assert "cache" in r.stderr.lower()


def test_alvo_inexistente_explica_onde_por_o_ficheiro(tmp_path):
    r = subprocess.run([sys.executable, str(RUNNER), "--alvo",
                        str(tmp_path / "nao_existe.py"), "--verificar"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "MESMA pasta" in r.stderr


# ---------------------------------------------------------------------------
#  Metricas
# ---------------------------------------------------------------------------
def test_trade_aberto_nao_entra_na_serie(alvo):
    """O duplo deixa um trade aberto com +999999 pontos. Se ele entrasse,
    o retorno disparava e o ensaio passava o gate por uma posicao que nunca
    foi fechada."""
    assert ensaio(alvo, {}, "2024-02-01", "2024-03-31").returncode == 0
    m = json.loads((alvo / "m.json").read_text())
    assert m["trades"] == m["sinais_totais"] - 1
    assert m["total_return"] < 1.0


def test_ha_um_retorno_por_dia_de_mercado(alvo):
    assert ensaio(alvo, {}, "2024-02-01", "2024-04-30").returncode == 0
    m = json.loads((alvo / "m.json").read_text())
    assert len(m["returns"]) == m["dias"] == 90


def test_periodos_por_ano_saem_dos_dados(tmp_path):
    """Cripto negoceia 7 dias por semana; um instrumento de bolsa nao. O numero
    tem de vir do calendario que os candles mostram, nao de um palpite."""
    (tmp_path / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    escrever_cache(tmp_path, dias=200, passo_min=60)
    assert ensaio(tmp_path, {}, "2024-01-02", "2024-06-30", "cripto.json").returncode == 0
    assert json.loads((tmp_path / "cripto.json").read_text())["periods_per_year"] == 365

    escrever_cache(tmp_path, dias=200, passo_min=60, so_dias_uteis=True)
    assert ensaio(tmp_path, {}, "2024-01-02", "2024-06-30", "bolsa.json").returncode == 0
    ppa = json.loads((tmp_path / "bolsa.json").read_text())["periods_per_year"]
    assert 255 <= ppa <= 266


def test_drawdown_e_a_curva_de_equity_contam_a_mesma_historia(alvo):
    assert ensaio(alvo, {}, "2024-01-02", "2024-04-30").returncode == 0
    m = json.loads((alvo / "m.json").read_text())
    equity = [1.0]
    for r in m["returns"]:
        equity.append(equity[-1] * (1 + r))
    assert m["max_drawdown"] == pytest.approx(runner.drawdown_maximo(equity), abs=1e-9)
    assert m["total_return"] == pytest.approx(equity[-1] - 1.0, abs=1e-9)


def test_a_ruina_e_assinalada():
    dias = ["2024-01-01", "2024-01-02", "2024-01-03"]
    trades = [{"pnl": -20000.0, "close_ts": 1704153600}]      # 2024-01-02
    rets, curva, fechados, ruina = runner.serie_de_retornos(trades, 1.0, dias, 10_000.0)
    assert ruina is True
    assert fechados == 1
    assert curva[-1] == 0.0
    assert runner.drawdown_maximo(curva) == pytest.approx(1.0)


def test_dia_sem_trades_conta_como_zero():
    dias = ["2024-01-01", "2024-01-02", "2024-01-03"]
    rets, curva, fechados, ruina = runner.serie_de_retornos([], 1.0, dias, 10_000.0)
    assert rets == [0.0, 0.0, 0.0]
    assert fechados == 0 and ruina is False


def test_mesmos_parametros_dao_o_mesmo_numero(alvo):
    """Sem isto, o gate estaria a comparar ruido entre ensaios."""
    assert ensaio(alvo, {"EMA_FAST": 3}, "2024-01-02", "2024-04-30", "a.json").returncode == 0
    assert ensaio(alvo, {"EMA_FAST": 3}, "2024-01-02", "2024-04-30", "b.json").returncode == 0
    a = json.loads((alvo / "a.json").read_text())
    b = json.loads((alvo / "b.json").read_text())
    assert a["returns"] == b["returns"]
    assert a["max_drawdown"] == b["max_drawdown"]


# ---------------------------------------------------------------------------
#  O gate do orquestrador tem de conseguir ler isto
# ---------------------------------------------------------------------------
def test_as_metricas_passam_pelo_leitor_do_orquestrador(alvo):
    spec = importlib.util.spec_from_file_location("orq_sut", AQUI / "orquestrador.py")
    orq = importlib.util.module_from_spec(spec)
    sys.modules["orq_sut"] = orq
    spec.loader.exec_module(orq)

    assert ensaio(alvo, {"EMA_FAST": 3}, "2024-01-02", "2024-04-30", "t.json").returncode == 0
    assert ensaio(alvo, {"EMA_FAST": 3}, "2024-05-01", "2024-07-15", "v.json").returncode == 0
    treino = orq.ler_metricas(json.loads((alvo / "t.json").read_text()))
    valida = orq.ler_metricas(json.loads((alvo / "v.json").read_text()))

    assert treino.n_obs == 120 and valida.n_obs == 76
    assert treino.periodos_ano == 365
    assert treino.trades > 0

    v = orq.avaliar(treino, valida, n_ensaios=10,
                    sharpes_anteriores=[0.3, 0.5, 0.42, 0.61], baseline=None)
    assert isinstance(v.passou, bool)
    assert len(v.criterios) >= 4


# ---------------------------------------------------------------------------
#  Detetar um script que fica a ouvir em vez de terminar
# ---------------------------------------------------------------------------
def carregar_orquestrador():
    spec = importlib.util.spec_from_file_location("orq_pistas", AQUI / "orquestrador.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orq_pistas"] = mod
    spec.loader.exec_module(mod)
    return mod


def pistas_de(fonte: str) -> list[str]:
    orq = carregar_orquestrador()
    nu = orq.codigo_nu(fonte)
    return sorted({p for p in orq.PISTAS_INTERATIVO if p in nu})


def test_a_prosa_nao_conta_como_bot():
    """Um ficheiro que EXPLICA como fugir ao Telegram fala de Telegram em cada
    paragrafo. Procurar no texto todo acusava-o de ser aquilo que evita."""
    fonte = '''
"""Corre o backtest sem telegram nenhum, sem polling e sem input()."""
# nada de while True: aqui, nem de flask
def correr():
    """Nao faz polling."""
    return 42
'''
    assert pistas_de(fonte) == []


def test_o_endpoint_conta_como_bot():
    """E num https://api.telegram.org/... que se ve um bot a serio."""
    fonte = 'URL = "https://api.telegram.org/bot123/getUpdates"\ndef f(): return URL\n'
    assert "api.telegram.org" in pistas_de(fonte)
    assert "getupdates" in pistas_de(fonte)


def test_o_ciclo_infinito_conta():
    assert "while true:" in pistas_de("def f():\n    while True:\n        pass\n")


def test_ficheiro_com_erro_de_sintaxe_nao_rebenta():
    """Melhor um falso alarme do que uma excecao no meio do `configurar`."""
    assert isinstance(pistas_de("def f(:\n  isto nao compila\n"), list)


def test_o_proprio_runner_nao_e_acusado():
    """Se o orq_runner fosse marcado como interativo, o aviso mandava-te
    arranjar exatamente o ficheiro que existe para resolver o problema."""
    assert pistas_de(RUNNER.read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------
#  Quanta janela cabe no tempo que ha
# ---------------------------------------------------------------------------
def test_o_orcamento_de_tempo_extrapola_da_medicao():
    """"Da timeout" nao diz o que fazer; "a tua janela e 13x maior do que
    cabe" diz."""
    txt = runner.orcamento_de_tempo(90.0, 90, teto=1800)   # 1s por dia
    assert "1.00s/dia" in txt
    assert "~1440 dias" in txt          # 1800 * 0.8 / 1.0


def test_janela_curta_de_mais_leva_aviso():
    txt = runner.orcamento_de_tempo(900.0, 90, teto=1800)  # 10s por dia
    assert "~144 dias" in txt
    assert "DUAS janelas" in txt         # o custo de um ensaio e o dobro


def test_janela_folgada_nao_leva_aviso():
    txt = runner.orcamento_de_tempo(9.0, 90, teto=1800)    # 0.1s por dia
    assert "DUAS janelas" not in txt


def test_o_orcamento_aguenta_uma_medicao_instantanea():
    assert "s/dia" in runner.orcamento_de_tempo(0.0, 90)


# ---------------------------------------------------------------------------
#  Um erro tem de dizer o que correu mal
# ---------------------------------------------------------------------------
class _AvisoFalso:
    def __init__(self):
        self.enviadas = []

    def enviar(self, msg):
        self.enviadas.append(msg)


class _EstadoFalso:
    def __init__(self):
        self.guardado = {}

    def acabar_ensaio(self, eid, **kw):
        self.guardado[eid] = kw


def falhar(erro, saida):
    orq = carregar_orquestrador()
    o = _OrqFalso([])
    o.estado, o.aviso = _EstadoFalso(), _AvisoFalso()
    orq.Orquestrador._falhar(o, "ens_1", erro, saida)
    return o.aviso.enviadas[0], o.estado.guardado["ens_1"]


def test_o_diagnostico_vai_junto_com_o_erro():
    """`falhou: backtest de treino falhou` nao diz nada e nao da nada para
    fazer a seguir. O diagnostico ja estava na base de dados — faltava
    entrega-lo."""
    msg, gravado = falhar("backtest de treino: saiu com codigo 2 em 5s",
                          "erro: o motor avaliou os sinais e bloqueou-os todos\n"
                          "  90000  fora_de_londres")
    assert "fora_de_londres" in msg and "90000" in msg
    assert gravado["saida"]              # continua a ir para a base de dados


def test_erro_sem_saida_nao_poe_bloco_vazio():
    msg, _ = falhar("sandbox: PROJETO nao existe", None)
    assert "```" not in msg
    assert "PROJETO nao existe" in msg


def test_saida_gigante_e_cortada_pelo_fim():
    """O fim, nao o principio: e ai que vive a mensagem de erro."""
    orq = carregar_orquestrador()
    msg, _ = falhar("x", "COMECO" + ("ruido " * 5000) + "A CAUSA REAL")
    assert "A CAUSA REAL" in msg
    assert "COMECO" not in msg
    assert len(msg) < orq.LIMITE_ERRO_TELEGRAM + 300


def test_o_timeout_distingue_se_de_um_rebentamento():
    """Sem o resumo, um script que expirou e um que rebentou leem-se igual."""
    orq = carregar_orquestrador()
    expirou = orq.Resultado(False, -1, "", 1800.0, True)
    rebentou = orq.Resultado(False, 2, "", 5.0, False)
    assert "timeout" in expirou.resumo and "1800" in expirou.resumo
    assert "codigo 2" in rebentou.resumo


# ---------------------------------------------------------------------------
#  Parametros que nao estao ligados a nada
# ---------------------------------------------------------------------------
class _OrqFalso:
    """So o suficiente para exercitar _params_mortos sem montar o sistema."""

    def __init__(self, historico):
        self._h = historico

    def _historico(self, estudo_id, n=30):
        return self._h[-n:]


def mortos(historico):
    orq = carregar_orquestrador()
    return orq.Orquestrador._params_mortos(_OrqFalso(historico), "est_1")


def h(sharpe, **params):
    return {"params": params, "hipotese": "", "sharpe": sharpe}


def test_tres_ensaios_iguais_denunciam_o_parametro(alvo):
    """O sintoma que custou 12 ensaios: numeros identicos com parametros
    diferentes. Nao e a estrategia a nao prestar — e o motor a nao ler."""
    assert mortos([h(0.0, EMA_FAST=9), h(0.0, EMA_FAST=15),
                   h(0.0, EMA_FAST=30)]) == ["EMA_FAST"]


def test_dois_ensaios_ainda_nao_chegam():
    """Dois valores darem o mesmo numero ainda e coincidencia plausivel."""
    assert mortos([h(0.0, EMA_FAST=9), h(0.0, EMA_FAST=15)]) is None


def test_resultados_que_mexem_nao_acusam_ninguem():
    assert mortos([h(0.0, EMA_FAST=9), h(0.4, EMA_FAST=15),
                   h(0.0, EMA_FAST=30)]) is None


def test_so_acusa_o_parametro_que_variou():
    """Um parametro que ficou parado nao provou nada, e acusa-lo mandava-te
    procurar no sitio errado."""
    assert mortos([h(0.0, EMA_FAST=9, SESSAO_LIMIAR=0.25),
                   h(0.0, EMA_FAST=15, SESSAO_LIMIAR=0.25),
                   h(0.0, EMA_FAST=30, SESSAO_LIMIAR=0.25)]) == ["EMA_FAST"]


def test_varios_mortos_saem_todos():
    assert mortos([h(1.0, A=1, B=10), h(1.0, A=2, B=20),
                   h(1.0, A=3, B=30)]) == ["A", "B"]


def test_ensaios_sem_sharpe_nao_contam():
    assert mortos([h(None, A=1), h(None, A=2), h(None, A=3)]) is None


def test_nao_acusa_quando_os_params_nao_mudaram():
    """Repetir o mesmo ensaio da o mesmo numero, e ainda bem — isso e a
    reprodutibilidade a funcionar, nao um parametro morto."""
    assert mortos([h(0.5, A=1), h(0.5, A=1), h(0.5, A=1)]) is None


# ---------------------------------------------------------------------------
#  Zero trades nao e uma medicao
# ---------------------------------------------------------------------------
def alvo_que_devolve(tmp_path: Path, corpo_do_retorno: str) -> Path:
    """Um duplo cujo run_backtest devolve exatamente o dict que eu mandar."""
    fonte = DUPLO.replace(
        DUPLO[DUPLO.index("def run_backtest("):DUPLO.index('if __name__ == "__main__":')],
        "def run_backtest(bars_raw, digits, label='', cfg=None):\n"
        f"    return {corpo_do_retorno}\n\n")
    (tmp_path / "run_backtest.py").write_text(fonte, encoding="utf-8")
    escrever_cache(tmp_path, dias=200, passo_min=60)
    return tmp_path


def test_zero_trades_com_funil_diz_que_filtro_matou(tmp_path):
    """O gate recebia `Sharpe 0.00` e chumbava por `sharpe_oos` — a mensagem
    culpava o parametro quando o problema era um filtro a cortar tudo."""
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': [], "
                         "'no_entry_diagnostics': {'fora_de_londres': 90000, "
                         "'sem_rompimento': 412, 'faixa_asia_curta': 7}}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert r.returncode != 0
    assert "bloqueou-os todos" in r.stderr
    assert "fora_de_londres" in r.stderr and "90000" in r.stderr
    assert not (p / "m.json").exists()      # nao escreve metricas falsas


def test_o_funil_aponta_para_o_fim_e_nao_para_o_maior(tmp_path):
    """O funil real do utilizador, que mostrou o defeito da mensagem antiga.

    Ordenar por contagem punha `fora_de_londres` em primeiro e mandava
    afrouxa-lo — mas esse filtro rejeita 3/4 do dia POR DESENHO: a estrategia
    so opera em Londres. Afrouxa-lo era desligar a estrategia.

    Quem tem contagem pequena esta no FIM do funil: sao os sinais que
    sobreviveram a tudo e morreram a um passo da entrada.
    """
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': [], "
                         "'no_entry_diagnostics': {'fora_de_londres': 1723695, "
                         "'sem_rompimento': 304923, 'faixa_ja_operada': 268546, "
                         "'spread_excedido': 1145, 'faixa_asia_curta': 360}}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert r.returncode != 0

    fim = r.stderr[r.stderr.index(">>>"):]
    assert "spread_excedido" in fim          # o que se pode arranjar
    assert "fora_de_londres" not in fim      # esse e por desenho
    assert "spread_excedido" in r.stderr[r.stderr.index("tens de mexer") - 120:]
    assert "1505 sinais chegaram" in r.stderr    # 1145 + 360


def test_funil_so_estrutural_diz_que_nada_chegou_ao_fim(tmp_path):
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': [], "
                         "'no_entry_diagnostics': {'fora_de_londres': 500000, "
                         "'sem_rompimento': 400000}}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert "nunca gerou uma entrada" in r.stderr


def test_os_dois_contadores_do_alvo_entram_no_funil(tmp_path):
    """O alvo tem dois: `no_entry_diagnostics` antes da entrada e
    `context_blocks` depois. Mostrar so um escondia metade da historia."""
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': [], "
                         "'no_entry_diagnostics': {'fora_de_londres': 900000}, "
                         "'context_blocks': {'score_baixo': 12}}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert "fora_de_londres" in r.stderr and "score_baixo" in r.stderr


def test_zero_trades_sem_funil_aponta_para_as_janelas(tmp_path):
    """Sem `no_entry_diagnostics`, o teu script desistiu por falta de candles
    validos — e isso quase sempre e a janela a nao bater com o cache."""
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': []}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert r.returncode != 0
    assert "nem chegou a avaliar" in r.stderr
    assert "--verificar" in r.stderr


def test_trades_todos_abertos_tambem_e_zero(tmp_path):
    p = alvo_que_devolve(tmp_path, "{'total': 1, 'closed': 0, 'trades': ["
                         "{'side': 'BUY', 'pnl': 500.0, 'close_ts': 0, 'open_ts': 1}]}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert r.returncode != 0
    assert "nenhum fechou" in r.stderr


def test_a_janela_do_cache_aparece_no_diagnostico(tmp_path):
    p = alvo_que_devolve(tmp_path, "{'total': 0, 'closed': 0, 'trades': []}")
    r = ensaio(p, {}, "2024-02-01", "2024-04-30")
    assert "2024-02-01" in r.stderr and "2024-04-30" in r.stderr


def test_com_trades_o_funil_entra_nas_metricas(alvo):
    """Com trades a mais isto e curiosidade; quando a contagem cai a pique e a
    primeira coisa que se quer ver."""
    assert ensaio(alvo, {}, "2024-02-01", "2024-04-30").returncode == 0
    assert "bloqueios" in json.loads((alvo / "m.json").read_text())


# ---------------------------------------------------------------------------
#  O arnes entra no worktree sem passar pelo git
# ---------------------------------------------------------------------------
def repo_com_arnes(tmp_path: Path) -> Path:
    """Um projeto git onde o orq_runner.py existe mas NAO esta versionado."""
    import subprocess as sp
    projeto = tmp_path / "projeto"
    projeto.mkdir()
    (projeto / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    (projeto / ".gitignore").write_text("orq_runner.py\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"]):
        sp.run(["git", *cmd], cwd=projeto, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "inicial"], cwd=projeto, check=True, capture_output=True)
    (projeto / "orq_runner.py").write_text("# sou o arnes\n", encoding="utf-8")
    return projeto


def test_o_arnes_chega_ao_worktree_sem_estar_no_git(tmp_path):
    """O worktree so traz o que esta no git. O arnes nao esta — e nao deve
    estar, porque o que esta dentro do worktree e alteravel."""
    orq = carregar_orquestrador()
    projeto = repo_com_arnes(tmp_path)
    with orq.Sandbox("ens_teste", projeto=projeto,
                     worktrees=tmp_path / "wt") as caixa:
        raiz = caixa.raiz
        assert "orq_runner.py" not in orq.Sandbox(
            "ens_x", projeto=projeto).ficheiros_versionados()
        assert (raiz / "run_backtest.py").is_file()          # veio do git
        assert (raiz / "orq_runner.py").is_file()            # veio da copia
        assert (raiz / "orq_runner.py").read_text(encoding="utf-8") == "# sou o arnes\n"
        # copia e nao atalho: um atalho deixava o ensaio mexer no teu original
        assert not (raiz / "orq_runner.py").is_symlink()
        (raiz / "orq_runner.py").write_text("# estragado\n", encoding="utf-8")

    with orq.Sandbox("ens_dois", projeto=projeto,
                     worktrees=tmp_path / "wt") as caixa:
        raiz = caixa.raiz
        assert (raiz / "orq_runner.py").read_text(encoding="utf-8") == "# sou o arnes\n"
        assert (projeto / "orq_runner.py").read_text(encoding="utf-8") == "# sou o arnes\n"


def test_alterar_o_arnes_e_sempre_recusado():
    """Mesmo com FICHEIROS_EDITAVEIS = ["*"]. A copia fresca protege o ensaio
    seguinte; nao protege ESTE, e e este que da a nota."""
    orq = carregar_orquestrador()
    with pytest.raises(orq.CaminhoProibido, match="arnes"):
        orq.exigir_permitido("orq_runner.py", ["*"])


def test_a_estrategia_continua_editavel():
    orq = carregar_orquestrador()
    orq.exigir_permitido("run_backtest.py", ["run_backtest.py"])   # nao levanta


def test_verificar_diz_o_que_ha_no_cache(alvo):
    r = invocar(alvo, "--verificar")
    assert r.returncode == 0
    assert "ETHUSD" in r.stdout
    assert "2024-01-01" in r.stdout
    assert "offline" in r.stdout.lower()


def test_verificar_sem_cache_falha(tmp_path):
    (tmp_path / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    r = invocar(tmp_path, "--verificar")
    assert r.returncode == 1
    assert "NENHUM" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ===========================================================================
#  NAVEGACAO — o agente vai buscar o codigo em vez de o receber picotado
# ===========================================================================
FICHEIRO_GRANDE = '''
"""Um alvo com a forma do real: constantes no topo, uso 200 linhas abaixo."""
SYMBOL_NAME = "ETHUSD"
MAX_SPREAD_PIPS = 1.2
ESTRATEGIA = "sessao"
''' + "\n".join(f"_ENCHIMENTO_{i} = {i}" for i in range(200)) + '''

def _sessao_sinal(preco, faixa, limiar):
    """A decisao de entrada, isolada para ser testavel."""
    if not faixa:
        return None, "sem_faixa"
    return "BUY", ""


def calcular(x):
    return x * MAX_SPREAD_PIPS
'''


@pytest.fixture
def alvo_grande(tmp_path: Path) -> Path:
    (tmp_path / "alvo.py").write_text(FICHEIRO_GRANDE, encoding="utf-8")
    return tmp_path


def nav_de(pasta: Path):
    orq = carregar_orquestrador()
    return orq, orq.Navegador(pasta, ["alvo.py"])


def test_estrutura_da_o_indice_com_linhas(alvo_grande):
    """Por omissao so nomes e linhas: chega para decidir onde olhar, que e para
    o que o indice serve. Com valores dava 24 KB no ficheiro real — 61% do
    orcamento numa consulta, e reenviado a cada volta ate o modelo desistir."""
    _, nav = nav_de(alvo_grande)
    est = nav.estrutura()
    assert "SYMBOL_NAME" in est and "_sessao_sinal" in est
    assert "CONSTANTES" in est and "FUNCOES" in est
    assert "ETHUSD" not in est                     # valor nao, so o nome
    assert "(preco, faixa, limiar)" not in est     # assinatura tambem nao


def test_estrutura_com_detalhe_traz_os_valores(alvo_grande):
    _, nav = nav_de(alvo_grande)
    assert "ETHUSD" in nav.estrutura(detalhe="constantes")
    assert "(preco, faixa, limiar)" in nav.estrutura(detalhe="funcoes")


def test_procurar_liga_partes_distantes(alvo_grande):
    """E para isto que a navegacao existe: a constante esta no topo e o uso
    duzentas linhas abaixo, e as duas aparecem no mesmo resultado."""
    _, nav = nav_de(alvo_grande)
    r = nav.procurar("MAX_SPREAD_PIPS")
    assert "= 1.2" in r and "x * MAX_SPREAD_PIPS" in r


def test_funcao_devolve_o_corpo_inteiro(alvo_grande):
    _, nav = nav_de(alvo_grande)
    r = nav.funcao("_sessao_sinal")
    assert "sem_faixa" in r and 'return "BUY"' in r


def test_ler_respeita_o_teto_de_linhas(alvo_grande):
    _, nav = nav_de(alvo_grande)
    r = nav.ler(1, 9999, max_linhas=10)
    assert "cortei em 10 linhas" in r
    assert len([l for l in r.splitlines() if " | " in l]) == 10


def test_erros_voltam_como_texto_nunca_como_excecao(alvo_grande):
    """O modelo tem de poder corrigir-se; excecao aqui matava o turno."""
    _, nav = nav_de(alvo_grande)
    for pedido in ({"ferramenta": "ler", "argumentos": {"inicio": 99999, "fim": 99999}},
                   {"ferramenta": "ler", "argumentos": {"inicio": 50, "fim": 10}},
                   {"ferramenta": "procurar", "argumentos": {"termo": ""}},
                   {"ferramenta": "voar", "argumentos": {}},
                   {"ferramenta": "ler", "argumentos": {"ficheiro": "nao_ha.py",
                                                        "inicio": 1, "fim": 2}}):
        r = nav.executar(pedido)
        assert r.startswith("ERRO:"), pedido


def test_funcao_inexistente_sugere_o_caminho(alvo_grande):
    _, nav = nav_de(alvo_grande)
    r = nav.funcao("nao_existe")
    assert "nao encontrei" in r and "estrutura()" in r


def test_o_navegador_de_leitura_recusa_correr(alvo_grande):
    """Projeto vivo nao e sitio para um modelo executar comandos."""
    _, nav = nav_de(alvo_grande)
    assert nav.executar({"ferramenta": "correr",
                         "argumentos": {"comando": "ls"}}).startswith("ERRO:")
    assert "correr" not in nav.ferramentas()


# ---------------------------------------------------------------------------
#  Correr codigo: o que a reposicao tem de garantir
# ---------------------------------------------------------------------------
def projeto_git(tmp_path: Path) -> Path:
    """Um projeto versionado, com arnes fora do git e dados ignorados."""
    import subprocess as sp
    p = tmp_path / "projeto"
    (p / "data").mkdir(parents=True)
    (p / "estrategia.py").write_text("LIMIAR = 1.0\n", encoding="utf-8")
    (p / "arnes.py").write_text("# calcula as metricas\nVERDADE = 42\n", encoding="utf-8")
    (p / ".gitignore").write_text("data/\narnes.py\n.orq/\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"]):
        sp.run(["git", *cmd], cwd=p, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
           cwd=p, check=True, capture_output=True)
    return p


def sandbox_de(tmp_path: Path):
    orq = carregar_orquestrador()
    orq.FICHEIROS_ARNES = ["arnes.py"]
    orq.PASTAS_LIGADAS = ["data"]
    orq.FICHEIROS_EDITAVEIS = ["estrategia.py"]
    return orq, projeto_git(tmp_path)


def test_repor_desfaz_o_que_o_agente_fez_por_shell(tmp_path):
    """Com shell no worktree ele contorna a lista branca inteira. A reposicao e
    o que impede isso de chegar aos numeros."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_1", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        (sb.raiz / "estrategia.py").write_text("LIMIAR = 999\n", encoding="utf-8")
        (sb.raiz / "intruso.py").write_text("nada\n", encoding="utf-8")
        sb.repor()
        assert (sb.raiz / "estrategia.py").read_text() == "LIMIAR = 1.0\n"
        assert not (sb.raiz / "intruso.py").exists()


def test_repor_devolve_o_arnes_original(tmp_path):
    """O arnes calcula as metricas. Reescreve-lo por shell seria o agente a
    redigir o proprio boletim."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_2", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        (sb.raiz / "arnes.py").write_text("VERDADE = 'mentira'\n", encoding="utf-8")
        sb.repor()
        assert "VERDADE = 42" in (sb.raiz / "arnes.py").read_text()


def test_repor_apaga_metricas_plantadas(tmp_path):
    """Um `echo` para o .orq/metricas.json bastava, se ele sobrevivesse."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_3", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        (sb.raiz / ".orq").mkdir(exist_ok=True)
        (sb.raiz / ".orq" / "metricas.json").write_text('{"sharpe": 99}', encoding="utf-8")
        sb.repor()
        assert not (sb.raiz / ".orq" / "metricas.json").exists()


def test_repor_nao_leva_os_atalhos_dos_dados(tmp_path):
    """Sem os dados o backtest correria vazio, e a defesa quebrava o produto."""
    orq, projeto = sandbox_de(tmp_path)
    (projeto / "data" / "candles.bin").write_bytes(b"x" * 10)
    with orq.Sandbox("ens_4", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        sb.repor()
        assert (sb.raiz / "data" / "candles.bin").exists()


def test_a_alteracao_aprovada_sobrevive_a_reposicao(tmp_path):
    """A ordem importa: repor primeiro, aplicar depois. Ao contrario, a defesa
    apagava exatamente aquilo que se queria medir."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_5", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        (sb.raiz / "estrategia.py").write_text("LIXO\n", encoding="utf-8")
        sb.repor()
        ok, _ = sb.aplicar("estrategia.py",
                           [{"procurar": "LIMIAR = 1.0", "substituir": "LIMIAR = 2.5"}])
        assert ok
        assert "LIMIAR = 2.5" in (sb.raiz / "estrategia.py").read_text()


def test_o_navegador_do_ensaio_corre_e_ve_o_resultado(tmp_path):
    """Ler diz o que o codigo DIZ; correr diz o que ele FAZ."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_6", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        nav = orq.NavegadorComExecucao(sb.raiz, ["estrategia.py"], sb)
        assert "correr" in nav.ferramentas()
        r = nav.executar({"ferramenta": "correr", "argumentos": {
            "comando": "python3 -c \"import estrategia; print('LIMIAR', estrategia.LIMIAR)\""}})
        assert "LIMIAR 1.0" in r
        assert nav.corridas and nav.corridas[0]["codigo"] == 0


def test_correr_invalida_o_cache_de_leitura(tmp_path):
    """Depois de um comando mexer no ficheiro, continuar a servir a versao
    antiga poria o agente a raciocinar sobre codigo que ja nao existe."""
    orq, projeto = sandbox_de(tmp_path)
    with orq.Sandbox("ens_7", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        nav = orq.NavegadorComExecucao(sb.raiz, ["estrategia.py"], sb)
        assert "1.0" in nav.ler(1, 5)
        nav.executar({"ferramenta": "correr", "argumentos": {
            "comando": "python3 -c \"open('estrategia.py','w').write('LIMIAR = 7.0')\""}})
        assert "7.0" in nav.ler(1, 5)


# ---------------------------------------------------------------------------
#  O ciclo: pedir codigo, depois responder
# ---------------------------------------------------------------------------
def ciclo(alvo_grande: Path, respostas: list[str], **kw):
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso(respostas)
    saida = orq.correr_agente_navegando(
        llm, papel="teste", modelo="m", sistema="s", prompt="p",
        validar=lambda d: d["ok"], navegador=nav, **kw)
    return saida, llm, nav


def test_o_agente_consulta_e_so_depois_responde(alvo_grande):
    saida, llm, nav = ciclo(alvo_grande, [
        '{"ferramenta": "estrutura", "argumentos": {}}',
        '{"ferramenta": "funcao", "argumentos": {"nome": "_sessao_sinal"}}',
        '{"ok": "pronto"}'])
    assert saida == "pronto"
    assert nav.pedidos == 2
    # o resultado da consulta anterior tem de estar na mensagem seguinte,
    # senao ele nao acumula nada e cada volta comeca do zero
    assert "sem_faixa" in llm.chamadas[-1]["utilizador"]


def test_nada_e_pre_enviado(alvo_grande):
    """Nem o indice. Uma pergunta que nao e sobre codigo nao paga 6 mil tokens."""
    _, llm, _ = ciclo(alvo_grande, ['{"ok": 1}'])
    primeira = llm.chamadas[0]["utilizador"]
    assert "_ENCHIMENTO_100" not in primeira
    assert "estrutura" in primeira          # sabe que PODE pedir


def test_um_erro_do_navegador_nao_mata_o_turno(alvo_grande):
    saida, _, _ = ciclo(alvo_grande, [
        '{"ferramenta": "ler", "argumentos": {"inicio": 99999, "fim": 99999}}',
        '{"ok": "recuperou"}'])
    assert saida == "recuperou"


def test_resposta_invalida_volta_com_o_motivo(alvo_grande):
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso(['{"errado": 1}', '{"ok": "agora sim"}'])

    def validar(d):
        if "ok" not in d:
            raise ValueError("falta a chave `ok`")
        return d["ok"]

    saida = orq.correr_agente_navegando(llm, papel="t", modelo="m", sistema="s",
                                        prompt="p", validar=validar, navegador=nav)
    assert saida == "agora sim"
    assert "falta a chave `ok`" in llm.chamadas[-1]["utilizador"]


def test_o_teto_de_consultas_e_respeitado(alvo_grande):
    pedido = '{"ferramenta": "estrutura", "argumentos": {}}'
    saida, _, nav = ciclo(alvo_grande, [pedido] * 4 + ['{"ok": "fim"}'],
                          max_ferramentas=3)
    assert saida == "fim"
    assert nav.pedidos == 3


def test_insistir_em_pedir_depois_do_teto_gasta_as_tentativas(alvo_grande):
    """Depois de lhe dizerem para responder, cada pedido novo E uma resposta
    falhada. Sem isso, um modelo teimoso girava aqui para sempre."""
    orq, nav = nav_de(alvo_grande)
    pedido = '{"ferramenta": "estrutura", "argumentos": {}}'
    llm = orq.ModeloFalso([pedido] * 30)
    with pytest.raises(orq.ErroAgente, match="tentativas"):
        orq.correr_agente_navegando(
            llm, papel="t", modelo="m", sistema="s", prompt="p", navegador=nav,
            validar=lambda d: d["ok"], max_ferramentas=2, tentativas=3)
    assert nav.pedidos == 2
    assert len(llm.chamadas) == 5          # 2 consultas + 3 tentativas, e para


def test_estourar_o_orcamento_deixa_marca(alvo_grande):
    """Ele tem de SABER que perdeu consultas, senao raciocina sobre o que ja
    nao esta la."""
    saida, llm, _ = ciclo(alvo_grande, [
        '{"ferramenta": "estrutura", "argumentos": {}}',
        '{"ferramenta": "estrutura", "argumentos": {}}',
        '{"ferramenta": "estrutura", "argumentos": {}}',
        '{"ok": "fim"}'], orcamento=500)
    assert saida == "fim"
    assert "descartadas" in llm.chamadas[-1]["utilizador"]


def test_sem_resposta_valida_rebenta_com_o_motivo(alvo_grande):
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso(['{"nao": 1}'] * 40)
    with pytest.raises(orq.ErroAgente, match="falta"):
        orq.correr_agente_navegando(
            llm, papel="t", modelo="m", sistema="s", prompt="p", navegador=nav,
            validar=lambda d: d["ok"] if "ok" in d else (_ for _ in ()).throw(
                ValueError("falta `ok`")))


# ---------------------------------------------------------------------------
#  Ficheiro grande de mais: entra com aviso, nunca em silencio
# ---------------------------------------------------------------------------
def test_ficheiro_grande_nao_desaparece(tmp_path):
    """Era descartado sem aviso, e o agente propunha alteracoes a um ficheiro
    que nunca tinha visto — inventando nomes e funcoes."""
    orq, projeto = sandbox_de(tmp_path)
    orq.TETO_FICHEIRO_INTEIRO = 50
    (projeto / "gordo.py").write_text("X = 1\n" * 200, encoding="utf-8")
    import subprocess as sp
    sp.run(["git", "add", "-A"], cwd=projeto, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "g"],
           cwd=projeto, check=True, capture_output=True)
    with orq.Sandbox("ens_g", projeto=projeto, worktrees=tmp_path / "wt") as sb:
        vistos = sb.ler_visiveis()
        assert "gordo.py" in vistos
        assert "grande de mais" in vistos["gordo.py"]
        assert "procurar()" in vistos["gordo.py"]


# ---------------------------------------------------------------------------
#  O `local` da hipotese
# ---------------------------------------------------------------------------
def test_local_valido_e_aceite(alvo_grande):
    _, nav = nav_de(alvo_grande)
    assert nav.existe({"funcao": "_sessao_sinal"}) == ""
    assert nav.existe({"inicio": 1, "fim": 5}) == ""


def test_local_inventado_e_recusado_cedo(alvo_grande):
    """Uma funcao que nao existe descoberta agora e uma linha de erro que o
    modelo corrige; descoberta no programador e um ensaio perdido."""
    _, nav = nav_de(alvo_grande)
    assert "nao encontrei" in nav.existe({"funcao": "inventada"})
    assert nav.existe({"ficheiro": "outro.py", "funcao": "x"})
    assert nav.existe({}) and nav.existe("nao e dict")


def test_trecho_devolve_so_a_regiao(alvo_grande):
    _, nav = nav_de(alvo_grande)
    t = nav.trecho({"funcao": "_sessao_sinal"})
    assert "sem_faixa" in t
    assert "_ENCHIMENTO_100" not in t


def test_procurar_encontra_constantes_alinhadas_com_espacos(alvo_grande):
    """O alvo real alinha as constantes com padding:

        SYMBOL_NAME          = "ETHUSD"

    Procurar `SYMBOL_NAME =` a letra encontrava so os USOS, nunca a definicao —
    e o agente concluia que a constante nao existia. Que e exatamente o engano
    que esta navegacao existe para acabar."""
    (alvo_grande / "alvo.py").write_text(
        'SYMBOL_NAME          = "ETHUSD"\n'
        'MAX_SPREAD_PIPS      = 1.2\n'
        'def f():\n    return SYMBOL_NAME\n', encoding="utf-8")
    _, nav = nav_de(alvo_grande)
    r = nav.procurar("SYMBOL_NAME =")
    assert "ETHUSD" in r
    assert "MAX_SPREAD" in nav.procurar("MAX_SPREAD_PIPS = 1.2")


# ---------------------------------------------------------------------------
#  O que os modelos guionados nunca apanharam
# ---------------------------------------------------------------------------
class ModeloLento:
    """Nao responde. Foi assim que o minimax:cloud falhou na primeira corrida,
    e nenhum teste tinha maneira de ver isso: um modelo guionado responde
    sempre, e sempre depressa."""

    def __init__(self, orq):
        self.erro = orq.ErroModelo
        self.chamadas = []

    def conversar(self, sistema, utilizador, *, modelo, json_mode=True):
        self.chamadas.append({"sistema": sistema, "utilizador": utilizador,
                              "modelo": modelo})
        raise self.erro(f"o modelo {modelo} nao respondeu em 300s.")


def test_um_modelo_que_nao_responde_e_dito_como_tal(alvo_grande):
    """"Nao chegou a uma resposta valida" mandava procurar no formato, quando o
    problema era o modelo nao atender. Sao arranjos diferentes."""
    orq, nav = nav_de(alvo_grande)
    with pytest.raises(orq.ErroAgente) as e:
        orq.correr_agente_navegando(
            ModeloLento(orq), papel="pesquisa", modelo="minimax-m3:cloud",
            sistema="s", prompt="p", validar=lambda d: d, navegador=nav)
    msg = str(e.value)
    assert "nao respondeu a tempo" in msg
    assert "MAX_FERRAMENTAS" in msg          # diz o que fazer a seguir


def test_um_modelo_que_erra_o_formato_e_dito_como_tal(alvo_grande):
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso(['{"errado": 1}'] * 20)
    with pytest.raises(orq.ErroAgente) as e:
        orq.correr_agente_navegando(
            llm, papel="conversa", modelo="m", sistema="s", prompt="p",
            navegador=nav,
            validar=lambda d: d["resposta"] if "resposta" in d
            else (_ for _ in ()).throw(ValueError("falta a chave `resposta`")))
    msg = str(e.value)
    assert "fora do formato" in msg
    assert "falta a chave `resposta`" in msg


def test_gastar_as_consultas_sem_responder_e_dito_como_tal(alvo_grande):
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso(['{"ferramenta": "estrutura", "argumentos": {}}'] * 20)
    with pytest.raises(orq.ErroAgente) as e:
        orq.correr_agente_navegando(
            llm, papel="t", modelo="m", sistema="s", prompt="p", navegador=nav,
            validar=lambda d: d["ok"], max_ferramentas=2, tentativas=2)
    assert "Gastou as 2 consultas" in str(e.value)


# ---------------------------------------------------------------------------
#  num_ctx: so aos locais
# ---------------------------------------------------------------------------
class OllamaEspiao:
    """O Ollama de verdade, sem rede: guarda a carga em vez de a mandar."""

    def __init__(self, orq):
        self.orq, self.cargas = orq, []

    def conversar(self, sistema, utilizador, *, modelo, json_mode=True):
        carga = {"model": modelo, "options": {"temperature": 0.2}}
        if modelo in self.orq.JANELA_MODELO:
            carga["options"]["num_ctx"] = self.orq.JANELA_MODELO[modelo]
        self.cargas.append(carga)
        return '{"ok": 1}'


def test_num_ctx_so_vai_para_os_modelos_locais():
    """Impor num_ctx a um modelo de nuvem nao traz beneficio — o contexto e
    decidido do lado dele — e foi o que o pos a nao responder em 300s."""
    orq = carregar_orquestrador()
    assert "qwen2.5-coder:7b" in orq.JANELA_MODELO
    assert not any(":cloud" in m for m in orq.JANELA_MODELO)

    espiao = OllamaEspiao(orq)
    espiao.conversar("s", "u", modelo="minimax-m3:cloud")
    espiao.conversar("s", "u", modelo="qwen2.5-coder:7b")
    assert "num_ctx" not in espiao.cargas[0]["options"]
    assert espiao.cargas[1]["options"]["num_ctx"] == 32_768


def test_o_codigo_real_nao_manda_num_ctx_a_nuvem():
    """Le a montagem da carga no proprio ficheiro: o teste acima usa um espiao,
    e um espiao pode divergir do original sem ninguem dar por isso."""
    fonte = (AQUI / "orquestrador.py").read_text(encoding="utf-8")
    i = fonte.index('"options": {"temperature"')
    trecho = fonte[i:i + 400]
    assert "if modelo in JANELA_MODELO" in trecho
    assert "JANELA_PADRAO" not in fonte      # o omissao para todos morreu


# ---------------------------------------------------------------------------
#  Nenhuma consulta pode dominar o contexto
# ---------------------------------------------------------------------------
def test_um_resultado_gigante_e_cortado(alvo_grande):
    orq, nav = nav_de(alvo_grande)
    r = nav.executar({"ferramenta": "ler", "argumentos": {"inicio": 1, "fim": 300}})
    assert len(r) <= orq.MAX_RESULTADO + 200
    if len(r) > orq.MAX_RESULTADO:
        assert "cortei aqui" in r


def test_o_indice_do_ficheiro_real_cabe_numa_consulta():
    """A medida que interessa, no alvo verdadeiro de 19.363 linhas."""
    import shutil, tempfile
    orq = carregar_orquestrador()
    real = Path("/root/.claude/uploads/80f6f73a-ad3f-566c-8e0a-6294c59d82c0/"
                "8d052ae9-run_backtest.py")
    if not real.exists():
        pytest.skip("o ficheiro real nao esta disponivel neste ambiente")
    d = Path(tempfile.mkdtemp())
    shutil.copy(real, d / "run_backtest.py")
    nav = orq.Navegador(d, ["run_backtest.py"])
    est = nav.executar({"ferramenta": "estrutura", "argumentos": {}})
    assert len(est) <= orq.MAX_RESULTADO
    assert "cortei aqui" not in est          # cabe inteiro, nao truncado
    # e as constantes que interessam estao la, com a linha
    for nome in ("SYMBOL_NAME", "ESTRATEGIA", "MAX_SPREAD_PIPS", "SESSAO_LIMIAR"):
        assert nome in est


# ---------------------------------------------------------------------------
#  O formato final e a ultima coisa que ele le
# ---------------------------------------------------------------------------
def test_o_lembrete_do_formato_fica_no_fim(alvo_grande):
    """As instrucoes de navegacao acabavam com exemplos de CHAMADAS. A ultima
    coisa que ele lia era como pedir codigo, e respondia com um pedido quando
    devia responder de vez."""
    orq, nav = nav_de(alvo_grande)
    llm = orq.ModeloFalso([
        '{"ferramenta": "estrutura", "argumentos": {}}',
        '{"nao_serve": 1}',
        '{"ok": 1}'])
    orq.correr_agente_navegando(
        llm, papel="t", modelo="m", sistema="s", prompt="p", navegador=nav,
        formato='{"ok": "..."}',
        validar=lambda d: d["ok"] if "ok" in d
        else (_ for _ in ()).throw(ValueError("falta `ok`")))
    # em todas as voltas, incluindo com historico E com erro anterior
    for c in llm.chamadas:
        assert c["utilizador"].rstrip().endswith('{"ok": "..."}')


# ===========================================================================
#  LEITURA DE CONTEXTO
#
#  Contra velas construidas a mao, onde a resposta certa se sabe de antemao.
#  Uma estatistica testada contra dados aleatorios so prova que nao rebenta.
# ===========================================================================
def vela(t, o, h, l, c):
    return (t, float(o), float(h), float(l), float(c))


def barras_ctrader(velas_ohlc, t0=0):
    """As mesmas velas na forma que a API do cTrader devolve."""
    saida = []
    for i, (o, h, l, c) in enumerate(velas_ohlc):
        low = int(round(l * 1e5))
        saida.append({"utcTimestampInMinutes": t0 + i, "low": low,
                      "deltaOpen": int(round(o * 1e5)) - low,
                      "deltaHigh": int(round(h * 1e5)) - low,
                      "deltaClose": int(round(c * 1e5)) - low})
    return saida


class ModuloFalso:
    """O minimo que o /contexto precisa de um run_backtest.py."""

    def __init__(self, barras, digits=2, **extra):
        self._barras = barras
        self._digits = digits
        self._state = {"symbol_digits": digits}
        self.SYMBOL_NAME = "ETHUSD"
        self.MARKET_SPECS = {"ETHUSD": {"spread": 8.0}}
        self.descargas = 0
        for k, v in extra.items():
            setattr(self, k, v)

    def _bar_ts_min(self, b):
        return int(b.get("utcTimestampInMinutes") or 0)

    def _load_bars_cache(self, simbolo, tf="M1"):
        return list(self._barras), self._digits

    def _bars_cache_path(self, simbolo, tf="M1"):
        return f"/nao/existe/bars_{simbolo}_M1.pkl"


# -- estatistica ------------------------------------------------------------
def test_atr_de_wilder_bate_a_conta_a_mao():
    """14 barras com TR=10 e uma com TR=24: (10*13 + 24)/14 = 11."""
    velas = [vela(i, 100, 105, 95, 100) for i in range(15)]
    velas.append(vela(15, 100, 124, 100, 120))
    assert runner.atr(velas, 14) == pytest.approx(11.0)


def test_atr_sem_historia_devolve_none():
    assert runner.atr([vela(i, 100, 101, 99, 100) for i in range(5)], 14) is None


def test_extremos_e_posicao_por_horizonte():
    velas = [vela(0, 100, 110, 90, 105), vela(1, 105, 120, 100, 118)]
    h = runner._horizonte("teste", velas, preco=115.0, atr_ref=10.0, atr_tf="1h")
    assert (h["maxima"], h["minima"]) == (120.0, 90.0)
    assert h["amplitude_pts"] == pytest.approx(30.0)
    assert h["amplitude_atr"] == pytest.approx(3.0)
    assert h["posicao_pct"] == pytest.approx(100 * (115 - 90) / 30)


def test_um_horizonte_vazio_nao_rebenta():
    h = runner._horizonte("vazio", [], preco=100.0, atr_ref=1.0, atr_tf="1h")
    assert h["maxima"] is None and h["posicao_pct"] is None


def test_a_maior_perna_nao_e_a_amplitude():
    """Sobe 40, corrige 35, sobe 38: amplitude 43, maior perna 40.

    Se estes dois numeros fossem iguais, a metrica nao estaria a medir nada —
    foi exatamente o erro da primeira versao.
    """
    subida = [vela(i, 100 + i * 4, 100 + i * 4, 100 + i * 4, 100 + i * 4) for i in range(11)]
    descida = [vela(11 + i, 140 - i * 3.5, 140 - i * 3.5, 140 - i * 3.5, 140 - i * 3.5)
               for i in range(11)]
    volta = [vela(22 + i, 105 + i * 3.8, 105 + i * 3.8, 105 + i * 3.8, 105 + i * 3.8)
             for i in range(11)]
    velas = subida + descida + volta
    alta, baixa = runner.extremos(velas)
    assert alta - baixa == pytest.approx(43.0)
    assert runner.maior_movimento(velas, limiar=10.0) == pytest.approx(40.0)


def test_sem_limiar_a_maior_perna_cai_para_a_amplitude():
    velas = [vela(0, 100, 130, 100, 120), vela(1, 120, 120, 90, 95)]
    assert runner.maior_movimento(velas, limiar=0) == pytest.approx(40.0)


def test_percentil_da_amplitude_separa_o_dia_extremo_do_normal():
    amostra = [10.0] * 89 + [11.0]
    extremo, n = runner.percentil(50.0, amostra, minimo=30)
    normal, _ = runner.percentil(10.0, amostra, minimo=30)
    assert n == 90 and extremo == pytest.approx(100.0)
    assert normal < 10


def test_percentil_recusa_se_a_amostra_for_curta_mas_diz_o_n():
    pct, n = runner.percentil(5.0, [1.0] * 12, minimo=30)
    assert pct is None and n == 12


def test_velas_seguidas_conta_do_fim_e_o_doji_corta():
    subir = [vela(i, 100, 101, 99, 101) for i in range(3)]
    assert runner.seguidas(subir) == 3
    assert runner.seguidas(subir + [vela(3, 100, 101, 99, 100)]) == 0
    descer = [vela(i, 100, 101, 99, 99) for i in range(2)]
    assert runner.seguidas(subir + descer) == -2


def test_frequencia_da_serie_sem_amostra_devolve_none_e_o_n():
    freq, n = runner.frequencia_de_serie([vela(i, 100, 101, 99, 101) for i in range(10)], 3)
    assert freq is None and n == 8


# -- o formato do candle ----------------------------------------------------
OHLC_EXEMPLO = [(100.0, 110.0, 95.0, 105.0), (105.0, 112.0, 103.0, 108.0)]


def test_as_tres_formas_de_candle_dao_o_mesmo():
    """cTrader, OHLC direto e uma funcao do modulo tem de concordar.

    Se discordassem, o mesmo mercado dava leituras diferentes conforme o
    ficheiro do utilizador — e nao haveria maneira de saber qual estava certa.
    """
    ct = ModuloFalso(barras_ctrader(OHLC_EXEMPLO))
    f_ct, desc_ct = runner.achar_ohlc(ct, ct._barras)

    directas = [{"utcTimestampInMinutes": i, "open": o, "high": h, "low": l, "close": c}
                for i, (o, h, l, c) in enumerate(OHLC_EXEMPLO)]
    f_dir, desc_dir = runner.achar_ohlc(ModuloFalso(directas), directas)

    proprias = [{"utcTimestampInMinutes": i, "v": [o, h, l, c]}
                for i, (o, h, l, c) in enumerate(OHLC_EXEMPLO)]
    mod = ModuloFalso(proprias)
    mod._bar_ohlc = lambda b: tuple(b["v"])
    f_mod, desc_mod = runner.achar_ohlc(mod, proprias)

    for i, esperado in enumerate(OHLC_EXEMPLO):
        assert f_ct(ct._barras[i]) == pytest.approx(esperado)
        assert f_dir(directas[i]) == pytest.approx(esperado)
        assert f_mod(proprias[i]) == pytest.approx(esperado)
    assert "cTrader" in desc_ct and "direto" in desc_dir and "_bar_ohlc" in desc_mod


def test_a_funcao_do_teu_ficheiro_ganha_as_outras():
    """Se o teu ficheiro sabe ler o candle, e ele que manda — nao o meu palpite."""
    barras = barras_ctrader(OHLC_EXEMPLO)
    mod = ModuloFalso(barras)
    mod._bar_ohlc = lambda b: (1.0, 2.0, 3.0, 4.0)
    f, desc = runner.achar_ohlc(mod, barras)
    assert f(barras[0]) == (1.0, 2.0, 3.0, 4.0)
    assert "_bar_ohlc" in desc


def test_um_candle_ja_convertido_nao_e_dividido_outra_vez():
    """low=3412.5 e um preco, nao 341.250.000 pontos. A escala tem de ver isso."""
    barras = [{"utcTimestampInMinutes": 0, "low": 3400.0, "deltaOpen": 5.0,
               "deltaHigh": 12.5, "deltaClose": 8.0}]
    f, desc = runner.achar_ohlc(ModuloFalso(barras), barras)
    assert f(barras[0]) == pytest.approx((3405.0, 3412.5, 3400.0, 3408.0))
    assert "escala 1" in desc


def test_um_formato_desconhecido_falha_a_dizer_as_chaves():
    barras = [{"utcTimestampInMinutes": 0, "preco_medio": 100, "tick": 3}]
    with pytest.raises(runner.ErroRunner) as e:
        runner.achar_ohlc(ModuloFalso(barras), barras)
    texto = str(e.value)
    assert "preco_medio" in texto and "tick" in texto
    assert "utcTimestampInMinutes" in texto


# -- de onde vem o candle ---------------------------------------------------
def barras_longas(n=6000, base=3400.0):
    """Velas suficientes para haver ATR, faixas e historia."""
    ohlc = []
    for i in range(n):
        p = base + (i % 50) * 0.1
        ohlc.append((p, p + 0.5, p - 0.5, p + 0.2))
    return barras_ctrader(ohlc, t0=0)


def test_desligado_a_descarga_nunca_e_chamada():
    """O CONTEXTO_AO_VIVO desligado tem de significar zero toques na rede."""
    mod = ModuloFalso(barras_longas())
    chamadas = []
    mod.fetch_bars = lambda symbol, hours=48: chamadas.append(symbol)
    d = runner.contexto(mod, "ETHUSD", ao_vivo=False)
    assert chamadas == []
    assert d["fonte"] == "cache"
    assert "desligado" in d["motivo_da_fonte"]


def test_ligado_a_descarga_e_chamada_e_a_fonte_diz_ctrader():
    mod = ModuloFalso(barras_longas())
    chamadas = []

    def fetch_bars(symbol, hours=48):
        chamadas.append((symbol, hours))
        return mod._barras[-60:]

    mod.fetch_bars = fetch_bars
    d = runner.contexto(mod, "ETHUSD", ao_vivo=True)
    assert chamadas and chamadas[0][0] == "ETHUSD"
    assert d["fonte"] == "cTrader" and d["motivo_da_fonte"] == ""


@pytest.mark.parametrize("falha,pedaco", [
    (lambda symbol, hours=48: (_ for _ in ()).throw(ConnectionError("recusou")), "recusou"),
    (lambda symbol, hours=48: [], "nao devolveu candle"),
])
def test_uma_descarga_que_falha_cai_para_o_cache_com_a_razao(falha, pedaco):
    """Falhar nao pode matar o comando: os numeros do cache continuam uteis."""
    mod = ModuloFalso(barras_longas())
    mod.fetch_bars = falha
    d = runner.contexto(mod, "ETHUSD", ao_vivo=True)
    assert d["fonte"] == "cache" and pedaco in d["motivo_da_fonte"]


def test_uma_descarga_pendurada_nao_prende_o_comando():
    import time as _t
    mod = ModuloFalso(barras_longas())
    mod.fetch_bars = lambda symbol, hours=48: _t.sleep(30)
    inicio = _t.monotonic()
    d = runner.contexto(mod, "ETHUSD", ao_vivo=True, timeout=1)
    assert _t.monotonic() - inicio < 10
    assert d["fonte"] == "cache" and "sem responder" in d["motivo_da_fonte"]


def test_sem_funcao_de_descarga_a_razao_nomeia_as_candidatas():
    mod = ModuloFalso(barras_longas())
    mod._get_bars_do_broker = lambda s: []      # nome fora da lista, mas com cara
    d = runner.contexto(mod, "ETHUSD", ao_vivo=True)
    assert d["fonte"] == "cache"
    assert "_get_bars_do_broker" in d["motivo_da_fonte"]


def test_a_descarga_indicada_a_mao_e_respeitada(monkeypatch):
    monkeypatch.setattr(runner, "FUNCAO_DESCARGA", "_o_meu_download")
    mod = ModuloFalso(barras_longas())
    mod._o_meu_download = lambda symbol, hours=48: mod._barras[-60:]
    nome, _ = runner.achar_descarga(mod)
    assert nome == "_o_meu_download"


def test_uma_descarga_com_argumentos_que_nao_percebo_diz_quais():
    mod = ModuloFalso(barras_longas())
    mod.fetch_bars = lambda conta, cofre: []
    d = runner.contexto(mod, "ETHUSD", ao_vivo=True)
    assert "conta" in d["motivo_da_fonte"] and "cofre" in d["motivo_da_fonte"]


def test_os_frescos_juntam_se_a_historia_do_cache():
    """Os frescos dao o agora, o cache da os 90 dias. Nenhum chega sozinho."""
    velhas = barras_longas(3000)
    novas = barras_ctrader([(9.0, 9.5, 8.5, 9.2)] * 3, t0=3000)
    juntas = runner._juntar(velhas, novas, lambda b: int(b["utcTimestampInMinutes"]))
    assert len(juntas) == 3003
    assert [b["utcTimestampInMinutes"] for b in juntas] == sorted(
        b["utcTimestampInMinutes"] for b in juntas)


# -- a faixa da Asia --------------------------------------------------------
def test_a_faixa_da_asia_usa_as_horas_do_teu_ficheiro():
    mod = ModuloFalso([], ASIA_INICIO=22, ASIA_FIM=7)
    ini, fim, origem = runner.horas_da_asia(mod)
    assert (ini, fim) == (22, 7) and "do teu ficheiro" in origem


def test_sem_constantes_tuas_a_faixa_diz_que_horas_usou():
    ini, fim, origem = runner.horas_da_asia(ModuloFalso([]))
    assert (ini, fim) == runner.ASIA_OMISSAO
    assert "omissao" in origem


def test_a_faixa_que_atravessa_a_meia_noite_e_a_que_ja_comecou():
    """22h-07h as 03:00 e a de ontem a noite, nao a de hoje a noite."""
    agora = 5 * 1440 + 3 * 60          # dia 5, 03:00
    a, b = runner.janela_asia(agora, 22, 7)
    assert a == 4 * 1440 + 22 * 60 and b == 5 * 1440 + 7 * 60
    assert a <= agora < b


def test_a_faixa_normal_e_a_de_hoje_depois_de_ela_fechar():
    agora = 5 * 1440 + 20 * 60         # dia 5, 20:00
    a, b = runner.janela_asia(agora, 0, 7)
    assert (a, b) == (5 * 1440, 5 * 1440 + 7 * 60)


def test_uma_varrida_da_asia_e_reconhecida_como_tal():
    """Sair da faixa e voltar e manipulacao; sair e ficar fora e outra coisa."""
    dentro = [vela(i, 100, 101, 99, 100) for i in range(5)]
    saiu_e_voltou = [vela(5, 100, 105, 99, 100)]
    saiu_e_ficou = [vela(5, 100, 105, 99, 104)]
    assert runner._rompeu(saiu_e_voltou, 101.0, 99.0) == ("acima", True)
    assert runner._rompeu(saiu_e_ficou, 101.0, 99.0) == ("acima", False)
    assert runner._rompeu(dentro, 101.0, 99.0) == ("nao", False)


# -- a regua ----------------------------------------------------------------
NIVEIS = [("max hoje", 3470.20), ("max 4h", 3455.00), ("max 15m", 3418.00),
          ("min 15m", 3404.20), ("min Asia", 3388.40), ("min ontem", 3310.50)]
FAIXAS = [("Asia", 3409.40, 3388.40), ("hoje", 3470.20, 3388.40),
          ("ontem", 3402.00, 3310.50)]
PRECO = 3412.50


def test_a_regua_faz_as_contas_do_teu_exemplo():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "compra", FAIXAS, spread=8.0)
    assert r["R"] == pytest.approx(412 / 215)
    assert r["equilibrio_pct"] == pytest.approx(100 * 215 / 627, abs=0.05)
    assert r["equilibrio_com_spread_pct"] == pytest.approx(100 * 223 / 627, abs=0.05)
    assert r["take_preco"] == pytest.approx(PRECO + 412)
    assert r["stop_preco"] == pytest.approx(PRECO - 215)


def test_a_escada_sai_ordenada_com_o_take_e_o_stop_no_sitio():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "compra", FAIXAS)
    precos = [d["preco"] for d in r["degraus"]]
    assert precos == sorted(precos, reverse=True)
    assert r["degraus"][0]["marca"] == "TAKE"
    assert r["degraus"][-1]["marca"] == "STOP"


def test_as_distancias_batem_em_pontos_em_atr_e_em_R():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "compra", FAIXAS)
    por_nome = {d["etiqueta"]: d for d in r["degraus"]}
    alto = por_nome["max hoje"]
    assert alto["pontos"] == pytest.approx(3470.20 - PRECO)
    assert alto["atr"] == pytest.approx((3470.20 - PRECO) / 14.8)
    assert alto["R"] == pytest.approx((3470.20 - PRECO) / 215)
    baixo = por_nome["min ontem"]
    assert baixo["pontos"] < 0 and baixo["R"] < 0
    assert baixo["atr"] > 0            # o ATR e distancia, nao direcao
    assert por_nome["STOP"]["R"] == pytest.approx(-1.0)


def test_um_take_para_la_de_um_nivel_avisa_por_quantos_pontos():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "compra", FAIXAS)
    alem = r["take_alem_de"]
    assert alem["nivel"] == "max hoje"
    assert alem["pontos"] == pytest.approx(412 - (3470.20 - PRECO))


def test_um_take_aquem_de_todos_os_niveis_nao_avisa_nada():
    r = runner.regua(NIVEIS, PRECO, 14.8, 20, 3, "compra", FAIXAS)
    assert r["take_alem_de"] is None


def test_um_stop_dentro_da_faixa_da_asia_e_apontado():
    r = runner.regua(NIVEIS, PRECO, 14.8, 10, 40, "compra", FAIXAS)
    faixas = [x["faixa"] for x in r["stop_dentro_de"]]
    assert "Asia" in faixas                      # 3402,50 cai dentro de 3388-3409


def test_um_stop_fora_de_todas_as_faixas_nao_e_apontado():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "compra", FAIXAS)
    assert r["stop_dentro_de"] == []


def test_do_lado_da_venda_o_take_e_o_stop_trocam_de_lado():
    r = runner.regua(NIVEIS, PRECO, 14.8, 215, 412, "venda", FAIXAS)
    assert r["lado"] == "venda"
    assert r["take_preco"] == pytest.approx(PRECO - 412)
    assert r["stop_preco"] == pytest.approx(PRECO + 215)
    por_nome = {d["etiqueta"]: d for d in r["degraus"]}
    assert por_nome["TAKE"]["R"] == pytest.approx(412 / 215)   # favoravel = positivo
    assert por_nome["STOP"]["R"] == pytest.approx(-1.0)
    assert por_nome["min ontem"]["R"] > 0        # cair e a favor de quem vende


def test_sem_stop_nem_take_a_regua_sai_na_mesma():
    r = runner.regua(NIVEIS, PRECO, 14.8, None, None, "compra", FAIXAS)
    assert len(r["degraus"]) == len(NIVEIS)
    assert all(d["marca"] is None for d in r["degraus"])
    assert r["take_alem_de"] is None and r["stop_dentro_de"] == []
    assert r["R"] is None


# -- o modo, de ponta a ponta -----------------------------------------------
def instantaneo(pasta: Path) -> dict:
    """Ficheiros teus e o que lhes aconteceu. O __pycache__ nao conta: e o
    Python a guardar o import compilado, coisa que o teu proprio bot ja faz —
    nao e uma alteracao ao teu codigo nem aos teus dados."""
    return {p: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(pasta.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def test_o_contexto_nao_altera_nada_teu(alvo, tmp_path_factory):
    """Uma leitura nao pode mexer no projeto. Nem no cache."""
    fora = tmp_path_factory.mktemp("saida")
    antes = instantaneo(alvo)
    r = invocar(alvo, "--contexto", "--out", str(fora / "ctx.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert instantaneo(alvo) == antes


def test_o_contexto_nao_religa_a_aprendizagem(alvo, tmp_path):
    """O duplo rebenta se o Telegram falar; o FORCAR_DESLIGADO continua a valer."""
    saida = tmp_path / "ctx.json"
    r = invocar(alvo, "--contexto", "--out", str(saida))
    assert r.returncode == 0, r.stdout + r.stderr
    mod = runner.carregar_alvo(alvo / "run_backtest.py")
    assert set(runner.PROIBIDOS) <= set(runner.FORCAR_DESLIGADO)
    assert mod.BOT_ARRANCOU is False


def test_o_contexto_escreve_o_json_com_o_que_o_placar_precisa(alvo, tmp_path):
    saida = tmp_path / "ctx.json"
    assert invocar(alvo, "--contexto", "--out", str(saida),
                   "--stop", "215", "--take", "412").returncode == 0
    d = json.loads(saida.read_text(encoding="utf-8"))
    for chave in ("preco", "ts_min", "atr", "historico_1h", "regua",
                  "horizontes", "fonte", "idade_min", "notavel"):
        assert chave in d, chave
    assert d["regua"]["R"] == pytest.approx(412 / 215)
    assert d["historico_1h"] and len(d["historico_1h"][0]) == 2


def test_o_verificar_diz_as_duas_incognitas(alvo):
    """O formato do candle e a funcao de descarga, ditos antes de darem erro."""
    r = invocar(alvo, "--verificar")
    assert r.returncode == 0, r.stderr
    assert "candle    :" in r.stdout
    assert "descarga  :" in r.stdout
    assert "faixa Asia:" in r.stdout


def test_um_cache_velho_e_dito_como_velho():
    """Ler dados velhos sabendo que sao velhos e util. A pensar que sao de
    agora e o unico resultado mesmo mau."""
    import time as _t
    agora = int(_t.time()) // 60
    velhas = barras_ctrader([(100.0, 100.5, 99.5, 100.2)] * 6000,
                            t0=agora - 3 * 24 * 60 - 6000)
    d = runner.contexto(ModuloFalso(velhas), "ETHUSD", ao_vivo=False)
    assert d["idade_min"] > 3 * 24 * 60 - 60


# ===========================================================================
#  O GUARDA DOS NUMEROS, A TABELA E O PLACAR  (lado do orquestrador)
# ===========================================================================
def contexto_de_exemplo(tmp_path: Path, stop=215, take=412) -> dict:
    (tmp_path / "run_backtest.py").write_text(DUPLO, encoding="utf-8")
    escrever_cache(tmp_path, dias=200, passo_min=60)
    saida = tmp_path / "ctx.json"
    r = invocar(tmp_path, "--contexto", "--out", str(saida),
                "--stop", str(stop), "--take", str(take))
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(saida.read_text(encoding="utf-8"))


def test_um_numero_inventado_e_apanhado_e_nomeado():
    orq = carregar_orquestrador()
    permitidos = orq.numeros_dos_dados({"preco": 3412.5, "atr": 14.8, "max": 3470.2})
    assert orq.numero_inventado("resistencia forte em 3.500", permitidos) == "3.500"
    assert orq.numero_inventado("alvo 4.000,00", permitidos) == "4.000,00"


def test_o_mesmo_numero_a_portuguesa_e_aceite():
    """3.412,50 e 3412.5 sao o mesmo numero. Rejeitar isto seria o guarda a
    apanhar aritmetica correta em vez de invencao."""
    orq = carregar_orquestrador()
    permitidos = orq.numeros_dos_dados({"preco": 3412.5, "r": 1.916279, "eq": 34.29027})
    assert orq.numero_inventado("o preco esta em 3.412,50", permitidos) is None
    assert orq.numero_inventado("R de 1,92 e equilibrio de 34,3%", permitidos) is None


def test_contagens_pequenas_passam_mas_percentagens_nao():
    """"3 velas" e uma contagem; "subiu 12%" e uma afirmacao sobre os dados."""
    orq = carregar_orquestrador()
    permitidos = orq.numeros_dos_dados({"preco": 3412.5, "freq": 8.0})
    assert orq.numero_inventado("3 velas seguidas a subir", permitidos) is None
    assert orq.numero_inventado("acontece em 8% dos casos", permitidos) is None
    assert orq.numero_inventado("subiu 12%", permitidos) == "12"


def test_a_tabela_e_a_regua_cabem_numa_mensagem(tmp_path):
    orq = carregar_orquestrador()
    texto = orq.formatar_contexto(contexto_de_exemplo(tmp_path))
    assert len(texto) < 3000                      # o Telegram corta aos 4096
    assert "REGUA DE PONTOS" in texto
    assert "TAKE" in texto and "STOP" in texto
    assert "1,92" in texto                        # R a portuguesa, nao 1.92
    assert "1.92" not in texto


def test_a_tabela_diz_sempre_de_onde_vieram_os_dados(tmp_path):
    orq = carregar_orquestrador()
    dados = contexto_de_exemplo(tmp_path)
    texto = orq.formatar_contexto(dados)
    assert dados["fonte"] in texto
    assert "candle de ha" in texto


class ModeloInventor:
    """Responde sempre com um numero que nao esta nos dados."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.pedidos = 0

    def conversar(self, sistema, prompt, modelo=None, json_mode=True):
        self.pedidos += 1
        return self.respostas[min(self.pedidos - 1, len(self.respostas) - 1)]


LEITURA_MA = json.dumps({"leitura": "o preco vai a 9.999,99", "hipotese": "sobe",
                         "invalidacao": "perde o suporte", "direcao": "subir",
                         "confianca": "alta"})
LEITURA_BOA = json.dumps({"leitura": "mercado apertado", "hipotese": "continua",
                          "invalidacao": "perde a minima da Asia",
                          "direcao": "lateral", "confianca": "baixa"})


def test_o_modelo_corrige_se_depois_de_lhe_dizerem_o_numero(tmp_path):
    """O ciclo so funciona porque o erro nomeia o numero: sem isso, o modelo
    nao tem por onde se corrigir."""
    orq = carregar_orquestrador()
    dados = contexto_de_exemplo(tmp_path)
    llm = ModeloInventor([LEITURA_MA, LEITURA_BOA])
    agentes = orq.Agentes(llm, None)
    leitura = agentes.ler_mercado(dados)
    assert llm.pedidos == 2
    assert leitura["direcao"] == "lateral"


def test_um_modelo_que_inventa_sempre_nao_rebenta_a_tabela(tmp_path):
    orq = carregar_orquestrador()
    dados = contexto_de_exemplo(tmp_path)
    agentes = orq.Agentes(ModeloInventor([LEITURA_MA]), None)
    with pytest.raises(orq.ErroAgente) as e:
        agentes.ler_mercado(dados)
    assert "9.999,99" in str(e.value)
    # e a tabela continua a sair inteira, que e o que decide
    assert "REGUA DE PONTOS" in orq.formatar_contexto(dados)


def test_uma_direcao_fora_das_tres_e_recusada():
    orq = carregar_orquestrador()
    with pytest.raises(ValueError) as e:
        orq._validar_leitura({"leitura": "a", "hipotese": "b", "invalidacao": "c",
                              "direcao": "talvez", "confianca": "alta"}, set())
    assert "talvez" in str(e.value)


def estado_limpo(tmp_path: Path):
    orq = carregar_orquestrador()
    return orq, orq.Estado(tmp_path / "orq.sqlite")


def guardar(estado, direcao: str, preco: float, ts_min: int,
            limiar: float = 10.0, confianca: str = "media") -> str:
    return estado.nova_leitura(
        "ETHUSD", preco, ts_min, "cache", limiar,
        {"leitura": "x", "hipotese": "y", "invalidacao": "z",
         "direcao": direcao, "confianca": confianca})


def test_o_placar_vazio_nao_da_percentagem_nenhuma(tmp_path):
    orq, estado = estado_limpo(tmp_path)
    assert "%" not in orq._fmt_placar(estado.placar())


def test_o_placar_com_tres_leituras_diz_que_nao_chega(tmp_path):
    """Uma percentagem sobre tres leituras nao e um resultado: e uma maneira
    de ganhar confianca sem ter ganho nada."""
    orq, estado = estado_limpo(tmp_path)
    for i in range(3):
        lid = guardar(estado, "subir", 100.0, i * 60)
        estado.marcar_leitura(lid, "acertou" if i else "falhou", 110.0, 4.0)
    texto = orq._fmt_placar(estado.placar())
    assert "2 acertaram" in texto and "1 falharam" in texto
    assert "%" not in texto
    assert "faltam" in texto


def test_o_placar_com_amostra_suficiente_da_a_taxa(tmp_path):
    orq, estado = estado_limpo(tmp_path)
    for i in range(orq.MINIMO_LEITURAS_PARA_PLACAR):
        lid = guardar(estado, "subir", 100.0, i * 60)
        estado.marcar_leitura(lid, "acertou" if i % 2 == 0 else "falhou", 110.0, 4.0)
    texto = orq._fmt_placar(estado.placar())
    assert "50%" in texto


def test_uma_leitura_e_conferida_contra_o_preco_que_houve_mesmo(tmp_path):
    """Por codigo, contra os fechos reais — nao perguntando ao modelo se acha
    que acertou."""
    orq, estado = estado_limpo(tmp_path)
    o = orq.Orquestrador(estado, None, aviso=type("A", (), {"enviar": lambda *a: None})())
    t0 = 10_000
    subiu = guardar(estado, "subir", 100.0, t0)
    desceu = guardar(estado, "descer", 100.0, t0)
    parado = guardar(estado, "lateral", 100.0, t0)
    depois = t0 + orq.HORAS_PARA_AVALIAR * 60
    dados = {"historico_1h": [[t0, 100.0], [depois, 130.0]]}

    assert o.avaliar_leituras(dados) == 3
    por_id = {r["id"]: r for r in estado.c.execute("SELECT * FROM leituras")}
    assert por_id[subiu]["resultado"] == "acertou"
    assert por_id[desceu]["resultado"] == "falhou"
    assert por_id[parado]["resultado"] == "falhou"
    assert por_id[subiu]["preco_depois"] == pytest.approx(130.0)
    assert por_id[subiu]["horas_depois"] == pytest.approx(orq.HORAS_PARA_AVALIAR)


def test_um_movimento_abaixo_do_limiar_fica_indeciso(tmp_path):
    """Meio ATR nao e "subiu". Contar isso como acerto seria dar razao a
    qualquer leitura que apontasse para algum lado."""
    orq, estado = estado_limpo(tmp_path)
    o = orq.Orquestrador(estado, None, aviso=type("A", (), {"enviar": lambda *a: None})())
    t0 = 10_000
    lid = guardar(estado, "subir", 100.0, t0, limiar=10.0)
    parado = guardar(estado, "lateral", 100.0, t0, limiar=10.0)
    dados = {"historico_1h": [[t0, 100.0], [t0 + orq.HORAS_PARA_AVALIAR * 60, 103.0]]}
    o.avaliar_leituras(dados)
    por_id = {r["id"]: r for r in estado.c.execute("SELECT * FROM leituras")}
    assert por_id[lid]["resultado"] == "indeciso"
    assert por_id[parado]["resultado"] == "acertou"


def test_uma_leitura_recente_de_mais_fica_a_espera(tmp_path):
    orq, estado = estado_limpo(tmp_path)
    o = orq.Orquestrador(estado, None, aviso=type("A", (), {"enviar": lambda *a: None})())
    t0 = 10_000
    guardar(estado, "subir", 100.0, t0)
    o.avaliar_leituras({"historico_1h": [[t0, 100.0], [t0 + 60, 130.0]]})
    assert estado.placar()["por_decidir"] == 1


def test_uma_leitura_fora_da_janela_do_historico_nao_fica_pendurada(tmp_path):
    """Uma leitura eterna por decidir seria um acerto que nunca se cobra."""
    orq, estado = estado_limpo(tmp_path)
    o = orq.Orquestrador(estado, None, aviso=type("A", (), {"enviar": lambda *a: None})())
    guardar(estado, "subir", 100.0, 1_000)
    o.avaliar_leituras({"historico_1h": [[900_000, 100.0], [900_060, 101.0]]})
    linha = list(estado.c.execute("SELECT resultado FROM leituras"))[0]
    assert linha["resultado"] == "sem dados"
    assert estado.placar()["por_decidir"] == 0


def test_a_tabela_leituras_aparece_numa_base_que_ja_existia(tmp_path):
    """Quem ja tem historico nao pode ter de recomecar por eu acrescentar uma
    tabela."""
    orq = carregar_orquestrador()
    caminho = tmp_path / "antiga.sqlite"
    primeiro = orq.Estado(caminho)
    primeiro.c.execute("DROP TABLE leituras")
    primeiro.c.close()
    segundo = orq.Estado(caminho)
    assert segundo.placar()["total"] == 0


# ===========================================================================
#  O ERRO TEM DE DIZER A VERDADE
#
#  Um 402 do Ollama ("this model requires a subscription") chegou ao Telegram
#  vestido de "respondeu, mas fora do formato pedido — troca-o por outro". Nao
#  respondeu, nao foi formato, e o conselho mandava trocar um modelo de nuvem
#  por outro da MESMA conta. Estes testes existem por causa disso.
# ===========================================================================
class RespostaFalsa:
    """O minimo do requests.Response que o erro_de_estado usa."""

    def __init__(self, status, texto=""):
        self.status_code = status
        self.text = texto
        self.ok = 200 <= status < 300


CORPO_402 = ('{"error":"this model requires a subscription or extra usage, '
             'upgrade for access at https://ollama.com/upgrade"}')


def test_o_402_e_dito_como_cobranca_e_nao_como_formato():
    orq = carregar_orquestrador()
    e = orq.erro_de_estado(RespostaFalsa(402, CORPO_402), "minimax-m3:cloud")
    assert e.permanente is True
    texto = f"{e} {e.pista}".lower()
    assert "conta" in texto and "acesso" in texto
    assert "formato" not in texto
    assert "ollama.com/upgrade" in texto
    assert "local" in texto              # diz a alternativa que nao custa nada


def test_a_frase_do_ollama_aparece_sem_o_json_a_volta():
    """Despejar {"error": ...} fazia parecer avaria minha, quando la dentro
    esta uma frase clara sobre a conta de quem a le."""
    orq = carregar_orquestrador()
    e = orq.erro_de_estado(RespostaFalsa(402, CORPO_402), "m:cloud")
    assert "requires a subscription" in str(e)
    assert '{"error"' not in str(e)


def test_um_corpo_que_nao_e_json_passa_a_mesma():
    orq = carregar_orquestrador()
    e = orq.erro_de_estado(RespostaFalsa(500, "Bad Gateway"), "m:cloud")
    assert "Bad Gateway" in str(e)


@pytest.mark.parametrize("estado,permanente", [
    (401, True), (402, True), (403, True), (404, True),
    (429, False), (500, False), (503, False),
])
def test_o_que_vale_a_pena_repetir_e_o_que_nao_vale(estado, permanente):
    orq = carregar_orquestrador()
    e = orq.erro_de_estado(RespostaFalsa(estado, '{"error":"x"}'), "m:cloud")
    assert e.permanente is permanente


def test_o_404_continua_a_mandar_fazer_pull():
    orq = carregar_orquestrador()
    e = orq.erro_de_estado(RespostaFalsa(404, '{"error":"model not found"}'), "gemma4:26b")
    assert "ollama pull gemma4:26b" in e.pista


class ModeloQueRecusa:
    """Levanta sempre o mesmo erro. Conta as chamadas."""

    def __init__(self, orq, **kw):
        self.erro = orq.ErroModelo
        self.kw = kw
        self.chamadas = 0

    def conversar(self, sistema, utilizador, *, modelo, json_mode=True):
        self.chamadas += 1
        raise self.erro("a tua conta Ollama nao tem acesso", **self.kw)


def test_um_erro_permanente_gasta_uma_tentativa_e_nao_tres():
    """Repetir um 402 tres vezes so multiplica a espera pelo mesmo erro."""
    orq = carregar_orquestrador()
    llm = ModeloQueRecusa(orq, permanente=True, pista="paga ou usa um local")
    with pytest.raises(orq.ErroAgente) as e:
        orq.correr_agente(llm, papel="p", modelo="m:cloud", sistema="s",
                          prompt="p", validar=lambda d: d, tentativas=3)
    assert llm.chamadas == 1
    assert "paga ou usa um local" in str(e.value)
    assert "repetir nao ia mudar nada" in str(e.value)


def test_um_erro_temporario_continua_a_gastar_as_tentativas():
    """O ciclo de correcao e o que torna os modelos pequenos utilizaveis. Nao
    pode ser sacrificado para arranjar o caso do 402."""
    orq = carregar_orquestrador()
    llm = ModeloQueRecusa(orq, permanente=False)
    with pytest.raises(orq.ErroAgente):
        orq.correr_agente(llm, papel="p", modelo="m", sistema="s", prompt="p",
                          validar=lambda d: d, tentativas=3)
    assert llm.chamadas == 3


def test_a_pista_do_erro_chega_intacta_e_nao_e_reescrita():
    """Quem levantou o erro sabia a causa; quem o apanha so a pode adivinhar."""
    orq = carregar_orquestrador()
    e = orq.ErroModelo("seja o que for", permanente=True, pista="FAZ ISTO E MAIS NADA")
    assert orq.pista_do_erro(e, houve_resposta=False) == "FAZ ISTO E MAIS NADA"
    assert orq.pista_do_erro(e, houve_resposta=True, consultas=99,
                             max_ferramentas=1) == "FAZ ISTO E MAIS NADA"


def test_sem_resposta_nenhuma_o_erro_nao_diz_que_respondeu():
    """Este e o ramo que mentiu: dizia "respondeu, mas fora do formato" sobre
    um modelo que nunca abriu a boca."""
    orq = carregar_orquestrador()
    pista = orq.pista_do_erro(orq.ErroModelo("qualquer coisa"), houve_resposta=False)
    assert "nao chegou a responder" in pista.lower()
    assert "fora do formato" not in pista


def test_com_resposta_torta_o_erro_diz_que_foi_o_formato():
    orq = carregar_orquestrador()
    pista = orq.pista_do_erro(ValueError("falta a chave x"), houve_resposta=True)
    assert "fora do formato" in pista


def test_o_402_a_navegar_nao_e_relatado_como_problema_de_formato(alvo_grande):
    """O caso real, de ponta a ponta: 0 consultas, e a mensagem tem de falar
    de conta, nao de formato nem de trocar de modelo."""
    orq, nav = nav_de(alvo_grande)
    llm = ModeloQueRecusa(orq, permanente=True,
                          pista="Isto e cobranca, nao codigo — poe creditos ou usa um local.")
    with pytest.raises(orq.ErroAgente) as e:
        orq.correr_agente_navegando(
            llm, papel="conversa", modelo="minimax-m3:cloud", sistema="s",
            prompt="p", validar=lambda d: d, navegador=nav)
    msg = str(e.value)
    assert llm.chamadas == 1
    assert "cobranca" in msg
    assert "fora do formato" not in msg
    assert "aguentar" not in msg          # nada de "nao esta a aguentar navegar"


# -- o doctor tem de testar o que so se descobre a testar --------------------
def test_testar_modelo_devolve_none_quando_o_modelo_corre(monkeypatch):
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.Ollama, "conversar",
                        lambda self, s, u, *, modelo, json_mode=True: "ok")
    assert orq.testar_modelo("gemma4:26b") is None


def test_testar_modelo_devolve_o_erro_quando_a_conta_nao_tem_acesso(monkeypatch):
    orq = carregar_orquestrador()

    def recusa(self, s, u, *, modelo, json_mode=True):
        raise orq.erro_de_estado(RespostaFalsa(402, CORPO_402), modelo)

    monkeypatch.setattr(orq.Ollama, "conversar", recusa)
    falha = orq.testar_modelo("minimax-m3:cloud")
    assert falha is not None and falha.permanente
    assert "402" in str(falha)


def test_cada_papel_diz_o_que_se_perde_com_ele():
    """Um ❌ sozinho a dizer 402 nao te diz se ainda podes trabalhar."""
    orq = carregar_orquestrador()
    for papel in ("pesquisa", "desenvolvimento", "params", "relatorio", "contexto"):
        assert orq.COMANDOS_POR_PAPEL.get(papel), papel
    assert "/contexto" in orq.COMANDOS_POR_PAPEL["contexto"][0]
    assert "/tarefa" in orq.COMANDOS_POR_PAPEL["pesquisa"][0]


def test_o_doctor_apanha_o_402_em_vez_de_dar_visto_verde(tmp_path, monkeypatch, capsys):
    """O caso que passou despercebido: o modelo esta na lista do `ollama list`
    e da 402 em todas as chamadas. O doctor existe para isto nao chegar ao
    Telegram a meio de uma pergunta."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq, "BD", tmp_path / "orq.sqlite")
    monkeypatch.setattr(orq, "PROJETO", str(tmp_path / "nao_existe"))
    monkeypatch.setattr(orq, "MODELO_PESQUISA", "minimax-m3:cloud")
    monkeypatch.setattr(orq, "MODELO_DESENVOLVIMENTO", "dcxglm-5.2:cloud")
    monkeypatch.setattr(orq, "MODELO_RELATORIO", "gemma4:26b")
    monkeypatch.setattr(orq, "MODELO_CONTEXTO", "gemma4:26b")
    monkeypatch.setattr(orq, "MODO", "code")
    monkeypatch.setattr(orq, "procurar_web",
                        lambda *a, **k: (_ for _ in ()).throw(orq.ErroWeb("sem rede")))
    monkeypatch.setattr(orq.Ollama, "modelos",
                        lambda self: ["gemma4:26b", "minimax-m3:cloud",
                                      "dcxglm-5.2:cloud"])

    pedidos = []

    def conversar(self, s, u, *, modelo, json_mode=True):
        pedidos.append(modelo)
        raise orq.erro_de_estado(RespostaFalsa(402, CORPO_402), modelo)

    monkeypatch.setattr(orq.Ollama, "conversar", conversar)
    orq.doctor()
    saida = capsys.readouterr().out

    # Os dois de nuvem foram mesmo chamados; o local NAO — sabe-se pela lista,
    # e um pedido a serio carregava um 26b para a memoria por nada.
    assert sorted(pedidos) == ["dcxglm-5.2:cloud", "minimax-m3:cloud"]
    assert "❌ pesquisa: minimax-m3:cloud" in saida
    assert "❌ desenvolvimento: dcxglm-5.2:cloud" in saida
    assert "✅ relatorio+contexto: gemma4:26b" in saida
    assert "/tarefa" in saida                      # o que se perde
    assert "/contexto e /placar" not in saida      # esse nao se perdeu
    assert "MESMA conta" in saida                  # trocar um pelo outro nao serve
    assert "Locais que ja tens: gemma4:26b" in saida


def test_o_doctor_nao_incomoda_um_modelo_local_que_esta_na_lista(tmp_path, monkeypatch, capsys):
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq, "BD", tmp_path / "orq.sqlite")
    monkeypatch.setattr(orq, "PROJETO", str(tmp_path / "nao_existe"))
    for nome in ("MODELO_PESQUISA", "MODELO_DESENVOLVIMENTO",
                 "MODELO_RELATORIO", "MODELO_CONTEXTO"):
        monkeypatch.setattr(orq, nome, "qwen2.5-coder:7b")
    monkeypatch.setattr(orq, "procurar_web",
                        lambda *a, **k: (_ for _ in ()).throw(orq.ErroWeb("sem rede")))
    monkeypatch.setattr(orq.Ollama, "modelos", lambda self: ["qwen2.5-coder:7b"])
    monkeypatch.setattr(orq.Ollama, "conversar",
                        lambda *a, **k: pytest.fail("nao devia chamar um modelo local"))
    orq.doctor()
    assert "qwen2.5-coder:7b" in capsys.readouterr().out
