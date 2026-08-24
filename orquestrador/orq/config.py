"""Carregamento e validacao do config.

Regra: segredos vem do ambiente (.env), nunca do YAML. O YAML descreve o
comportamento; o .env guarda credenciais e esta no .gitignore.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Config invalido. A mensagem diz exatamente que chave corrigir."""


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def load_dotenv(path: str | Path) -> dict[str, str]:
    """Le um .env simples (KEY=valor) para o ambiente, sem sobrepor o que ja existe.

    Nao suporta interpolacao nem multilinha de proposito: menos superficie para
    um segredo escapar por engano.
    """
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


# --------------------------------------------------------------------------
# Seccoes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_chat_ids: tuple[int, ...]
    poll_timeout_sec: int = 30

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.allowed_chat_ids


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    request_timeout_sec: int = 300
    temperature: float = 0.2
    num_ctx: int = 8192
    max_json_retries: int = 3
    models: dict[str, str] = field(default_factory=dict)

    def model_for(self, role: str) -> str:
        try:
            return self.models[role]
        except KeyError:
            raise ConfigError(
                f"llm.models.{role} nao definido. Corre `ollama list` e poe o tag exato."
            ) from None


@dataclass(frozen=True)
class TargetConfig:
    path: Path
    backtest_cmd: str
    timeout_sec: int = 1800
    network: bool = False
    test_cmd: str | None = None
    # Caminhos (relativos ao target) a ligar por symlink dentro do worktree.
    # Serve para dados historicos que nao estao no git e que seria absurdo copiar
    # a cada ensaio.
    link_paths: tuple[str, ...] = ()
    # Ficheiro (relativo ao target) onde vivem os parametros em producao.
    # Uma proposta aprovada e escrita aqui, num ramo novo — nunca no ramo ativo.
    params_file: str = "params.json"
    # Modo `code`: os UNICOS ficheiros que o agente de desenvolvimento pode
    # alterar. Tudo o resto — em especial o que corre e mede o backtest — fica
    # fora do alcance dele.
    editable_paths: tuple[str, ...] = ()
    # Funcoes congeladas: para quando estrategia e metricas partilham
    # ficheiro e a lista branca de caminhos ja nao protege nada.
    frozen_functions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str  # "int" | "float"
    min: float
    max: float

    def coerce(self, value: Any) -> int | float:
        """Converte e valida um valor proposto pelo LLM. Levanta ValueError se sair dos limites."""
        if isinstance(value, bool):
            raise ValueError(f"{self.name}: booleano nao e um valor numerico valido")
        try:
            number = int(value) if self.type == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.name}: {value!r} nao converte para {self.type}") from None
        if not (self.min <= number <= self.max):
            raise ValueError(
                f"{self.name}: {number} fora dos limites [{self.min}, {self.max}]"
            )
        return number


@dataclass(frozen=True)
class ExperimentConfig:
    mode: str = "params"
    params_schema: dict[str, ParamSpec] = field(default_factory=dict)
    # Travao de tamanho no modo `code`. Uma proposta que toca 400 linhas nao e
    # uma hipotese testavel — e uma reescrita, e ninguem consegue rever isso a
    # partir do Telegram.
    max_edit_lines: int = 120

    def coerce_params(self, proposed: dict[str, Any]) -> dict[str, int | float]:
        """Valida um conjunto completo de parametros vindo do LLM.

        Rejeita chaves desconhecidas e valores fora dos limites. Esta e a
        fronteira onde o modelo deixa de poder inventar.
        """
        if not isinstance(proposed, dict):
            raise ValueError("params tem de ser um objeto JSON")
        unknown = set(proposed) - set(self.params_schema)
        if unknown:
            raise ValueError(f"parametros desconhecidos: {sorted(unknown)}")
        missing = set(self.params_schema) - set(proposed)
        if missing:
            raise ValueError(f"parametros em falta: {sorted(missing)}")
        return {name: spec.coerce(proposed[name]) for name, spec in self.params_schema.items()}


@dataclass(frozen=True)
class ProtocolConfig:
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    holdout_start: str
    holdout_end: str
    max_trials_per_study: int = 200


@dataclass(frozen=True)
class GateConfig:
    min_trades: int = 100
    min_oos_sharpe: float = 0.5
    max_oos_drawdown: float = 0.25
    min_dsr: float = 0.95
    require_oos_improvement_pct: float = 5.0
    max_is_oos_sharpe_gap: float = 1.0


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path
    worktrees_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    llm: LLMConfig
    target: TargetConfig
    experiment: ExperimentConfig
    protocol: ProtocolConfig
    gate: GateConfig
    storage: StorageConfig
    source_path: Path | None = None


# --------------------------------------------------------------------------
# Carregamento
# --------------------------------------------------------------------------

def _require(section: dict, key: str, where: str) -> Any:
    if key not in section:
        raise ConfigError(f"falta a chave obrigatoria `{where}.{key}` no config")
    return section[key]


def _parse_params_schema(raw: dict) -> dict[str, ParamSpec]:
    specs: dict[str, ParamSpec] = {}
    for name, body in (raw or {}).items():
        if not isinstance(body, dict):
            raise ConfigError(f"experiment.params_schema.{name} tem de ser um objeto")
        kind = body.get("type", "float")
        if kind not in ("int", "float"):
            raise ConfigError(
                f"experiment.params_schema.{name}.type: esperado 'int' ou 'float', veio {kind!r}"
            )
        if "min" not in body or "max" not in body:
            raise ConfigError(f"experiment.params_schema.{name}: falta `min` ou `max`")
        low, high = float(body["min"]), float(body["max"])
        if low > high:
            raise ConfigError(f"experiment.params_schema.{name}: min ({low}) > max ({high})")
        specs[name] = ParamSpec(name=name, type=kind, min=low, max=high)
    return specs


def load_config(path: str | Path | None = None, *, env_path: str | Path | None = None) -> Config:
    """Le config.yaml + .env e devolve um Config validado.

    A ordem de procura do config: argumento > ORQ_CONFIG > ./config.yaml
    junto ao pacote.
    """
    base_dir = Path(__file__).resolve().parent.parent
    load_dotenv(env_path or base_dir / ".env")

    if path is None:
        path = os.environ.get("ORQ_CONFIG") or base_dir / "config.yaml"
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"config nao encontrado em {path}. Copia o config.example.yaml para config.yaml."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN nao definido. Poe no .env (copia do .env.example). "
            "Nunca metas o token no config.yaml."
        )
    if raw.get("telegram", {}).get("token"):
        raise ConfigError(
            "encontrei um token dentro do config.yaml. Tira-o dai e usa o .env — "
            "o config.yaml e facil de partilhar por engano."
        )

    tg_raw = raw.get("telegram", {}) or {}
    chat_ids = tuple(int(c) for c in _require(tg_raw, "allowed_chat_ids", "telegram"))
    if not chat_ids:
        raise ConfigError("telegram.allowed_chat_ids esta vazio: ninguem poderia dar ordens")

    llm_raw = raw.get("llm", {}) or {}
    target_raw = raw.get("target", {}) or {}
    exp_raw = raw.get("experiment", {}) or {}
    proto_raw = raw.get("protocol", {}) or {}
    gate_raw = raw.get("gate", {}) or {}
    store_raw = raw.get("storage", {}) or {}

    mode = exp_raw.get("mode", "params")
    if mode not in ("params", "code"):
        raise ConfigError(f"experiment.mode: esperado 'params' ou 'code', veio {mode!r}")
    if mode == "code" and not (target_raw.get("editable_paths") or []):
        raise ConfigError(
            "experiment.mode: 'code' exige target.editable_paths preenchido. "
            "Sem lista branca, o agente pode editar o proprio codigo que calcula "
            "as metricas — e a forma mais rapida de 'melhorar' um Sharpe e "
            "reescrever a funcao que o calcula. Indica so os ficheiros de "
            "estrategia."
        )

    def _store_path(key: str, default: str) -> Path:
        value = Path(store_raw.get(key, default))
        return value if value.is_absolute() else (base_dir / value).resolve()

    protocol = ProtocolConfig(
        train_start=str(_require(proto_raw, "train_start", "protocol")),
        train_end=str(_require(proto_raw, "train_end", "protocol")),
        validation_start=str(_require(proto_raw, "validation_start", "protocol")),
        validation_end=str(_require(proto_raw, "validation_end", "protocol")),
        holdout_start=str(_require(proto_raw, "holdout_start", "protocol")),
        holdout_end=str(_require(proto_raw, "holdout_end", "protocol")),
        max_trials_per_study=int(proto_raw.get("max_trials_per_study", 200)),
    )
    if not (protocol.train_end < protocol.validation_start < protocol.holdout_start):
        raise ConfigError(
            "protocol: as janelas tem de ser cronologicas e sem sobreposicao "
            "(train_end < validation_start < holdout_start). "
            "Se o holdout se sobrepoe ao treino, deixa de ser holdout."
        )

    return Config(
        telegram=TelegramConfig(
            token=token,
            allowed_chat_ids=chat_ids,
            poll_timeout_sec=int(tg_raw.get("poll_timeout_sec", 30)),
        ),
        llm=LLMConfig(
            provider=llm_raw.get("provider", "ollama"),
            base_url=llm_raw.get("base_url", "http://localhost:11434").rstrip("/"),
            request_timeout_sec=int(llm_raw.get("request_timeout_sec", 300)),
            temperature=float(llm_raw.get("temperature", 0.2)),
            num_ctx=int(llm_raw.get("num_ctx", 8192)),
            max_json_retries=int(llm_raw.get("max_json_retries", 3)),
            models=dict(llm_raw.get("models", {}) or {}),
        ),
        target=TargetConfig(
            path=Path(_require(target_raw, "path", "target")).expanduser(),
            backtest_cmd=str(_require(target_raw, "backtest_cmd", "target")),
            timeout_sec=int(target_raw.get("timeout_sec", 1800)),
            network=bool(target_raw.get("network", False)),
            test_cmd=target_raw.get("test_cmd") or None,
            link_paths=tuple(target_raw.get("link_paths", []) or []),
            params_file=str(target_raw.get("params_file", "params.json")),
            editable_paths=tuple(target_raw.get("editable_paths", []) or []),
            frozen_functions=tuple(target_raw.get("frozen_functions", []) or []),
        ),
        experiment=ExperimentConfig(
            mode=mode,
            params_schema=_parse_params_schema(exp_raw.get("params_schema", {})),
            max_edit_lines=int(exp_raw.get("max_edit_lines", 120)),
        ),
        protocol=protocol,
        gate=GateConfig(
            min_trades=int(gate_raw.get("min_trades", 100)),
            min_oos_sharpe=float(gate_raw.get("min_oos_sharpe", 0.5)),
            max_oos_drawdown=float(gate_raw.get("max_oos_drawdown", 0.25)),
            min_dsr=float(gate_raw.get("min_dsr", 0.95)),
            require_oos_improvement_pct=float(gate_raw.get("require_oos_improvement_pct", 5.0)),
            max_is_oos_sharpe_gap=float(gate_raw.get("max_is_oos_sharpe_gap", 1.0)),
        ),
        storage=StorageConfig(
            db_path=_store_path("db_path", "./data/orq.db"),
            worktrees_dir=_store_path("worktrees_dir", "./data/worktrees"),
            log_dir=_store_path("log_dir", "./data/logs"),
        ),
        source_path=path,
    )
