"""As propriedades do DSR que sustentam todo o gate."""
import random

import pytest

from orq.metrics import (
    MetricsError, deflated_sharpe_ratio, expected_max_sharpe, kurtosis,
    max_drawdown, parse_window_metrics, probabilistic_sharpe_ratio,
    sharpe_ratio, skewness,
)


def serie(mu=0.0008, sigma=0.01, n=1260, semente=7):
    rng = random.Random(semente)
    return [rng.gauss(mu, sigma) for _ in range(n)]


def test_sharpe_anualiza_por_raiz_do_tempo():
    r = serie()
    assert sharpe_ratio(r, 252) == pytest.approx(sharpe_ratio(r) * 252**0.5)


def test_sharpe_de_serie_constante_e_zero():
    assert sharpe_ratio([0.01] * 100) == 0.0


def test_drawdown():
    assert max_drawdown([100, 120, 84, 90]) == pytest.approx(0.30)
    assert max_drawdown([100, 110, 120]) == 0.0
    assert max_drawdown([]) == 0.0


def test_mais_tentativas_exigem_sharpe_maior():
    """O nucleo da correcao de multiple testing."""
    anteriores = [expected_max_sharpe(n, 0.0004) for n in (2, 10, 50, 200, 1000)]
    assert anteriores == sorted(anteriores)
    assert expected_max_sharpe(1, 0.0004) == 0.0


def test_dsr_cai_quando_o_numero_de_ensaios_sobe():
    """Mesmo resultado + mais tentativas = menos credivel. E o ponto todo."""
    r = serie()
    sr, n = sharpe_ratio(r), len(r)
    valores = [
        deflated_sharpe_ratio(sr, n, ensaios, 0.0004, skewness(r), kurtosis(r))
        for ensaios in (1, 10, 100, 1000)
    ]
    assert valores == sorted(valores, reverse=True)
    assert valores[0] > 0.95, "com 1 ensaio este Sharpe devia ser credivel"
    assert valores[-1] < valores[0], "com 1000 ensaios ja nao devia ser"


def test_psr_entre_zero_e_um():
    r = serie()
    for benchmark in (-1.0, 0.0, 0.5):
        p = probabilistic_sharpe_ratio(sharpe_ratio(r), benchmark, len(r))
        assert 0.0 <= p <= 1.0


def test_psr_sem_observacoes_suficientes():
    assert probabilistic_sharpe_ratio(1.0, 0.0, 1) == 0.0


def test_parse_calcula_tudo_a_partir_dos_retornos():
    r = serie()
    m = parse_window_metrics({"returns": r, "trades": 300, "periods_per_year": 252})
    assert m.n_obs == len(r)
    assert m.trades == 300
    assert m.sharpe_annualised == pytest.approx(sharpe_ratio(r, 252))
    assert m.total_return is not None


def test_parse_normaliza_sharpe_anualizado_recebido():
    """Sem a serie, um Sharpe anualizado tem de voltar a por-periodo para o DSR."""
    m = parse_window_metrics(
        {"sharpe": 1.6, "trades": 300, "periods_per_year": 252, "max_drawdown": 0.2}
    )
    assert m.sharpe == pytest.approx(1.6 / 252**0.5)
    assert m.sharpe_annualised == pytest.approx(1.6)


def test_parse_exige_trades():
    with pytest.raises(MetricsError, match="trades"):
        parse_window_metrics({"returns": serie()})


def test_parse_exige_sharpe_ou_returns():
    with pytest.raises(MetricsError, match="returns"):
        parse_window_metrics({"trades": 100})


def test_parse_rejeita_returns_nao_numericos():
    with pytest.raises(MetricsError, match="lista de numeros"):
        parse_window_metrics({"returns": [0.1, "x"], "trades": 100})
