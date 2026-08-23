#!/usr/bin/env python3
"""Arnes do backtest. ← O AGENTE NAO PODE ALTERAR ESTE FICHEIRO

E este o programa que o orquestrador executa a cada ensaio:

    python3 run_backtest.py --params p.json --start 2015-01-01 --end 2021-12-31 --out m.json

Faz tres coisas, por esta ordem:
  1. carrega os dados da janela pedida (o filtro e aqui, nao na estrategia);
  2. pede os sinais a estrategia;
  3. simula, calcula as metricas e grava o JSON.

A estrategia nunca ve o passo 3. E de proposito.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from dados import ErroDados, carregar          # noqa: E402
from metricas import para_json, simular        # noqa: E402

FICHEIRO_DADOS = BASE / "dados" / "serie.csv"
PERIODOS_ANO = 252


def main() -> int:
    ap = argparse.ArgumentParser(description="Corre um backtest e grava as metricas.")
    ap.add_argument("--params", required=True, help="JSON com os parametros")
    ap.add_argument("--start", required=True, help="inicio da janela (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="fim da janela (YYYY-MM-DD)")
    ap.add_argument("--out", required=True, help="onde gravar o JSON de metricas")
    ap.add_argument("--dados", default=str(FICHEIRO_DADOS), help="CSV de barras")
    a = ap.parse_args()

    try:
        params = json.loads(Path(a.params).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"erro a ler {a.params}: {e}", file=sys.stderr)
        return 2

    try:
        barras = carregar(a.dados, a.start, a.end)
    except ErroDados as e:
        print(f"erro nos dados: {e}", file=sys.stderr)
        return 2

    # A estrategia so entra aqui, e so recebe barras e parametros.
    from estrategia import gerar_sinais, tamanho_posicao

    try:
        sinais = gerar_sinais(barras, params)
    except Exception as e:
        # Uma estrategia partida tem de falhar alto. Devolver zeros em silencio
        # daria um backtest "valido" com resultado nulo, e o agente ficaria a
        # optimizar contra um erro.
        print(f"a estrategia rebentou: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    if not isinstance(sinais, list) or len(sinais) != len(barras):
        print(f"a estrategia devolveu {type(sinais).__name__} com "
              f"{len(sinais) if hasattr(sinais, '__len__') else '?'} elementos; "
              f"esperava uma lista de {len(barras)} inteiros", file=sys.stderr)
        return 3
    if any(s not in (-1, 0, 1) for s in sinais):
        invalidos = sorted({s for s in sinais if s not in (-1, 0, 1)})[:5]
        print(f"sinais invalidos: {invalidos}. So sao aceites -1, 0 e 1.", file=sys.stderr)
        return 3

    resultado = simular(
        barras, sinais,
        custo_por_trade=float(params.get("custo_por_trade", 0.0005)),
        slippage=float(params.get("slippage", 0.0002)),
        tamanho=tamanho_posicao(params),
    )
    saida = para_json(resultado, PERIODOS_ANO)
    saida["janela"] = [a.start, a.end]
    saida["barras"] = len(barras)

    Path(a.out).write_text(json.dumps(saida), encoding="utf-8")
    print(f"{a.start} a {a.end} | {len(barras)} barras | {resultado.trades} trades | "
          f"Sharpe {saida['sharpe']:.2f} | drawdown {resultado.drawdown_maximo*100:.1f}% | "
          f"exposicao {resultado.exposicao*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
