"""Cliente do Ollama. So HTTP — sem SDK, sem dependencia extra."""
from __future__ import annotations

import time

import requests

from .base import LLMError, LLMResponse


class OllamaProvider:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        timeout: int = 300,
        temperature: float = 0.2,
        num_ctx: int = 8192,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.num_ctx = num_ctx

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if json_mode:
            # Constrange a geracao a JSON valido. Nao garante o *esquema* certo,
            # so a sintaxe — a validacao do conteudo continua a ser nossa.
            payload["format"] = "json"

        started = time.monotonic()
        try:
            response = requests.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
        except requests.exceptions.ConnectionError as exc:
            raise LLMError(
                f"nao consegui falar com o Ollama em {self.base_url}. "
                "Esta a correr? Testa com `ollama list`."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise LLMError(
                f"o modelo {model} nao respondeu em {self.timeout}s. "
                "Se for um modelo grande em CPU, sobe llm.request_timeout_sec."
            ) from exc

        if response.status_code == 404:
            raise LLMError(
                f"o Ollama nao conhece o modelo {model!r}. "
                f"Corre `ollama pull {model}` ou corrige llm.models no config."
            )
        if not response.ok:
            raise LLMError(f"Ollama devolveu {response.status_code}: {response.text[:300]}")

        try:
            body = response.json()
            content = body["message"]["content"]
        except (ValueError, KeyError) as exc:
            raise LLMError(f"resposta do Ollama em formato inesperado: {response.text[:300]}") from exc

        return LLMResponse(
            text=content,
            model=model,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def available_models(self) -> list[str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=15)
            response.raise_for_status()
            return [m["name"] for m in response.json().get("models", [])]
        except (requests.RequestException, ValueError, KeyError):
            return []
