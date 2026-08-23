"""Carregamento de dados. ARNES — o agente nao pode alterar este ficheiro.

Le CSV com uma barra por linha. Aceita nomes de coluna em portugues ou ingles,
porque toda a gente exporta de um sitio diferente.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

# Cada campo aceita varios nomes. O primeiro que aparecer no ficheiro ganha.
COLUNAS = {
    "data":     ("data", "date", "datetime", "timestamp", "time"),
    "abertura": ("abertura", "open", "o"),
    "maxima":   ("maxima", "máxima", "high", "h"),
    "minima":   ("minima", "mínima", "low", "l"),
    "fecho":    ("fecho", "fechamento", "close", "c", "adj close", "adj_close"),
    "volume":   ("volume", "vol", "v"),
}


@dataclass(frozen=True)
class Barra:
    data: str
    abertura: float
    maxima: float
    minima: float
    fecho: float
    volume: float


class ErroDados(Exception):
    pass


def _mapear(cabecalho: list[str]) -> dict[str, int]:
    normalizado = [c.strip().lower().lstrip("﻿") for c in cabecalho]
    mapa: dict[str, int] = {}
    for campo, alternativas in COLUNAS.items():
        for alt in alternativas:
            if alt in normalizado:
                mapa[campo] = normalizado.index(alt)
                break
    em_falta = [c for c in ("data", "fecho") if c not in mapa]
    if em_falta:
        raise ErroDados(
            f"faltam colunas obrigatorias no CSV: {em_falta}. "
            f"Encontrei: {normalizado}"
        )
    return mapa


def carregar(caminho: str | Path, inicio: str | None = None,
             fim: str | None = None) -> list[Barra]:
    """Le o CSV e devolve as barras dentro da janela [inicio, fim].

    O filtro de datas e feito aqui, no arnes, e nao na estrategia. Se a
    estrategia pudesse escolher a janela, podia escolher a janela que lhe
    convem — e o protocolo de treino/validacao/holdout deixava de significar
    alguma coisa.
    """
    caminho = Path(caminho)
    if not caminho.is_file():
        raise ErroDados(
            f"nao encontrei {caminho}. Poe o teu CSV em dados/ ou corre "
            "`python gerar_dados_exemplo.py` para criar uma serie sintetica."
        )
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        leitor = csv.reader(f)
        try:
            mapa = _mapear(next(leitor))
        except StopIteration:
            raise ErroDados(f"{caminho} esta vazio") from None

        barras: list[Barra] = []
        for n, linha in enumerate(leitor, 2):
            if not linha or not linha[mapa["data"]].strip():
                continue
            data = linha[mapa["data"]].strip()[:10]
            if inicio and data < inicio:
                continue
            if fim and data > fim:
                continue
            try:
                fecho = float(linha[mapa["fecho"]])
                barras.append(Barra(
                    data=data,
                    abertura=float(linha[mapa["abertura"]]) if "abertura" in mapa else fecho,
                    maxima=float(linha[mapa["maxima"]]) if "maxima" in mapa else fecho,
                    minima=float(linha[mapa["minima"]]) if "minima" in mapa else fecho,
                    fecho=fecho,
                    volume=float(linha[mapa["volume"]]) if "volume" in mapa else 0.0,
                ))
            except (ValueError, IndexError) as e:
                raise ErroDados(f"{caminho}:{n} nao consigo ler a linha: {e}") from e

    if not barras:
        raise ErroDados(
            f"nenhuma barra entre {inicio} e {fim} em {caminho}. "
            "Confere as janelas do protocolo contra as datas que tens no ficheiro."
        )
    barras.sort(key=lambda b: b.data)
    return barras
