import pytest
import yaml

from orq.config import ConfigError, ExperimentConfig, ParamSpec, load_config


def test_carrega_e_valida(config):
    assert config.telegram.allowed_chat_ids == (42,)
    assert config.telegram.is_allowed(42)
    assert not config.telegram.is_allowed(999)
    assert config.experiment.mode == "params"
    assert config.protocol.holdout_start == "2024-01-01"


def test_recusa_token_dentro_do_yaml(tmp_path, alvo, monkeypatch):
    """Um token no YAML e um token que vai parar ao git mais cedo ou mais tarde."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    corpo = {
        "telegram": {"allowed_chat_ids": [1], "token": "123:SEGREDO"},
        "target": {"path": str(alvo), "backtest_cmd": "x"},
        "protocol": {
            "train_start": "2015-01-01", "train_end": "2020-12-31",
            "validation_start": "2021-01-01", "validation_end": "2022-12-31",
            "holdout_start": "2023-01-01", "holdout_end": "2024-12-31",
        },
    }
    caminho = tmp_path / "c.yaml"
    caminho.write_text(yaml.safe_dump(corpo))
    with pytest.raises(ConfigError, match="token dentro do config"):
        load_config(caminho, env_path=tmp_path / "vazio")


def test_exige_token_no_ambiente(tmp_path, alvo, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    corpo = {
        "telegram": {"allowed_chat_ids": [1]},
        "target": {"path": str(alvo), "backtest_cmd": "x"},
        "protocol": {
            "train_start": "2015-01-01", "train_end": "2020-12-31",
            "validation_start": "2021-01-01", "validation_end": "2022-12-31",
            "holdout_start": "2023-01-01", "holdout_end": "2024-12-31",
        },
    }
    caminho = tmp_path / "c.yaml"
    caminho.write_text(yaml.safe_dump(corpo))
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(caminho, env_path=tmp_path / "vazio")


def test_recusa_janelas_sobrepostas(tmp_path, alvo, monkeypatch):
    """Um holdout que se sobrepoe ao treino nao e um holdout."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    corpo = {
        "telegram": {"allowed_chat_ids": [1]},
        "target": {"path": str(alvo), "backtest_cmd": "x"},
        "protocol": {
            "train_start": "2015-01-01", "train_end": "2023-12-31",
            "validation_start": "2021-01-01", "validation_end": "2022-12-31",
            "holdout_start": "2020-01-01", "holdout_end": "2024-12-31",
        },
    }
    caminho = tmp_path / "c.yaml"
    caminho.write_text(yaml.safe_dump(corpo))
    with pytest.raises(ConfigError, match="cronologicas"):
        load_config(caminho, env_path=tmp_path / "vazio")


def test_recusa_allowlist_vazia(tmp_path, alvo, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    corpo = {
        "telegram": {"allowed_chat_ids": []},
        "target": {"path": str(alvo), "backtest_cmd": "x"},
        "protocol": {
            "train_start": "2015-01-01", "train_end": "2020-12-31",
            "validation_start": "2021-01-01", "validation_end": "2022-12-31",
            "holdout_start": "2023-01-01", "holdout_end": "2024-12-31",
        },
    }
    caminho = tmp_path / "c.yaml"
    caminho.write_text(yaml.safe_dump(corpo))
    with pytest.raises(ConfigError, match="allowed_chat_ids"):
        load_config(caminho, env_path=tmp_path / "vazio")


# --- a fronteira onde o LLM deixa de poder inventar -----------------------

@pytest.fixture
def exp():
    return ExperimentConfig(
        mode="params",
        params_schema={
            "a": ParamSpec("a", "int", 2, 50),
            "b": ParamSpec("b", "float", 0.5, 6.0),
        },
    )


def test_aceita_valores_validos(exp):
    assert exp.coerce_params({"a": 10, "b": 2.5}) == {"a": 10, "b": 2.5}


def test_converte_tipos(exp):
    saida = exp.coerce_params({"a": "10", "b": 3})
    assert saida == {"a": 10, "b": 3.0}
    assert isinstance(saida["b"], float)


def test_rejeita_fora_dos_limites(exp):
    with pytest.raises(ValueError, match=r"fora dos limites \[2"):
        exp.coerce_params({"a": 900, "b": 2.5})


def test_rejeita_parametro_inventado(exp):
    with pytest.raises(ValueError, match="desconhecidos"):
        exp.coerce_params({"a": 10, "b": 2.5, "rsi": 14})


def test_rejeita_parametro_em_falta(exp):
    with pytest.raises(ValueError, match="em falta"):
        exp.coerce_params({"a": 10})


def test_rejeita_booleano(exp):
    """True vale 1 em Python. Um LLM a mandar `true` nao pode virar um parametro."""
    with pytest.raises(ValueError, match="booleano"):
        exp.coerce_params({"a": True, "b": 2.5})


def test_rejeita_texto_nao_numerico(exp):
    with pytest.raises(ValueError, match="nao converte"):
        exp.coerce_params({"a": "muito rapido", "b": 2.5})
