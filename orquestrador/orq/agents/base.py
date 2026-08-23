"""Base dos sub-agentes: prompt, chamada, validacao, e nova tentativa com o erro.

O truque que torna um modelo de 7B utilizavel aqui nao e o prompt — e o ciclo.
Pedir JSON a um modelo pequeno falha muitas vezes a primeira. Mas se lhe
devolvermos a mensagem de erro concreta ("o parametro sma_slow tem de estar
entre 10 e 300, mandaste 1200") ele corrige quase sempre a segunda. Sem este
ciclo, precisarias de um modelo muito maior para ter a mesma taxa de sucesso.

A validacao nunca e delegada ao modelo. `parse` corre em Python e levanta
ValueError com uma mensagem escrita para o modelo a conseguir agir sobre ela.
"""
from __future__ import annotations

import time
from typing import Any

from ..llm.base import LLMError, LLMProvider, extract_json


class AgentError(Exception):
    """O sub-agente nao produziu nada utilizavel dentro das tentativas permitidas."""


class Agent:
    role: str = "agent"
    json_mode: bool = True

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        *,
        max_retries: int = 3,
        store: Any = None,
    ):
        self.provider = provider
        self.model = model
        self.max_retries = max(1, max_retries)
        self.store = store

    # -- a implementar por cada sub-agente -------------------------------
    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_prompt(self, **kwargs: Any) -> str:
        raise NotImplementedError

    def parse(self, data: Any, **kwargs: Any) -> Any:
        """Valida a resposta ja desserializada.

        Levanta ValueError com uma mensagem que o modelo consiga usar para se
        corrigir. Frases como "invalido" nao ajudam; "faltou a chave `params`"
        ajuda.
        """
        return data

    # -- ciclo -----------------------------------------------------------
    def run(self, *, experiment_id: str | None = None, task_id: str | None = None, **kwargs: Any) -> Any:
        system = self.system_prompt()
        prompt = self.build_prompt(**kwargs)
        started = time.monotonic()
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            message = prompt if last_error is None else (
                f"{prompt}\n\n"
                f"--- A TUA RESPOSTA ANTERIOR FOI REJEITADA ---\n"
                f"Motivo: {last_error}\n"
                f"Corrige e devolve APENAS o JSON no formato pedido, sem texto a volta."
            )
            try:
                response = self.provider.chat(
                    system, message, model=self.model, json_mode=self.json_mode
                )
                data = extract_json(response.text) if self.json_mode else response.text
                result = self.parse(data, **kwargs)
            except (LLMError, ValueError) as exc:
                last_error = str(exc)
                continue

            self._log(True, attempt, started, experiment_id, task_id)
            return result

        self._log(False, self.max_retries, started, experiment_id, task_id, last_error)
        raise AgentError(
            f"[{self.role}] o modelo {self.model} falhou {self.max_retries} tentativas. "
            f"Ultimo erro: {last_error}"
        )

    def _log(
        self,
        ok: bool,
        attempts: int,
        started: float,
        experiment_id: str | None,
        task_id: str | None,
        error: str | None = None,
    ) -> None:
        if self.store is None:
            return
        self.store.log_agent_run(
            role=self.role,
            model=self.model,
            ok=ok,
            experiment_id=experiment_id,
            task_id=task_id,
            attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
