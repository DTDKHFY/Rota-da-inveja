"""Simulacao e metricas. ARNES — o agente nao pode alterar este ficheiro.

Este e o ficheiro que o orquestrador protege com mais cuidado, e a razao e
simples: e aqui que os numeros nascem. Um agente cuja tarefa e melhorar o
Sharpe tem um atalho obvio — mexer aqui. Por isso a estrategia so decide
SINAIS; quem os transforma em retornos, custos e metricas e este codigo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from dados import Barra


@dataclass(frozen=True)
class Resultado:
    retornos: list[float]
    trades: int
    drawdown_maximo: float
    retorno_total: float
    exposicao: float          # fracao do tempo com posicao aberta
    custo_total: float


def simular(barras: Sequence[Barra], sinais: Sequence[int], *,
            custo_por_trade: float = 0.0005, slippage: float = 0.0002,
            tamanho: float = 1.0) -> Resultado:
    """Transforma sinais em retornos.

    Duas decisoes que estao aqui de proposito e nao na estrategia:

    1. DESFASAMENTO DE UMA BARRA. O sinal calculado com o fecho da barra t so
       pode ser executado na barra t+1. Sem isto estarias a negociar com o
       preco de fecho que ainda nao conhecias, e o backtest daria resultados
       maravilhosos e impossiveis. E o erro mais comum em backtests caseiros, e
       fica fora do alcance do agente exatamente por isso.

    2. CUSTOS. Comissao e slippage aplicados a cada mudanca de posicao. Uma
       estrategia que negoceia todos os dias parece otima sem custos e morre com
       eles.
    """
    if len(sinais) != len(barras):
        raise ValueError(
            f"a estrategia devolveu {len(sinais)} sinais para {len(barras)} barras. "
            "Tem de devolver exatamente um sinal por barra."
        )

    retornos: list[float] = []
    trades = 0
    custo_total = 0.0
    barras_expostas = 0
    posicao = 0

    for i in range(1, len(barras)):
        # O sinal de ontem e a posicao de hoje. Nunca o sinal de hoje.
        alvo = sinais[i - 1]
        preco_ant, preco = barras[i - 1].fecho, barras[i].fecho
        if preco_ant <= 0:
            retornos.append(0.0)
            continue

        variacao = (preco - preco_ant) / preco_ant

        # A posicao e assumida ANTES de contar o retorno do intervalo. O sinal
        # foi calculado com o fecho da barra i-1, e o movimento de i-1 para i
        # ainda era futuro nesse momento — logo e legitimo captura-lo.
        # Atualizar a posicao depois daria um desfasamento de duas barras, que
        # nao corresponde a nenhuma forma real de negociar.
        custo = 0.0
        if alvo != posicao:
            trades += 1
            custo = (custo_por_trade + slippage) * abs(alvo - posicao) * tamanho
            custo_total += custo
            posicao = alvo

        bruto = posicao * variacao * tamanho

        if posicao != 0:
            barras_expostas += 1
        retornos.append(bruto - custo)

    equity = [1.0]
    for r in retornos:
        equity.append(equity[-1] * (1.0 + r))

    return Resultado(
        retornos=retornos,
        trades=trades,
        drawdown_maximo=_drawdown(equity),
        retorno_total=equity[-1] - 1.0,
        exposicao=barras_expostas / max(1, len(retornos)),
        custo_total=custo_total,
    )


def _drawdown(equity: Sequence[float]) -> float:
    pico, pior = equity[0], 0.0
    for x in equity:
        pico = max(pico, x)
        if pico > 0:
            pior = max(pior, (pico - x) / pico)
    return pior


def sharpe(retornos: Sequence[float], periodos_ano: int = 252) -> float:
    """Anualizado. So para o relatorio no ecra — quem manda no gate e o
    orquestrador, que recalcula a partir da serie `returns`."""
    n = len(retornos)
    if n < 2:
        return 0.0
    m = sum(retornos) / n
    var = sum((r - m) ** 2 for r in retornos) / (n - 1)
    if var <= 0:
        return 0.0
    return (m / math.sqrt(var)) * math.sqrt(periodos_ano)


def para_json(resultado: Resultado, periodos_ano: int = 252) -> dict:
    """O contrato com o orquestrador.

    `returns` e o campo que importa: com a serie o orquestrador calcula
    assimetria, curtose e numero de observacoes, e o Deflated Sharpe fica
    fiavel. Sem ela seria um palpite.
    """
    return {
        "returns": resultado.retornos,
        "trades": resultado.trades,
        "max_drawdown": resultado.drawdown_maximo,
        "total_return": resultado.retorno_total,
        "periods_per_year": periodos_ano,
        # informativo, nao usado pelo gate
        "sharpe": sharpe(resultado.retornos, periodos_ano),
        "exposicao": resultado.exposicao,
        "custo_total": resultado.custo_total,
    }
