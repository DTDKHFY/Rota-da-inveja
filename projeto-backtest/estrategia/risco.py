"""Dimensionamento de posicao.  ← O AGENTE PODE ALTERAR ESTE FICHEIRO

Contrato:

    tamanho_posicao(params) -> float

Devolve o multiplicador aplicado a cada trade. 1.0 = posicao cheia.
Manter isto separado do sinal permite testar "a entrada esta certa mas o
tamanho esta errado" sem mexer na logica de entrada.
"""
from __future__ import annotations


def tamanho_posicao(params: dict) -> float:
    """Fracao do capital por trade, limitada a um intervalo defensavel.

    O limite superior nao e decorativo: sem ele, a forma mais rapida de
    aumentar o retorno de um backtest e aumentar a alavancagem, e o otimizador
    encontra isso antes de encontrar qualquer edge real.
    """
    risco = float(params.get("risco_por_trade", 0.01))
    return max(0.1, min(risco * 100.0, 2.0))
