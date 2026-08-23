"""O gate: decide, em codigo, se um ensaio merece chegar a ti.

Este modulo e deliberadamente burro e deliberadamente rigido. Nao ha LLM aqui.
Um ensaio so passa se cumprir TODOS os criterios; nao ha media ponderada nem
"quase la". A razao e simples: o proposito do gate e nao te mandar ruido, e um
criterio flexivel deixa passar ruido sempre que o ruido for simpatico.

O criterio mais importante nao e o Sharpe — e o gap entre treino e validacao.
Uma estrategia que brilha no treino e desaparece na validacao nao esta "quase
boa"; esta a decorar o passado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import GateConfig
from .metrics import WindowMetrics, deflated_sharpe_ratio, stdev


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    value: float
    threshold: float
    detail: str

    @property
    def icon(self) -> str:
        return "✅" if self.passed else "❌"

    def line(self) -> str:
        return f"{self.icon} {self.detail}"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    dsr: float = 0.0
    n_trials: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "dsr": self.dsr,
            "n_trials": self.n_trials,
            "warnings": self.warnings,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "value": c.value,
                    "threshold": c.threshold,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }

    def summary(self) -> str:
        head = "PASSOU no gate" if self.passed else "CHUMBOU no gate"
        lines = [f"{head} ({len(self.checks) - len(self.failures)}/{len(self.checks)} criterios)"]
        lines += [c.line() for c in self.checks]
        lines += [f"⚠️ {w}" for w in self.warnings]
        return "\n".join(lines)


def trials_sharpe_variance(sharpes: list[float]) -> float:
    """Variancia dos Sharpes ja observados no estudo.

    Entra no DSR como medida de quanta dispersao ha entre tentativas. Com menos
    de dois ensaios nao ha dispersao para medir e devolvo 0 — o DSR degenera
    para o PSR simples, o que e o comportamento honesto quando ainda nao ha
    historico.
    """
    return stdev(sharpes, ddof=1) ** 2 if len(sharpes) >= 2 else 0.0


def evaluate(
    *,
    train: WindowMetrics,
    validation: WindowMetrics,
    config: GateConfig,
    n_trials: int,
    prior_sharpes: list[float],
    baseline: WindowMetrics | None = None,
) -> Verdict:
    """Aplica todos os criterios. `validation` e a janela out-of-sample."""
    checks: list[Check] = []
    warnings: list[str] = []

    variance = trials_sharpe_variance(prior_sharpes)
    dsr = deflated_sharpe_ratio(
        observed_sharpe=validation.sharpe,
        n_obs=validation.n_obs,
        n_trials=max(n_trials, 1),
        trials_sharpe_variance=variance,
        skew=validation.skew,
        kurt=validation.kurt,
    )

    if validation.n_obs == 0:
        warnings.append(
            "o backtest nao devolveu `returns`, so o Sharpe agregado. O DSR "
            "abaixo e uma estimativa fraca — manda a serie de retornos para ter "
            "um numero em que se possa confiar."
        )
    if len(prior_sharpes) < 2:
        warnings.append(
            "menos de 2 ensaios anteriores neste estudo: ainda nao ha dispersao "
            "para deflacionar. O DSR vai parecer otimista nos primeiros ensaios."
        )

    # 1. Volume de trades. Sharpe alto com 12 trades e uma anedota, nao um edge.
    checks.append(
        Check(
            name="trades",
            passed=validation.trades >= config.min_trades,
            value=float(validation.trades),
            threshold=float(config.min_trades),
            detail=(
                f"trades na validacao: {validation.trades} "
                f"(minimo {config.min_trades})"
            ),
        )
    )

    # 2. Sharpe out-of-sample.
    oos_annual = validation.sharpe_annualised
    checks.append(
        Check(
            name="oos_sharpe",
            passed=oos_annual >= config.min_oos_sharpe,
            value=oos_annual,
            threshold=config.min_oos_sharpe,
            detail=f"Sharpe out-of-sample: {oos_annual:.2f} (minimo {config.min_oos_sharpe:.2f})",
        )
    )

    # 3. Drawdown out-of-sample.
    checks.append(
        Check(
            name="oos_drawdown",
            passed=validation.max_drawdown <= config.max_oos_drawdown,
            value=validation.max_drawdown,
            threshold=config.max_oos_drawdown,
            detail=(
                f"drawdown out-of-sample: {validation.max_drawdown * 100:.1f}% "
                f"(maximo {config.max_oos_drawdown * 100:.1f}%)"
            ),
        )
    )

    # 4. Deflated Sharpe: o Sharpe sobrevive a ter sido o melhor de N tentativas?
    checks.append(
        Check(
            name="dsr",
            passed=dsr >= config.min_dsr,
            value=dsr,
            threshold=config.min_dsr,
            detail=(
                f"Deflated Sharpe: {dsr:.3f} apos {n_trials} ensaios "
                f"(minimo {config.min_dsr:.2f})"
            ),
        )
    )

    # 5. Gap treino/validacao: o sinal de overfit mais directo que existe.
    gap = train.sharpe_annualised - oos_annual
    checks.append(
        Check(
            name="is_oos_gap",
            passed=gap <= config.max_is_oos_sharpe_gap,
            value=gap,
            threshold=config.max_is_oos_sharpe_gap,
            detail=(
                f"queda treino->validacao: {gap:+.2f} de Sharpe "
                f"({train.sharpe_annualised:.2f} -> {oos_annual:.2f}, "
                f"maximo tolerado {config.max_is_oos_sharpe_gap:.2f})"
            ),
        )
    )

    # 6. Bater a baseline. Sem isto o sistema aceitaria mexer por mexer.
    if baseline is not None:
        base = baseline.sharpe_annualised
        if abs(base) < 1e-9:
            improvement = 100.0 if oos_annual > 0 else 0.0
        else:
            improvement = (oos_annual - base) / abs(base) * 100.0
        checks.append(
            Check(
                name="improvement",
                passed=improvement >= config.require_oos_improvement_pct,
                value=improvement,
                threshold=config.require_oos_improvement_pct,
                detail=(
                    f"melhoria sobre a baseline: {improvement:+.1f}% "
                    f"({base:.2f} -> {oos_annual:.2f}, minimo "
                    f"{config.require_oos_improvement_pct:+.1f}%)"
                ),
            )
        )
    else:
        warnings.append(
            "sem baseline definida: nao da para dizer se isto melhora alguma "
            "coisa. Corre /baseline antes de aceitar qualquer proposta."
        )

    return Verdict(
        passed=all(c.passed for c in checks),
        checks=checks,
        dsr=dsr,
        n_trials=n_trials,
        warnings=warnings,
    )
