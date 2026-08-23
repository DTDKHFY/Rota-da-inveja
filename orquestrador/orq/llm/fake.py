"""Provider de mentira, para os testes correrem sem Ollama nem GPU."""
from __future__ import annotations

from collections.abc import Callable

from .base import LLMError, LLMResponse


class FakeProvider:
    """Devolve respostas guionadas, por ordem ou por funcao.

    Aceita callables para poder simular o caso que mais interessa testar: o
    modelo devolve lixo a primeira, e so acerta depois de lhe dizermos o erro.
    """

    def __init__(self, responses: list[str | Callable[[str, str], str]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "user": user, "model": model, "json_mode": json_mode}
        )
        if not self.responses:
            raise LLMError("FakeProvider ficou sem respostas guionadas")
        item = self.responses.pop(0)
        text = item(system, user) if callable(item) else item
        return LLMResponse(text=text, model=model, duration_ms=1)

    def available_models(self) -> list[str]:
        return ["fake:test"]
