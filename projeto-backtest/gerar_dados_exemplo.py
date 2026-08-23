#!/usr/bin/env python3
"""Gera uma serie sintetica, para poderes correr tudo antes de teres dados reais.

Aviso que vale a pena levar a serio: isto e ruido com uma tendencia suave por
cima. NAO e um mercado. Serve para confirmar que a maquinaria funciona — que o
backtest corre, que as metricas saem, que o orquestrador liga. Qualquer
resultado que obtenhas aqui nao diz absolutamente nada sobre trading.

    python3 gerar_dados_exemplo.py
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

DESTINO = Path(__file__).resolve().parent / "dados" / "serie.csv"
INICIO = date(2015, 1, 1)
FIM = date(2025, 12, 31)
SEMENTE = 42


def gerar() -> list[dict]:
    rng = random.Random(SEMENTE)
    linhas: list[dict] = []
    preco = 100.0
    dia = INICIO
    # Regimes alternados, para a serie nao ser uma unica tendencia limpa onde
    # qualquer cruzamento de medias parece genial.
    regime, restante = 1, 0
    while dia <= FIM:
        if dia.weekday() < 5:                    # so dias uteis
            if restante <= 0:
                regime = rng.choice([1, -1, 0])
                restante = rng.randint(40, 200)
            restante -= 1
            deriva = 0.0004 * regime
            preco *= 1 + rng.gauss(deriva, 0.011)
            preco = max(preco, 1.0)
            amplitude = preco * abs(rng.gauss(0, 0.006))
            abertura = preco * (1 + rng.gauss(0, 0.002))
            linhas.append({
                "data": dia.isoformat(),
                "abertura": round(abertura, 4),
                "maxima": round(max(abertura, preco) + amplitude, 4),
                "minima": round(min(abertura, preco) - amplitude, 4),
                "fecho": round(preco, 4),
                "volume": rng.randint(100_000, 900_000),
            })
        dia += timedelta(days=1)
    return linhas


def main() -> None:
    linhas = gerar()
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    with DESTINO.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=list(linhas[0]))
        escritor.writeheader()
        escritor.writerows(linhas)
    print(f"{len(linhas)} barras de {linhas[0]['data']} a {linhas[-1]['data']} "
          f"-> {DESTINO}")
    print("\nLembra-te: isto e ruido sintetico. Serve para testar a maquinaria, "
          "nao para tirar conclusoes.")


if __name__ == "__main__":
    main()
