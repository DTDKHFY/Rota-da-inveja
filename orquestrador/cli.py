#!/usr/bin/env python3
"""Ponto de entrada.

    python cli.py doctor    # verifica tudo antes de arrancar
    python cli.py run       # bot + worker no mesmo processo (o normal)
    python cli.py bot       # so o bot
    python cli.py worker    # so o worker
    python cli.py estado    # estado atual, sem Telegram
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from orq.config import Config, ConfigError, load_config
from orq.llm import build_provider
from orq.llm.ollama import OllamaProvider
from orq.orchestrator import NullNotifier, Orchestrator
from orq.sandbox import is_git_repo, network_isolation_available
from orq.store import Store
from orq.telegram import Bot, TelegramClient, TelegramError, TelegramNotifier
from orq.worker import Worker


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _build(config: Config, store: Store, *, notifier=None) -> Orchestrator:
    return Orchestrator(config, store, build_provider(config.llm), notifier)


# ---------------------------------------------------------------------------
def cmd_doctor(config: Config) -> int:
    """Verifica tudo o que costuma correr mal, antes de correr mal."""
    problemas = 0

    def ok(msg: str) -> None:
        print(f"  ✅ {msg}")

    def erro(msg: str) -> None:
        nonlocal problemas
        problemas += 1
        print(f"  ❌ {msg}")

    def aviso(msg: str) -> None:
        print(f"  ⚠️  {msg}")

    print("\nConfig")
    ok(f"lido de {config.source_path}")
    ok(f"chats autorizados: {list(config.telegram.allowed_chat_ids)}")

    print("\nProjeto-alvo")
    if not config.target.path.is_dir():
        erro(f"target.path nao existe: {config.target.path}")
    elif not is_git_repo(config.target.path):
        erro(f"{config.target.path} nao e repositorio git (faz `git init` + commit)")
    else:
        ok(f"{config.target.path} e repositorio git")
        params = config.target.path / config.target.params_file
        (ok if params.is_file() else aviso)(
            f"params_file: {config.target.params_file}"
            + ("" if params.is_file() else " (em falta — /baseline nao vai funcionar)")
        )

    print("\nIsolamento")
    if config.target.network:
        aviso("target.network: true — o backtest tem acesso a rede")
    elif network_isolation_available():
        ok("backtest corre sem rede (unshare disponivel)")
    else:
        aviso("nao consigo cortar a rede neste sistema; o backtest vai ter acesso")

    print("\nProtocolo")
    p = config.protocol
    ok(f"treino     {p.train_start} → {p.train_end}")
    ok(f"validacao  {p.validation_start} → {p.validation_end}")
    ok(f"holdout    {p.holdout_start} → {p.holdout_end}  (intocado)")
    ok(f"orcamento  {p.max_trials_per_study} ensaios por estudo")

    print("\nModelos")
    if config.llm.provider == "ollama":
        provider = OllamaProvider(config.llm.base_url, timeout=15)
        disponiveis = provider.available_models()
        if not disponiveis:
            erro(f"o Ollama nao respondeu em {config.llm.base_url} (esta a correr?)")
        else:
            ok(f"Ollama tem: {', '.join(disponiveis)}")
            for papel, modelo in config.llm.models.items():
                if modelo in disponiveis or any(m.startswith(modelo) for m in disponiveis):
                    ok(f"{papel}: {modelo}")
                else:
                    erro(f"{papel}: {modelo} nao esta instalado (`ollama pull {modelo}`)")
    else:
        aviso(f"provider = {config.llm.provider} (nao e o Ollama)")

    print("\nTelegram")
    try:
        eu = TelegramClient(config.telegram.token, timeout=15).get_me()
        ok(f"ligado como @{eu.get('username')}")
    except TelegramError as exc:
        erro(f"{exc}")

    print("\nParametros")
    if not config.experiment.params_schema:
        erro("experiment.params_schema esta vazio: nao ha nada para o agente propor")
    else:
        for spec in config.experiment.params_schema.values():
            ok(f"{spec.name}: {spec.type} [{spec.min:g}, {spec.max:g}]")

    print(f"\n{'Tudo pronto.' if problemas == 0 else f'{problemas} problema(s) a resolver.'}\n")
    return 0 if problemas == 0 else 1


def cmd_estado(config: Config) -> int:
    with Store(config.storage.db_path) as store:
        study = store.open_study()
        if study is None:
            print("Nenhum estudo aberto.")
        else:
            print(f"Estudo:   {study['id']}")
            print(f"Objetivo: {study['goal']}")
            print(f"Ensaios:  {store.trial_count(study['id'])}/{config.protocol.max_trials_per_study}")
            print(f"Baseline: {'definida' if study['baseline'] else 'POR DEFINIR'}")
        pendentes = store.pending_approvals()
        print(f"A aguardar decisao: {len(pendentes)}")
        for p in pendentes:
            print(f"  {p['id']}")
    return 0


def _run_threads(config: Config, *, com_bot: bool, com_worker: bool) -> int:
    stop = threading.Event()

    def parar(*_):
        print("\na parar...")
        stop.set()

    signal.signal(signal.SIGINT, parar)
    signal.signal(signal.SIGTERM, parar)

    client = TelegramClient(config.telegram.token, timeout=config.telegram.poll_timeout_sec)
    notifier = TelegramNotifier(client, config.telegram.allowed_chat_ids[0])
    threads: list[threading.Thread] = []

    if com_worker:
        # Cada thread tem o seu Store: uma ligacao SQLite nao atravessa threads.
        worker_store = Store(config.storage.db_path)
        worker = Worker(
            _build(config, worker_store, notifier=notifier), worker_store, stop_event=stop
        )
        threads.append(threading.Thread(target=worker.run, name="worker", daemon=True))

    if com_bot:
        bot_store = Store(config.storage.db_path)
        bot = Bot(
            config,
            bot_store,
            _build(config, bot_store, notifier=notifier),
            client,
            stop_event=stop,
        )
        threads.append(threading.Thread(target=bot.run, name="bot", daemon=True))

    for t in threads:
        t.start()
    print(f"a correr: {', '.join(t.name for t in threads)} — Ctrl+C para parar")
    try:
        while not stop.is_set() and any(t.is_alive() for t in threads):
            stop.wait(1)
    except KeyboardInterrupt:
        stop.set()
    for t in threads:
        t.join(timeout=10)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orq", description=__doc__)
    parser.add_argument("-c", "--config", help="caminho do config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "comando",
        choices=["doctor", "run", "bot", "worker", "estado"],
        help="doctor = verificacao previa; run = bot + worker",
    )
    args = parser.parse_args(argv)
    _logging(args.verbose)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"\n❌ Config: {exc}\n", file=sys.stderr)
        return 2

    config.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.worktrees_dir.mkdir(parents=True, exist_ok=True)
    config.storage.log_dir.mkdir(parents=True, exist_ok=True)

    if args.comando == "doctor":
        return cmd_doctor(config)
    if args.comando == "estado":
        return cmd_estado(config)
    if args.comando == "run":
        return _run_threads(config, com_bot=True, com_worker=True)
    if args.comando == "bot":
        return _run_threads(config, com_bot=True, com_worker=False)
    if args.comando == "worker":
        return _run_threads(config, com_bot=False, com_worker=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
