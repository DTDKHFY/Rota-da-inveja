"""Agente Proponente: transforma uma hipotese em valores concretos.

Aviso que vale a pena ter presente: um LLM e um mau otimizador numerico. Nao o
uses a fazer varredura de parametros — para isso ha busca aleatoria e Optuna,
que sao mais rapidos e nao alucinam. O que ele faz de util aqui e traduzir uma
hipotese ("aumentar o stop porque o drawdown vem de saidas prematuras") num
ponto de partida razoavel.

Por isso este modulo tem sempre uma rede: se o modelo falhar as tentativas
todas, cai numa amostragem aleatoria dentro dos limites. O estudo nunca para
por o Llama ter tido um mau dia.
"""
from __future__ import annotations

import random
from typing import Any

from ..config import ExperimentConfig, ParamSpec
from .base import Agent
from .research import describe_history, describe_schema

SYSTEM = """Es um assistente que escolhe valores de parametros para um backtest.

Recebes uma hipotese e uma lista de parametros com limites. Devolves um valor
para CADA parametro da lista, respeitando os limites.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- Inclui TODOS os parametros da lista, nenhum a mais, nenhum a menos.
- Nunca proponhas um valor fora dos limites indicados.
- Se um parametro nao for relevante para a hipotese, repete o valor atual dele.

Formato exato da resposta:
{"params": {"nome_param": valor, ...}, "justificacao": "uma frase curta"}
"""


def random_params(schema: dict[str, ParamSpec], rng: random.Random | None = None) -> dict:
    """Ponto aleatorio dentro dos limites. Usado como rede quando o modelo falha."""
    rng = rng or random.Random()
    saida: dict[str, int | float] = {}
    for spec in schema.values():
        if spec.type == "int":
            saida[spec.name] = rng.randint(int(spec.min), int(spec.max))
        else:
            saida[spec.name] = round(rng.uniform(spec.min, spec.max), 6)
    return saida


def perturb_params(
    base: dict, schema: dict[str, ParamSpec], *, escala: float = 0.15,
    rng: random.Random | None = None,
) -> dict:
    """Vizinhanca de um ponto conhecido. Serve para refinar o melhor ensaio ate agora."""
    rng = rng or random.Random()
    saida: dict[str, int | float] = {}
    for spec in schema.values():
        atual = base.get(spec.name, (spec.min + spec.max) / 2)
        amplitude = (spec.max - spec.min) * escala
        candidato = atual + rng.uniform(-amplitude, amplitude)
        candidato = min(max(candidato, spec.min), spec.max)
        saida[spec.name] = int(round(candidato)) if spec.type == "int" else round(candidato, 6)
    return saida


class ProposerAgent(Agent):
    role = "proposer"

    def __init__(self, provider, model, experiment: ExperimentConfig, **kwargs):
        super().__init__(provider, model, **kwargs)
        self.experiment = experiment

    def system_prompt(self) -> str:
        return SYSTEM

    def build_prompt(
        self,
        *,
        hipotese: dict,
        params_atuais: dict,
        historico: list[dict],
        **_: Any,
    ) -> str:
        schema = self.experiment.params_schema
        atuais = ", ".join(f"{k}={v:g}" for k, v in sorted(params_atuais.items())) or "(nenhum)"
        return (
            f"HIPOTESE A TESTAR:\n"
            f"{hipotese['nome']} — {hipotese['raciocinio']}\n"
            f"Parametros a mexer: {', '.join(hipotese['parametros_alvo'])}\n"
            f"Direcao sugerida: {hipotese['direcao']}\n\n"
            f"VALORES ATUAIS:\n{atuais}\n\n"
            f"LIMITES (obrigatorio respeitar):\n{describe_schema(schema)}\n\n"
            f"ENSAIOS ANTERIORES:\n{describe_history(historico, limite=8)}\n\n"
            f"Devolve os valores para o proximo ensaio."
        )

    def parse(self, data: Any, **_: Any) -> dict:
        if not isinstance(data, dict) or "params" not in data:
            raise ValueError("falta a chave `params` no objeto de topo")
        # coerce_params rejeita chaves a mais, a menos, e valores fora dos limites.
        # E aqui que o modelo deixa de poder inventar.
        params = self.experiment.coerce_params(data["params"])
        return {
            "params": params,
            "justificacao": str(data.get("justificacao", ""))[:400],
        }

    def propose_with_fallback(
        self,
        *,
        hipotese: dict,
        params_atuais: dict,
        historico: list[dict],
        experiment_id: str | None = None,
        rng: random.Random | None = None,
    ) -> dict:
        """Tenta o modelo; se falhar, amostra em vez de abortar o estudo."""
        from .base import AgentError

        try:
            return self.run(
                hipotese=hipotese,
                params_atuais=params_atuais,
                historico=historico,
                experiment_id=experiment_id,
            )
        except AgentError as exc:
            schema = self.experiment.params_schema
            params = (
                perturb_params(params_atuais, schema, rng=rng)
                if params_atuais
                else random_params(schema, rng=rng)
            )
            return {
                "params": params,
                "justificacao": (
                    "o modelo nao devolveu proposta valida; usei amostragem "
                    f"dentro dos limites como alternativa ({exc.__class__.__name__})"
                ),
                "fallback": True,
            }
