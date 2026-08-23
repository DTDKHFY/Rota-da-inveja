"""Fixtures: um projeto-alvo de mentira, mas com git a serio.

O backtest falso e deterministico e tem um optimo interior conhecido, para os
testes poderem afirmar coisas concretas sobre o comportamento do sistema em vez
de "correu sem rebentar".
"""
from __future__ import annotations

import json
import math
import subprocess
import textwrap

import pytest
import yaml

from orq.config import load_config
from orq.store import Store

BACKTEST_FALSO = '''\
import argparse, json, math, pathlib, random

ap = argparse.ArgumentParser()
ap.add_argument("--params", required=True)
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from estrategia.sinal import forca  # noqa: E402

p = json.loads(pathlib.Path(a.params).read_text())
# Optimo em sma_fast=20: quanto mais longe, pior. Deterministico pela semente.
distancia = abs(p.get("sma_fast", 10) - 20) / 20.0
# Fora do optimo o retorno esperado fica negativo, nao apenas nulo: uma serie
# de media zero tem Sharpe aleatorio e passaria o gate por acaso de vez em
# quando, o que tornaria os testes intermitentes por um motivo real e chato.
mu = 0.0010 * (1.0 - distancia) * forca()
# A janela de treino e mais generosa do que a de validacao, como na vida.
if a.start < "2022-01-01":
    mu *= 1.4
# Semente estavel: hash() de strings varia entre processos (PYTHONHASHSEED),
# o que tornaria este backtest falso nao-deterministico e os testes intermitentes.
semente = int(round(mu, 6) * 10**7) + sum(ord(c) for c in a.start)
rng = random.Random(semente)
rets = [rng.gauss(mu, 0.01) for _ in range(600)]
equity, pico, dd = 1.0, 1.0, 0.0
for r in rets:
    equity *= 1 + r
    pico = max(pico, equity)
    dd = max(dd, (pico - equity) / pico)
pathlib.Path(a.out).write_text(json.dumps({
    "returns": rets,
    "trades": 420,
    "max_drawdown": dd,
    "periods_per_year": 252,
    "janela": [a.start, a.end],
}))
print("backtest ok", a.start, a.end)
'''


ESTRATEGIA_FALSA = """\
def forca():
    \"\"\"Multiplicador do sinal. E este ficheiro que o agente pode alterar.\"\"\"
    return 1.0
"""

TESTE_DO_ALVO = """\
from estrategia.sinal import forca


def test_forca_e_positiva():
    assert forca() > 0
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def alvo(tmp_path):
    """Repositorio git com um backtest falso mas funcional."""
    repo = tmp_path / "alvo"
    (repo / "data").mkdir(parents=True)
    (repo / "run_backtest.py").write_text(BACKTEST_FALSO, encoding="utf-8")
    (repo / "estrategia").mkdir()
    (repo / "estrategia" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "estrategia" / "sinal.py").write_text(ESTRATEGIA_FALSA, encoding="utf-8")
    (repo / "testes").mkdir()
    (repo / "testes" / "test_estrategia.py").write_text(TESTE_DO_ALVO, encoding="utf-8")
    (repo / "params.json").write_text(
        json.dumps({"sma_fast": 8, "sma_slow": 60, "stop_atr": 2.0}) + "\n", encoding="utf-8"
    )
    (repo / "data" / "candles.csv").write_text("dados\n", encoding="utf-8")
    (repo / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "run_backtest.py", "params.json", ".gitignore", "estrategia", "testes")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "inicial")
    return repo


@pytest.fixture
def config(tmp_path, alvo, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TOKEN-DE-TESTE")
    corpo = {
        "telegram": {"allowed_chat_ids": [42], "poll_timeout_sec": 1},
        "llm": {
            "provider": "fake",
            "models": {"research": "m", "proposer": "m", "report": "m"},
            "max_json_retries": 2,
        },
        "target": {
            "path": str(alvo),
            "backtest_cmd": (
                "python3 run_backtest.py --params {params_file} "
                "--start {start} --end {end} --out {metrics_file}"
            ),
            "timeout_sec": 120,
            "network": False,
            "link_paths": ["data"],
            "params_file": "params.json",
        },
        "experiment": {
            "mode": "params",
            "params_schema": {
                "sma_fast": {"type": "int", "min": 2, "max": 50},
                "sma_slow": {"type": "int", "min": 10, "max": 300},
                "stop_atr": {"type": "float", "min": 0.5, "max": 6.0},
            },
        },
        "protocol": {
            "train_start": "2015-01-01",
            "train_end": "2021-12-31",
            "validation_start": "2022-01-01",
            "validation_end": "2023-12-31",
            "holdout_start": "2024-01-01",
            "holdout_end": "2025-12-31",
            "max_trials_per_study": 10,
        },
        "gate": {"min_trades": 100, "min_oos_sharpe": 0.5, "min_dsr": 0.90},
        "storage": {
            "db_path": str(tmp_path / "orq.db"),
            "worktrees_dir": str(tmp_path / "wt"),
            "log_dir": str(tmp_path / "logs"),
        },
    }
    caminho = tmp_path / "config.yaml"
    caminho.write_text(yaml.safe_dump(corpo), encoding="utf-8")
    return load_config(caminho, env_path=tmp_path / ".env-inexistente")


@pytest.fixture
def store(config):
    with Store(config.storage.db_path) as s:
        yield s


@pytest.fixture
def config_code(tmp_path, alvo, monkeypatch):
    """Config em modo `code`: o agente altera a estrategia, nunca o arnes."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TOKEN-DE-TESTE")
    corpo = {
        "telegram": {"allowed_chat_ids": [42], "poll_timeout_sec": 1},
        "llm": {
            "provider": "fake",
            "models": {"research": "m", "proposer": "m", "report": "m", "coder": "m"},
            "max_json_retries": 2,
        },
        "target": {
            "path": str(alvo),
            "backtest_cmd": (
                "python3 run_backtest.py --params {params_file} "
                "--start {start} --end {end} --out {metrics_file}"
            ),
            "test_cmd": "python3 -m pytest testes -q",
            "timeout_sec": 120,
            "network": False,
            "link_paths": ["data"],
            "params_file": "params.json",
            # run_backtest.py fica deliberadamente de fora.
            "editable_paths": ["estrategia"],
        },
        "experiment": {
            "mode": "code",
            "max_edit_lines": 40,
            "params_schema": {"sma_fast": {"type": "int", "min": 2, "max": 50}},
        },
        "protocol": {
            "train_start": "2015-01-01",
            "train_end": "2021-12-31",
            "validation_start": "2022-01-01",
            "validation_end": "2023-12-31",
            "holdout_start": "2024-01-01",
            "holdout_end": "2025-12-31",
            "max_trials_per_study": 10,
        },
        "gate": {"min_trades": 100, "min_oos_sharpe": 0.5, "min_dsr": 0.90},
        "storage": {
            "db_path": str(tmp_path / "orq_code.db"),
            "worktrees_dir": str(tmp_path / "wtc"),
            "log_dir": str(tmp_path / "logsc"),
        },
    }
    caminho = tmp_path / "config_code.yaml"
    caminho.write_text(yaml.safe_dump(corpo), encoding="utf-8")
    return load_config(caminho, env_path=tmp_path / ".env-inexistente")


@pytest.fixture
def store_code(config_code):
    with Store(config_code.storage.db_path) as s:
        yield s
