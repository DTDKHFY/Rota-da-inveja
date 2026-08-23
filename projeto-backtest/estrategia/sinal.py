"""Geracao de sinal.  ← O AGENTE PODE ALTERAR ESTE FICHEIRO

Contrato (nao mudes a assinatura):

    gerar_sinais(barras, params) -> list[int]

    barras : lista de Barra (data, abertura, maxima, minima, fecho, volume)
    params : dicionario com os parametros de params.json
    retorna: uma lista do MESMO tamanho que `barras`, com um valor por barra:
              1 = comprado,  0 = fora,  -1 = vendido

Regra de ouro: o sinal na posicao i so pode usar informacao ate barras[i].
Olhar para barras[i+1] e lookahead — o arnes desfasa uma barra na execucao, o
que apanha o caso obvio, mas nao te salva se calculares uma media com dados do
futuro. Nao o faças.

A estrategia abaixo e um cruzamento de medias, deliberadamente simples. Serve
de ponto de partida, nao de estrategia rentavel.
"""
from __future__ import annotations

from typing import Sequence


def media_movel(valores: Sequence[float], periodo: int) -> list[float | None]:
    """Media movel simples. `None` enquanto nao houver historico suficiente.

    Devolver None em vez de zero e importante: um zero seria interpretado como
    um preco valido e geraria cruzamentos falsos no arranque da serie.
    """
    saida: list[float | None] = []
    soma = 0.0
    for i, v in enumerate(valores):
        soma += v
        if i >= periodo:
            soma -= valores[i - periodo]
        saida.append(soma / periodo if i >= periodo - 1 else None)
    return saida


def amplitude_media(barras, periodo: int) -> list[float | None]:
    """ATR simplificado: media da amplitude verdadeira das ultimas `periodo` barras."""
    amplitudes: list[float] = []
    for i, b in enumerate(barras):
        if i == 0:
            amplitudes.append(b.maxima - b.minima)
            continue
        fecho_ant = barras[i - 1].fecho
        amplitudes.append(max(
            b.maxima - b.minima,
            abs(b.maxima - fecho_ant),
            abs(b.minima - fecho_ant),
        ))
    return media_movel(amplitudes, periodo)


def gerar_sinais(barras, params: dict) -> list[int]:
    """Cruzamento de medias com filtro de volatilidade.

    Compra quando a media rapida passa acima da lenta, sai quando passa abaixo.
    O filtro de volatilidade evita entrar quando o mercado esta parado, onde os
    custos comem o pouco movimento que ha.
    """
    rapida = int(params.get("sma_fast", 20))
    lenta = int(params.get("sma_slow", 60))
    filtro_vol = float(params.get("filtro_volatilidade", 0.0))

    if rapida >= lenta:
        # Medias invertidas nao geram cruzamentos uteis. Ficar de fora e mais
        # honesto do que produzir um resultado sem significado.
        return [0] * len(barras)

    fechos = [b.fecho for b in barras]
    mm_rapida = media_movel(fechos, rapida)
    mm_lenta = media_movel(fechos, lenta)
    atr = amplitude_media(barras, lenta)

    sinais: list[int] = []
    posicao = 0
    for i in range(len(barras)):
        r, l, a = mm_rapida[i], mm_lenta[i], atr[i]
        if r is None or l is None:
            sinais.append(0)
            continue

        if filtro_vol > 0 and a is not None and fechos[i] > 0:
            if (a / fechos[i]) < filtro_vol:
                sinais.append(0)      # mercado parado: fica de fora
                continue

        if r > l:
            posicao = 1
        elif r < l:
            posicao = 0
        sinais.append(posicao)

    return sinais
