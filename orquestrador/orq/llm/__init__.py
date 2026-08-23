from .base import LLMError, LLMProvider, LLMResponse, extract_json
from .fake import FakeProvider
from .ollama import OllamaProvider

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "extract_json",
    "FakeProvider",
    "OllamaProvider",
    "build_provider",
]


def build_provider(config) -> LLMProvider:
    """Constroi o provider a partir do bloco `llm` do config."""
    if config.provider == "ollama":
        return OllamaProvider(
            base_url=config.base_url,
            timeout=config.request_timeout_sec,
            temperature=config.temperature,
            num_ctx=config.num_ctx,
        )
    if config.provider == "fake":
        return FakeProvider()
    raise LLMError(f"provider desconhecido: {config.provider!r} (usa 'ollama' ou 'fake')")
