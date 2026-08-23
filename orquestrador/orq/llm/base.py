"""Interface comum aos modelos, e o extractor de JSON que aguenta modelos pequenos."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    """O modelo nao respondeu, ou respondeu algo inutilizavel."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    duration_ms: int


class LLMProvider(Protocol):
    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Tira um objeto JSON de uma resposta que pode vir suja.

    Um 7b raramente devolve so o JSON. Vem com "Claro! Aqui esta:" antes, uma
    explicacao depois, cercas de markdown a volta, ou tudo isso junto. Em vez de
    exigir limpeza do modelo (que nao a consegue dar de forma fiavel), limpo eu:
    primeiro tento o texto cru, depois blocos cercados, depois a maior regiao
    entre chavetas equilibradas.
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("resposta vazia do modelo")

    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMError(f"nao encontrei JSON valido na resposta: {text[:300]}")


def _json_candidates(text: str):
    yield text
    for block in _FENCE.findall(text):
        yield block.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
