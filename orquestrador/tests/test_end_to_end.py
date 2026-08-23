"""O ciclo completo: tarefa no Telegram -> ensaios -> gate -> aprovacao -> ramo git.

Corre o backtest a serio (o falso, mas por subprocesso, em worktree real, com
git a serio). So o LLM e substituido — porque a alternativa era exigir uma GPU
para correr os testes.
"""
import json
import subprocess

import pytest

from orq.llm import FakeProvider
from orq.orchestrator import NullNotifier, Orchestrator
from orq.worker import Worker

# 3 hipoteses; o proponente escolhe sma_fast 20 (optimo), 2 e 45 (maus).
PESQUISA = json.dumps({"hipoteses": [
    {"nome": "Media no optimo", "raciocinio": "20 parece o ponto certo",
     "parametros_alvo": ["sma_fast"], "direcao": "aumentar"},
    {"nome": "Media muito curta", "raciocinio": "testar o extremo baixo",
     "parametros_alvo": ["sma_fast"], "direcao": "diminuir"},
    {"nome": "Media muito longa", "raciocinio": "testar o extremo alto",
     "parametros_alvo": ["sma_fast"], "direcao": "aumentar"},
]})


def _proposta(sma_fast):
    return json.dumps({
        "params": {"sma_fast": sma_fast, "sma_slow": 60, "stop_atr": 2.0},
        "justificacao": "teste",
    })


@pytest.fixture
def sistema(config, store):
    provider = FakeProvider([
        PESQUISA, _proposta(20), _proposta(2), _proposta(45),
        *["Resultado consistente com a hipotese."] * 6,
    ])
    notifier = NullNotifier()
    orq = Orchestrator(config, store, provider, notifier)
    return orq, store, notifier, config


def _com_baseline(orq, store, config):
    """Fluxo correto: medir a referencia antes de comparar seja o que for."""
    estudo = orq.ensure_study("objetivo do estudo")
    params = json.loads((config.target.path / "params.json").read_text())
    orq.measure_baseline(params, estudo["id"])
    return estudo


def _correr_tudo(worker, limite=20):
    passos = 0
    while worker.step() and passos < limite:
        passos += 1
    return passos


def test_ciclo_completo(sistema):
    orq, store, notifier, config = sistema
    worker = Worker(orq, store, idle_sleep=0)
    _com_baseline(orq, store, config)

    store.enqueue_task(42, "encontrar a media que maximiza o Sharpe")
    _correr_tudo(worker)

    estudo = store.open_study()
    assert estudo is not None
    ensaios = store.list_experiments(estudo["id"], limit=10)
    assert len(ensaios) == 3
    assert all(e["status"] == "done" for e in ensaios), [e["error"] for e in ensaios]

    # O ensaio no optimo (sma_fast=20) tem de ganhar aos extremos.
    por_sma = {json.loads(e["params"])["sma_fast"]: e for e in ensaios}
    sharpe = lambda e: json.loads(e["metrics"])["validation"]["sharpe_anualizado"]
    assert sharpe(por_sma[20]) > sharpe(por_sma[2])
    assert sharpe(por_sma[20]) > sharpe(por_sma[45])

    # O optimo tem de chegar a ti; o claramente mau nunca pode chegar.
    # (Nao afirmo "exatamente uma aprovacao": com 600 observacoes uma
    # configuracao de sinal fraco bate a baseline por acaso com frequencia
    # suficiente, e isso e comportamento realista, nao um defeito. E para
    # esse caso que existe o DSR — que aperta a medida que os ensaios se
    # acumulam.)
    aprovados = {exp for exp, _ in notifier.approvals}
    assert por_sma[20]["id"] in aprovados, "o melhor ensaio tem de chegar a ti"
    assert por_sma[45]["id"] not in aprovados, "um ensaio com Sharpe negativo nunca pode passar"

    mensagem = next(m for exp, m in notifier.approvals if exp == por_sma[20]["id"])
    assert "sma_fast: 20" in mensagem
    assert "holdout NAO foi tocado" in mensagem


def test_aprovacao_cria_ramo_e_nao_faz_merge(sistema):
    orq, store, notifier, config = sistema
    worker = Worker(orq, store, idle_sleep=0)
    _com_baseline(orq, store, config)
    store.enqueue_task(42, "otimizar")
    _correr_tudo(worker)

    exp_id = next(exp for exp, _ in notifier.approvals)
    ramo_antes = subprocess.run(
        ["git", "-C", str(config.target.path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    store.set_approval(exp_id, "approved")
    ramo = orq.apply_approved(exp_id)

    ramos = subprocess.run(
        ["git", "-C", str(config.target.path), "branch", "--list", ramo],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ramo in ramos, "o ramo devia ter ficado no repositorio"

    ramo_depois = subprocess.run(
        ["git", "-C", str(config.target.path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ramo_depois == ramo_antes, "o ramo ativo nunca pode ser trocado"

    # Os parametros vivos continuam intactos ate tu fazeres merge.
    vivos = json.loads((config.target.path / "params.json").read_text())
    assert vivos["sma_fast"] == 8

    # E estao no ramo novo, a espera.
    no_ramo = subprocess.run(
        ["git", "-C", str(config.target.path), "show", f"{ramo}:params.json"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert json.loads(no_ramo)["sma_fast"] == 20


def test_holdout_intocado_ate_ordem_expressa(sistema):
    orq, store, notifier, config = sistema
    worker = Worker(orq, store, idle_sleep=0)
    _com_baseline(orq, store, config)
    store.enqueue_task(42, "otimizar")
    _correr_tudo(worker)

    for e in store.list_experiments(store.open_study()["id"], limit=10):
        assert e["holdout"] is None, "nenhum ensaio automatico pode ter tocado no holdout"
        janelas = [json.loads(e["metrics"])[j] for j in ("train", "validation")]
        assert all(j["n_obs"] > 0 for j in janelas)

    exp_id = next(exp for exp, _ in notifier.approvals)
    metricas = orq.run_holdout(exp_id)
    assert metricas.trades == 420
    assert store.get_experiment(exp_id)["holdout"] is not None


def test_holdout_recusa_segunda_corrida(sistema):
    orq, store, notifier, config = sistema
    worker = Worker(orq, store, idle_sleep=0)
    _com_baseline(orq, store, config)
    store.enqueue_task(42, "otimizar")
    _correr_tudo(worker)

    exp_id = next(exp for exp, _ in notifier.approvals)
    orq.run_holdout(exp_id)
    with pytest.raises(ValueError, match="ja foi corrido"):
        orq.run_holdout(exp_id)


def test_orcamento_de_ensaios_fecha_o_estudo(sistema):
    orq, store, notifier, config = sistema
    estudo = orq.ensure_study("objetivo")
    for _ in range(config.protocol.max_trials_per_study):
        exp = store.enqueue_experiment(estudo["id"], {"sma_fast": 20})
        store.finish_experiment(exp, status="done")

    task = {"id": "t1", "text": "continua a otimizar", "chat_id": 42}
    assert orq.handle_task(task) == 0
    assert store.get_study(estudo["id"])["status"] == "closed"
    assert any("multiple testing" in m for m in notifier.sent)


def test_baseline_serve_de_referencia(sistema):
    orq, store, notifier, config = sistema
    estudo = orq.ensure_study("objetivo")
    params = json.loads((config.target.path / "params.json").read_text())
    metricas = orq.measure_baseline(params, estudo["id"])
    assert metricas.trades == 420
    assert store.get_study(estudo["id"])["baseline"] is not None


def test_worker_sobrevive_a_ensaio_que_rebenta(sistema):
    """Um ensaio mau nao pode derrubar o worker: a fila tem de continuar."""
    orq, store, notifier, config = sistema
    worker = Worker(orq, store, idle_sleep=0)
    estudo = orq.ensure_study("objetivo")
    mau = store.enqueue_experiment(estudo["id"], {"sma_fast": 20})
    store._conn.execute("UPDATE experiments SET params='json partido' WHERE id=?", (mau,))
    bom = store.enqueue_experiment(estudo["id"], {"sma_fast": 20})

    _correr_tudo(worker)

    assert store.get_experiment(mau)["status"] == "failed"
    assert store.get_experiment(bom)["status"] == "done"


def test_falha_do_llm_nao_para_o_estudo(config, store):
    """Com o modelo a devolver lixo, o proponente cai na amostragem."""
    provider = FakeProvider([PESQUISA] + ["lixo"] * 30)
    notifier = NullNotifier()
    orq = Orchestrator(config, store, provider, notifier)
    worker = Worker(orq, store, idle_sleep=0)

    store.enqueue_task(42, "otimizar")
    _correr_tudo(worker)

    ensaios = store.list_experiments(store.open_study()["id"], limit=10)
    assert len(ensaios) == 3
    assert all(e["status"] == "done" for e in ensaios)
    eventos = [e["kind"] for e in store.recent_events(100)]
    assert "proposer.fallback" in eventos
