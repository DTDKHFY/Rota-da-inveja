"""O worker: consome a fila e nao guarda nada em memoria.

E propositadamente simples e propositadamente reiniciavel. Pode ser morto a
meio de um backtest de 40 minutos e, ao voltar, `recover_stale` devolve a fila
o que ficou pendurado. O estado esta todo no SQLite.
"""
from __future__ import annotations

import logging
import threading

from .orchestrator import Orchestrator
from .store import Store

log = logging.getLogger("orq.worker")

# Um ensaio que nao da sinal de vida ha mais tempo do que isto foi
# interrompido por um crash, nao esta so lento: o timeout do proprio backtest
# e mais curto, e ha heartbeat entre janelas.
STALE_FACTOR = 3


class Worker:
    def __init__(
        self,
        orchestrator: Orchestrator,
        store: Store,
        *,
        idle_sleep: float = 2.0,
        stop_event: threading.Event | None = None,
    ):
        self.orq = orchestrator
        self.store = store
        self.idle_sleep = idle_sleep
        self.stop_event = stop_event or threading.Event()

    def recover(self) -> None:
        stale_after = self.orq.config.target.timeout_sec * STALE_FACTOR
        recovered = self.store.recover_stale(stale_after)
        if recovered["experiments"] or recovered["tasks"]:
            log.warning(
                "recuperados apos crash: %s ensaios, %s tarefas",
                recovered["experiments"], recovered["tasks"],
            )
            self.orq.notifier.send(
                f"♻️ Retomei depois de uma paragem: "
                f"{recovered['experiments']} ensaios e {recovered['tasks']} tarefas "
                f"voltaram a fila."
            )

    def step(self) -> bool:
        """Faz uma unidade de trabalho. Devolve False se nao havia nada a fazer.

        As tarefas tem prioridade sobre os ensaios: uma ordem tua nova vale mais
        do que acabar a busca anterior.
        """
        task = self.store.claim_task()
        if task is not None:
            try:
                enfileirados = self.orq.handle_task(task)
                self.store.finish_task(task["id"], "done", result=f"{enfileirados} ensaios")
            except Exception as exc:  # noqa: BLE001 - o worker nao pode morrer por uma tarefa ma
                log.exception("tarefa %s rebentou", task["id"])
                self.store.finish_task(task["id"], "failed", error=str(exc))
                self.orq.notifier.send(f"⚠️ Tarefa falhou: {exc}")
            return True

        exp = self.store.claim_experiment()
        if exp is not None:
            try:
                self.orq.run_experiment(exp)
            except Exception as exc:  # noqa: BLE001
                log.exception("ensaio %s rebentou", exp["id"])
                self.store.finish_experiment(exp["id"], status="failed", error=str(exc))
                self.orq.notifier.send(f"⚠️ Ensaio `{exp['id']}` rebentou: {exc}")
            return True

        return False

    def run(self) -> None:
        self.recover()
        log.info("worker a correr")
        while not self.stop_event.is_set():
            try:
                if not self.step():
                    self.stop_event.wait(self.idle_sleep)
            except Exception:  # noqa: BLE001
                log.exception("erro no ciclo do worker")
                self.stop_event.wait(self.idle_sleep)
        log.info("worker parado")
