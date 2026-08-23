"""Estado duravel em SQLite.

Motivo de existir: o orquestrador vai morrer a meio de um backtest de 40
minutos — falta de luz, OOM, `Ctrl+C` sem querer. Nada pode viver so em
memoria. A fila, os ensaios e os vereditos ficam todos em disco, e um worker
que reinicia retoma de onde ficou.

Tambem serve de registo de trials: para corrigir multiple testing e preciso
saber quantas configuracoes foram testadas num estudo, e isso so e fiavel se
for gravado no momento, nao reconstruido depois.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    goal          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | closed
    baseline      TEXT,                            -- JSON das metricas de referencia
    created_at    REAL NOT NULL,
    closed_at     REAL,
    closed_reason TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    study_id    TEXT REFERENCES studies(id),
    chat_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    heartbeat   REAL,
    result      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS experiments (
    id            TEXT PRIMARY KEY,
    study_id      TEXT NOT NULL REFERENCES studies(id),
    task_id       TEXT REFERENCES tasks(id),
    hypothesis    TEXT,
    params        TEXT NOT NULL,                  -- JSON
    diff          TEXT,                            -- modo `code`: patch proposto
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    approval      TEXT NOT NULL DEFAULT 'none',    -- none | pending | approved | rejected
    metrics       TEXT,                            -- JSON {train:{}, validation:{}}
    holdout       TEXT,                            -- JSON, so preenchido por ordem manual
    verdict       TEXT,                            -- JSON do gate
    stdout_tail   TEXT,
    error         TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    heartbeat     REAL
);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status, created_at);
CREATE INDEX IF NOT EXISTS idx_exp_study ON experiments(study_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT,
    task_id       TEXT,
    role          TEXT NOT NULL,
    model         TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 1,
    duration_ms   INTEGER,
    error         TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    ref_id     TEXT,
    payload    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Transacao imediata: garante que dois workers nao reclamam a mesma linha."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- eventos ---------------------------------------------------------
    def log_event(self, kind: str, ref_id: str | None = None, **payload: Any) -> None:
        self._conn.execute(
            "INSERT INTO events (kind, ref_id, payload, created_at) VALUES (?,?,?,?)",
            (kind, ref_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    # -- kv --------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- estudos ---------------------------------------------------------
    def create_study(self, name: str, goal: str, baseline: dict | None = None) -> str:
        study_id = new_id("std")
        self._conn.execute(
            "INSERT INTO studies (id, name, goal, baseline, created_at) VALUES (?,?,?,?,?)",
            (study_id, name, goal, json.dumps(baseline) if baseline else None, time.time()),
        )
        self.log_event("study.created", study_id, name=name, goal=goal)
        return study_id

    def get_study(self, study_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()

    def open_study(self) -> sqlite3.Row | None:
        """O estudo aberto mais recente, se houver."""
        return self._conn.execute(
            "SELECT * FROM studies WHERE status='open' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    def close_study(self, study_id: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE studies SET status='closed', closed_at=?, closed_reason=? WHERE id=?",
            (time.time(), reason, study_id),
        )
        self.log_event("study.closed", study_id, reason=reason)

    def set_baseline(self, study_id: str, baseline: dict) -> None:
        self._conn.execute(
            "UPDATE studies SET baseline=? WHERE id=?",
            (json.dumps(baseline, ensure_ascii=False), study_id),
        )
        self.log_event("study.baseline", study_id)

    def trial_count(self, study_id: str) -> int:
        """Quantos ensaios ja correram neste estudo.

        E este numero que entra na correcao de multiple testing: o melhor de N
        tentativas e inflacionado por construcao, e sem N nao ha como corrigir.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM experiments WHERE study_id=? AND status='done'",
            (study_id,),
        ).fetchone()
        return int(row["n"])

    # -- tarefas ---------------------------------------------------------
    def enqueue_task(self, chat_id: int, text: str, study_id: str | None = None) -> str:
        task_id = new_id("tsk")
        self._conn.execute(
            "INSERT INTO tasks (id, study_id, chat_id, text, created_at) VALUES (?,?,?,?,?)",
            (task_id, study_id, chat_id, text, time.time()),
        )
        self.log_event("task.queued", task_id, chat_id=chat_id, text=text)
        return task_id

    def claim_task(self) -> sqlite3.Row | None:
        """Reclama atomicamente a proxima tarefa da fila."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            conn.execute(
                "UPDATE tasks SET status='running', started_at=?, heartbeat=? WHERE id=?",
                (now, now, row["id"]),
            )
            return conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()

    def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET status=?, finished_at=?, result=?, error=? WHERE id=?",
            (status, time.time(), result, error, task_id),
        )
        self.log_event("task.finished", task_id, status=status, error=error)

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        )

    def cancel_queued_tasks(self) -> int:
        cur = self._conn.execute(
            "UPDATE tasks SET status='cancelled', finished_at=? WHERE status='queued'",
            (time.time(),),
        )
        self.log_event("task.cancelled_all", None, count=cur.rowcount)
        return cur.rowcount

    # -- ensaios ---------------------------------------------------------
    def enqueue_experiment(
        self,
        study_id: str,
        params: dict,
        hypothesis: str = "",
        task_id: str | None = None,
        diff: str | None = None,
    ) -> str:
        exp_id = new_id("exp")
        self._conn.execute(
            "INSERT INTO experiments (id, study_id, task_id, hypothesis, params, diff, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                exp_id,
                study_id,
                task_id,
                hypothesis,
                json.dumps(params, ensure_ascii=False, sort_keys=True),
                diff,
                time.time(),
            ),
        )
        self.log_event("experiment.queued", exp_id, study_id=study_id, hypothesis=hypothesis)
        return exp_id

    def claim_experiment(self) -> sqlite3.Row | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            conn.execute(
                "UPDATE experiments SET status='running', started_at=?, heartbeat=? WHERE id=?",
                (now, now, row["id"]),
            )
            return conn.execute("SELECT * FROM experiments WHERE id=?", (row["id"],)).fetchone()

    def heartbeat_experiment(self, exp_id: str) -> None:
        self._conn.execute(
            "UPDATE experiments SET heartbeat=? WHERE id=?", (time.time(), exp_id)
        )

    def finish_experiment(
        self,
        exp_id: str,
        *,
        status: str,
        metrics: dict | None = None,
        verdict: dict | None = None,
        approval: str | None = None,
        stdout_tail: str | None = None,
        error: str | None = None,
    ) -> None:
        fields = ["status=?", "finished_at=?"]
        values: list[Any] = [status, time.time()]
        if metrics is not None:
            fields.append("metrics=?")
            values.append(json.dumps(metrics, ensure_ascii=False))
        if verdict is not None:
            fields.append("verdict=?")
            values.append(json.dumps(verdict, ensure_ascii=False))
        if approval is not None:
            fields.append("approval=?")
            values.append(approval)
        if stdout_tail is not None:
            fields.append("stdout_tail=?")
            values.append(stdout_tail)
        if error is not None:
            fields.append("error=?")
            values.append(error)
        values.append(exp_id)
        self._conn.execute(f"UPDATE experiments SET {', '.join(fields)} WHERE id=?", values)
        self.log_event("experiment.finished", exp_id, status=status, approval=approval)

    def set_approval(self, exp_id: str, approval: str) -> None:
        self._conn.execute("UPDATE experiments SET approval=? WHERE id=?", (approval, exp_id))
        self.log_event("experiment.approval", exp_id, approval=approval)

    def set_holdout(self, exp_id: str, holdout: dict) -> None:
        self._conn.execute(
            "UPDATE experiments SET holdout=? WHERE id=?",
            (json.dumps(holdout, ensure_ascii=False), exp_id),
        )
        self.log_event("experiment.holdout", exp_id)

    def get_experiment(self, exp_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()

    def list_experiments(self, study_id: str | None = None, limit: int = 10) -> list[sqlite3.Row]:
        if study_id:
            return list(
                self._conn.execute(
                    "SELECT * FROM experiments WHERE study_id=? ORDER BY created_at DESC LIMIT ?",
                    (study_id, limit),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        )

    def pending_approvals(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM experiments WHERE approval='pending' ORDER BY created_at"
            )
        )

    # -- recuperacao apos crash -----------------------------------------
    def recover_stale(self, stale_after_sec: float) -> dict[str, int]:
        """Devolve a fila o que ficou 'running' de um worker que morreu.

        Chamado no arranque do worker. Sem isto, um crash deixa a linha presa em
        'running' para sempre e a fila para em silencio.
        """
        cutoff = time.time() - stale_after_sec
        with self._tx() as conn:
            exps = conn.execute(
                "UPDATE experiments SET status='queued', started_at=NULL, heartbeat=NULL "
                "WHERE status='running' AND (heartbeat IS NULL OR heartbeat < ?)",
                (cutoff,),
            ).rowcount
            tasks = conn.execute(
                "UPDATE tasks SET status='queued', started_at=NULL, heartbeat=NULL "
                "WHERE status='running' AND (heartbeat IS NULL OR heartbeat < ?)",
                (cutoff,),
            ).rowcount
        if exps or tasks:
            self.log_event("recover.stale", None, experiments=exps, tasks=tasks)
        return {"experiments": exps, "tasks": tasks}

    # -- registo de agentes ---------------------------------------------
    def log_agent_run(
        self,
        role: str,
        model: str,
        ok: bool,
        *,
        experiment_id: str | None = None,
        task_id: str | None = None,
        attempts: int = 1,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO agent_runs (id, experiment_id, task_id, role, model, ok, attempts, "
            "duration_ms, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("run"),
                experiment_id,
                task_id,
                role,
                model,
                int(ok),
                attempts,
                duration_ms,
                error,
                time.time(),
            ),
        )
