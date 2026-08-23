"""Agente de Pesquisa: le o historico do estudo e propoe o que investigar a seguir.

Este e o unico sitio onde um LLM faz algo que codigo nao faz melhor. Escolher
numeros e trabalho de um otimizador; olhar para vinte ensaios e dizer "sempre
que aumentaste o stop o drawdown melhorou mas o numero de trades colapsou,
vale a pena atacar por outro lado" e trabalho de raciocinio. E o que se pede
aqui: hipoteses e direcao, nao valores.
"""
from __future__ import annotations

from typing import Any

from ..config import ParamSpec
from .base import Agent

DIRECOES = {"aumentar", "diminuir", "explorar"}

SYSTEM = """Es um analista quantitativo. Trabalhas num sistema de backtest.

A tua unica funcao e propor HIPOTESES para o proximo ensaio, olhando para o
historico de ensaios ja feitos. Nao escolhes valores concretos — isso e feito
por outro componente. Dizes QUE parametro mexer, EM QUE DIRECAO e PORQUE.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- Usa apenas nomes de parametros da lista que te for dada.
- Se o historico mostrar que uma direcao ja foi tentada e piorou, nao a repitas.
- Se nao tiveres base para uma hipotese, diz "explorar" em vez de inventar.

Formato exato da resposta:
{"hipoteses": [{"nome": "...", "raciocinio": "...", "parametros_alvo": ["nome_param"], "direcao": "aumentar"}]}

"direcao" so pode ser: "aumentar", "diminuir" ou "explorar".
"""


def describe_schema(schema: dict[str, ParamSpec]) -> str:
    linhas = []
    for spec in schema.values():
        tipo = "inteiro" if spec.type == "int" else "decimal"
        linhas.append(f"- {spec.name}: {tipo} entre {spec.min:g} e {spec.max:g}")
    return "\n".join(linhas)


def describe_history(historico: list[dict], limite: int = 15) -> str:
    if not historico:
        return "(ainda nao ha ensaios neste estudo)"
    linhas = []
    for item in historico[-limite:]:
        params = ", ".join(f"{k}={v:g}" for k, v in sorted(item["params"].items()))
        sharpe = item.get("oos_sharpe")
        drawdown = item.get("oos_drawdown")
        resultado = "falhou" if sharpe is None else f"Sharpe OOS {sharpe:+.2f}"
        if drawdown is not None:
            resultado += f", drawdown {drawdown * 100:.1f}%"
        linhas.append(f"- {params} -> {resultado}")
    return "\n".join(linhas)


class ResearchAgent(Agent):
    role = "research"

    def system_prompt(self) -> str:
        return SYSTEM

    def build_prompt(
        self,
        *,
        objetivo: str,
        schema: dict[str, ParamSpec],
        historico: list[dict],
        n_hipoteses: int = 3,
        **_: Any,
    ) -> str:
        return (
            f"OBJETIVO DO ESTUDO:\n{objetivo}\n\n"
            f"PARAMETROS DISPONIVEIS:\n{describe_schema(schema)}\n\n"
            f"ENSAIOS JA FEITOS:\n{describe_history(historico)}\n\n"
            f"Propoe exatamente {n_hipoteses} hipoteses distintas para o proximo ensaio."
        )

    def parse(self, data: Any, *, schema: dict[str, ParamSpec], **_: Any) -> list[dict]:
        if not isinstance(data, dict) or "hipoteses" not in data:
            raise ValueError('falta a chave `hipoteses` no objeto de topo')
        hipoteses = data["hipoteses"]
        if not isinstance(hipoteses, list) or not hipoteses:
            raise ValueError("`hipoteses` tem de ser uma lista nao vazia")

        validas: list[dict] = []
        for i, h in enumerate(hipoteses):
            if not isinstance(h, dict):
                raise ValueError(f"hipotese {i} nao e um objeto")
            for chave in ("nome", "raciocinio", "parametros_alvo", "direcao"):
                if chave not in h:
                    raise ValueError(f"hipotese {i} nao tem a chave `{chave}`")
            direcao = str(h["direcao"]).lower().strip()
            if direcao not in DIRECOES:
                raise ValueError(
                    f"hipotese {i}: direcao {h['direcao']!r} invalida, "
                    f"usa uma de {sorted(DIRECOES)}"
                )
            alvos = h["parametros_alvo"]
            if not isinstance(alvos, list) or not alvos:
                raise ValueError(f"hipotese {i}: `parametros_alvo` tem de ser lista nao vazia")
            desconhecidos = [a for a in alvos if a not in schema]
            if desconhecidos:
                raise ValueError(
                    f"hipotese {i}: parametros inexistentes {desconhecidos}. "
                    f"So podes usar: {sorted(schema)}"
                )
            validas.append(
                {
                    "nome": str(h["nome"])[:120],
                    "raciocinio": str(h["raciocinio"])[:600],
                    "parametros_alvo": [str(a) for a in alvos],
                    "direcao": direcao,
                }
            )
        return validas
