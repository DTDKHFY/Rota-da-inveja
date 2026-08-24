import random

import pytest

from orq.config import GateConfig
from orq.gate import evaluate, trials_sharpe_variance
from orq.metrics import parse_window_metrics


def janela(mu, *, n=1260, trades=300, dd=0.15, semente=1):
    rng = random.Random(semente)
    return parse_window_metrics({
        "returns": [rng.gauss(mu, 0.01) for _ in range(n)],
        "trades": trades, "max_drawdown": dd, "periods_per_year": 252,
    })


@pytest.fixture
def cfg():
    return GateConfig()


def test_ensaio_solido_passa(cfg):
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2), config=cfg,
                 n_trials=3, prior_sharpes=[0.05, 0.07, 0.06], baseline=janela(0.0003, semente=3))
    assert v.passed, v.summary()


def test_o_mesmo_resultado_chumba_depois_de_muitos_ensaios(cfg):
    """O criterio que a aprovacao humana nunca conseguiria aplicar."""
    train, val, base = janela(0.0008), janela(0.0007, semente=2), janela(0.0003, semente=3)
    anteriores = [random.Random(i).gauss(0.05, 0.02) for i in range(50)]

    poucos = evaluate(train=train, validation=val, config=cfg, n_trials=3,
                      prior_sharpes=anteriores[:3], baseline=base)
    muitos = evaluate(train=train, validation=val, config=cfg, n_trials=500,
                      prior_sharpes=anteriores, baseline=base)

    assert poucos.passed
    assert not muitos.passed
    assert [c.name for c in muitos.failures] == ["dsr"]


def test_overfit_e_apanhado_pelo_gap(cfg):
    v = evaluate(train=janela(0.0030), validation=janela(0.0001, semente=2), config=cfg,
                 n_trials=5, prior_sharpes=[0.05, 0.06], baseline=janela(0.0003, semente=3))
    assert not v.passed
    assert "is_oos_gap" in [c.name for c in v.failures]


def test_poucos_trades_chumbam(cfg):
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2, trades=12),
                 config=cfg, n_trials=2, prior_sharpes=[0.05], baseline=janela(0.0003, semente=3))
    assert "trades" in [c.name for c in v.failures]


def test_drawdown_excessivo_chumba(cfg):
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2, dd=0.60),
                 config=cfg, n_trials=2, prior_sharpes=[0.05], baseline=janela(0.0003, semente=3))
    assert "oos_drawdown" in [c.name for c in v.failures]


def test_sem_melhoria_sobre_a_baseline_chumba(cfg):
    igual = janela(0.0007, semente=2)
    v = evaluate(train=janela(0.0008), validation=igual, config=cfg, n_trials=2,
                 prior_sharpes=[0.05], baseline=igual)
    assert "improvement" in [c.name for c in v.failures]


def test_sem_baseline_avisa(cfg):
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2), config=cfg,
                 n_trials=2, prior_sharpes=[0.05], baseline=None)
    assert any("baseline" in a for a in v.warnings)
    assert "improvement" not in [c.name for c in v.checks]


def test_avisa_quando_faltam_retornos(cfg):
    sem_serie = parse_window_metrics(
        {"sharpe": 1.5, "trades": 300, "max_drawdown": 0.1, "periods_per_year": 252}
    )
    v = evaluate(train=sem_serie, validation=sem_serie, config=cfg, n_trials=2,
                 prior_sharpes=[0.05], baseline=None)
    assert any("returns" in a for a in v.warnings)


def test_variancia_dos_ensaios():
    assert trials_sharpe_variance([]) == 0.0
    assert trials_sharpe_variance([0.5]) == 0.0
    assert trials_sharpe_variance([0.1, 0.2, 0.3]) == pytest.approx(0.01)


def test_verdict_serializa(cfg):
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2), config=cfg,
                 n_trials=3, prior_sharpes=[0.05], baseline=janela(0.0003, semente=3))
    d = v.to_dict()
    assert set(d) == {"passed", "dsr", "n_trials", "warnings", "checks"}
    assert all(set(c) == {"name", "passed", "value", "threshold", "detail"} for c in d["checks"])


def test_gate_exige_todos_os_criterios(cfg):
    """Sem media ponderada: um criterio falhado chumba o ensaio."""
    v = evaluate(train=janela(0.0008), validation=janela(0.0007, semente=2, trades=5),
                 config=cfg, n_trials=2, prior_sharpes=[0.05], baseline=janela(0.0003, semente=3))
    assert not v.passed
    assert sum(1 for c in v.checks if c.passed) >= 4, "so um criterio devia ter falhado"
