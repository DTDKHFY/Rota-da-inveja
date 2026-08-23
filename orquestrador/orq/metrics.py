"""Metricas de backtest e correcao de multiple testing.

Nada aqui chama um modelo. Sao contas — e e de proposito: quem decide se um
ensaio prestou tem de ser codigo deterministico, senao estamos a pedir a um LLM
que julgue um numero que ele proprio ajudou a produzir.

A peca central e o Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014). A
ideia: se testaste 200 configuracoes e ficaste com a melhor, o Sharpe dessa
melhor esta inflacionado so por teres testado 200. O DSR pergunta "qual a
probabilidade de este Sharpe ser real, dado que foi o melhor de N tentativas".
Sem isto, o numero que o agente te manda no Telegram nao quer dizer nada.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Sequence

EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()

# Erro de virgula flutuante acumulado numa soma de quadrados nao da exatamente
# zero para uma serie constante — da ~1e-18. Comparar `sigma == 0` deixa passar
# esse residuo e a divisao seguinte devolve um Sharpe de 1e15.
#
# Nao e um caso academico: uma configuracao que nao abre trades nenhuns produz
# retornos constantes, e sem esta guarda seria a configuracao com melhor Sharpe
# de todo o estudo. Um otimizador automatico encontra exatamente este tipo de
# buraco, e depois o gate deixa passar.
_EPSILON_RELATIVO = 1e-12


class MetricsError(ValueError):
    """Metricas do projeto-alvo mal formadas."""


# --------------------------------------------------------------------------
# Estatistica basica
# --------------------------------------------------------------------------

def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float], ddof: int = 1) -> float:
    n = len(values)
    if n - ddof <= 0:
        return 0.0
    mu = mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (n - ddof))


def _desvio_degenerado(sigma: float, values: Sequence[float]) -> bool:
    """True se o desvio-padrao for indistinguivel de zero a precisao do float."""
    escala = max((abs(v) for v in values), default=0.0)
    return sigma <= max(escala, 1.0) * _EPSILON_RELATIVO


def skewness(values: Sequence[float]) -> float:
    """Assimetria populacional. Entra na formula do PSR."""
    n = len(values)
    sigma = stdev(values, ddof=0)
    if n < 3 or _desvio_degenerado(sigma, values):
        return 0.0
    mu = mean(values)
    return sum(((v - mu) / sigma) ** 3 for v in values) / n


def kurtosis(values: Sequence[float]) -> float:
    """Curtose nao-excedente (normal = 3), que e a convencao da formula do PSR."""
    n = len(values)
    sigma = stdev(values, ddof=0)
    if n < 4 or _desvio_degenerado(sigma, values):
        return 3.0
    mu = mean(values)
    return sum(((v - mu) / sigma) ** 4 for v in values) / n


def sharpe_ratio(returns: Sequence[float], periods_per_year: int | None = None) -> float:
    """Sharpe por periodo, ou anualizado se `periods_per_year` for dado."""
    sigma = stdev(returns)
    if _desvio_degenerado(sigma, returns):
        # Sem variacao nao ha risco medido, e sem risco o Sharpe nao esta
        # definido. Zero e a resposta honesta; qualquer outra coisa e um numero
        # enorme que nao significa nada.
        return 0.0
    ratio = mean(returns) / sigma
    if periods_per_year:
        ratio *= math.sqrt(periods_per_year)
    return ratio


def max_drawdown(equity: Sequence[float]) -> float:
    """Queda maxima de pico a vale, em fracao (0.25 = -25%)."""
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def equity_from_returns(returns: Sequence[float], start: float = 1.0) -> list[float]:
    equity = [start]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    return equity


# --------------------------------------------------------------------------
# Probabilistic / Deflated Sharpe Ratio
# --------------------------------------------------------------------------

def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    benchmark_sharpe: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """P(Sharpe verdadeiro > benchmark), corrigido por assimetria e caudas.

    `observed_sharpe` e `benchmark_sharpe` tem de estar na MESMA unidade e
    nao-anualizados. Misturar Sharpe anualizado com nao-anualizado aqui e o erro
    classico e da um numero bonito e errado.
    """
    if n_obs < 2:
        return 0.0
    denominator_sq = (
        1.0 - skew * observed_sharpe + ((kurt - 1.0) / 4.0) * observed_sharpe**2
    )
    if denominator_sq <= 0:
        return 0.0
    z = ((observed_sharpe - benchmark_sharpe) * math.sqrt(n_obs - 1)) / math.sqrt(
        denominator_sq
    )
    return _NORMAL.cdf(z)


def expected_max_sharpe(n_trials: int, trials_sharpe_variance: float) -> float:
    """Sharpe que esperarias obter por puro acaso, testando N estrategias sem valor.

    Este e o patamar que o teu melhor ensaio tem de bater para significar
    alguma coisa. Cresce com o numero de tentativas — e por isso que "testei
    500 combinacoes e a melhor deu Sharpe 2" e uma frase quase vazia.
    """
    if n_trials < 2 or trials_sharpe_variance <= 0:
        return 0.0
    sigma = math.sqrt(trials_sharpe_variance)
    term_a = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    term_b = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sigma * ((1.0 - EULER_MASCHERONI) * term_a + EULER_MASCHERONI * term_b)


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_obs: int,
    n_trials: int,
    trials_sharpe_variance: float,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """PSR medido contra o maximo esperado por acaso em N tentativas.

    Devolve uma probabilidade. >= 0.95 e o limiar habitual para dizer "isto
    provavelmente nao e ruido".
    """
    benchmark = expected_max_sharpe(n_trials, trials_sharpe_variance)
    return probabilistic_sharpe_ratio(observed_sharpe, benchmark, n_obs, skew, kurt)


# --------------------------------------------------------------------------
# Contrato com o projeto-alvo
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowMetrics:
    """Metricas de uma janela (treino ou validacao)."""

    sharpe: float
    max_drawdown: float
    trades: int
    n_obs: int
    skew: float = 0.0
    kurt: float = 3.0
    total_return: float | None = None
    periods_per_year: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def sharpe_annualised(self) -> float:
        if self.periods_per_year:
            return self.sharpe * math.sqrt(self.periods_per_year)
        return self.sharpe


def parse_window_metrics(raw: dict) -> WindowMetrics:
    """Le o JSON que o teu backtest escreveu.

    Aceita duas formas. Se deres `returns` (lista de retornos por periodo),
    calculo tudo daqui — e a preferivel, porque o DSR precisa de assimetria,
    curtose e numero de observacoes, e esses so saem da serie. Se deres so
    `sharpe` e `trades`, funciona na mesma, mas o DSR fica menos fiavel e eu
    aviso-te no relatorio.
    """
    if not isinstance(raw, dict):
        raise MetricsError("as metricas tem de ser um objeto JSON")

    returns = raw.get("returns") or []
    if returns and not all(isinstance(r, (int, float)) for r in returns):
        raise MetricsError("`returns` tem de ser uma lista de numeros")
    periods = raw.get("periods_per_year")

    if returns:
        sharpe = sharpe_ratio(returns)  # por periodo, nao anualizado
        n_obs = len(returns)
        skew = skewness(returns)
        kurt = kurtosis(returns)
        drawdown = raw.get("max_drawdown")
        if drawdown is None:
            drawdown = max_drawdown(equity_from_returns(returns))
        total = raw.get("total_return")
        if total is None:
            total = math.prod(1.0 + r for r in returns) - 1.0
    else:
        if "sharpe" not in raw:
            raise MetricsError(
                "faltam `returns` e `sharpe`: preciso de pelo menos um dos dois. "
                "Manda `returns` se puderes — sem a serie nao consigo calcular o "
                "Deflated Sharpe com rigor."
            )
        sharpe = float(raw["sharpe"])
        if periods:
            sharpe /= math.sqrt(periods)  # normaliza para por-periodo
        n_obs = int(raw.get("n_obs", 0))
        skew = float(raw.get("skew", 0.0))
        kurt = float(raw.get("kurtosis", 3.0))
        drawdown = raw.get("max_drawdown", 0.0)
        total = raw.get("total_return")

    if "trades" not in raw:
        raise MetricsError("falta `trades` nas metricas: sem numero de trades nao ha gate")

    return WindowMetrics(
        sharpe=sharpe,
        max_drawdown=float(drawdown or 0.0),
        trades=int(raw["trades"]),
        n_obs=n_obs,
        skew=skew,
        kurt=kurt,
        total_return=None if total is None else float(total),
        periods_per_year=int(periods) if periods else None,
        raw=raw,
    )
