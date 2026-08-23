"""O bot: a tua unica porta de entrada, e o unico sitio onde decides.

Politica de acesso: allowlist por chat_id. Uma mensagem de qualquer outro chat
e ignorada em silencio e registada. Um bot do Telegram e descoberto por
acidente com mais frequencia do que se pensa, e o que esta do outro lado sabe
mexer no teu codigo.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from ..config import Config
from ..orchestrator import Orchestrator
from ..sandbox import SandboxError
from ..store import Store
from .client import (
    TelegramClient,
    TelegramError,
    approval_keyboard,
    holdout_confirm_keyboard,
)

log = logging.getLogger("orq.bot")

AJUDA = """*Orquestrador de backtest*

Manda-me uma tarefa em texto normal, por exemplo:
_reduzir o drawdown sem perder mais de 10% de retorno_

*Comandos*
/estado — estudo atual, fila, ensaios gastos
/ensaios — ultimos ensaios e o que deram
/baseline — mede a estrategia atual (referencia de comparacao)
/estudo <objetivo> — fecha o estudo atual e abre um novo
/aprovar <id> — aplica uma proposta (cria um ramo git)
/rejeitar <id> — descarta uma proposta
/holdout <id> — corre o holdout (uma unica vez, pensa antes)
/parar — cancela o que estiver em fila
/ajuda — isto

*O que eu nao faco*
Nao faco merge. Uma proposta aprovada vai para um ramo git novo; es tu que
olhas para o diff e decides. Nao corro nada sobre o holdout sem ordem tua.
"""


class TelegramNotifier:
    """Envia para o teu chat. Usado pelo worker e pelo orquestrador."""

    def __init__(self, client: TelegramClient, chat_id: int):
        self.client = client
        self.chat_id = chat_id
        self._lock = threading.Lock()

    def send(self, text: str) -> None:
        with self._lock:
            try:
                self.client.send_message(self.chat_id, text)
            except TelegramError as exc:
                log.error("nao consegui enviar mensagem: %s", exc)

    def ask_approval(self, text: str, exp_id: str) -> None:
        with self._lock:
            try:
                self.client.send_message(
                    self.chat_id, text, reply_markup=approval_keyboard(exp_id)
                )
            except TelegramError as exc:
                log.error("nao consegui pedir aprovacao: %s", exc)


class Bot:
    def __init__(
        self,
        config: Config,
        store: Store,
        orchestrator: Orchestrator,
        client: TelegramClient,
        *,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self.store = store
        self.orq = orchestrator
        self.client = client
        self.stop_event = stop_event or threading.Event()

    # ------------------------------------------------------------------
    def run(self) -> None:
        offset_gravado = self.store.kv_get("telegram_offset")
        offset = int(offset_gravado) if offset_gravado else None
        log.info("bot a ouvir (offset %s)", offset)

        while not self.stop_event.is_set():
            try:
                updates = self.client.get_updates(offset, self.config.telegram.poll_timeout_sec)
            except TelegramError as exc:
                log.warning("getUpdates falhou, tento outra vez: %s", exc)
                self.stop_event.wait(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                self.store.kv_set("telegram_offset", str(offset))
                try:
                    self._dispatch(update)
                except Exception:  # noqa: BLE001 - uma mensagem ma nao derruba o bot
                    log.exception("erro a tratar update %s", update.get("update_id"))

    # ------------------------------------------------------------------
    def _autorizado(self, chat_id: int) -> bool:
        if self.config.telegram.is_allowed(chat_id):
            return True
        log.warning("mensagem ignorada de chat nao autorizado: %s", chat_id)
        self.store.log_event("telegram.unauthorized", None, chat_id=chat_id)
        return False

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message or "text" not in message:
            return
        chat_id = message["chat"]["id"]
        if not self._autorizado(chat_id):
            return
        self._on_text(chat_id, message["text"].strip())

    # ------------------------------------------------------------------
    def _on_text(self, chat_id: int, texto: str) -> None:
        if not texto.startswith("/"):
            task_id = self.store.enqueue_task(chat_id, texto)
            self._reply(chat_id, f"📥 Tarefa aceite: `{task_id}`\nVou pensar e enfileirar ensaios.")
            return

        comando, _, argumento = texto.partition(" ")
        comando = comando.lstrip("/").split("@")[0].lower()
        argumento = argumento.strip()

        handlers = {
            "start": lambda: self._reply(chat_id, AJUDA),
            "ajuda": lambda: self._reply(chat_id, AJUDA),
            "help": lambda: self._reply(chat_id, AJUDA),
            "estado": lambda: self._cmd_estado(chat_id),
            "status": lambda: self._cmd_estado(chat_id),
            "ensaios": lambda: self._cmd_ensaios(chat_id),
            "tarefa": lambda: self._cmd_tarefa(chat_id, argumento),
            "estudo": lambda: self._cmd_estudo(chat_id, argumento),
            "baseline": lambda: self._cmd_baseline(chat_id),
            "aprovar": lambda: self._decidir(chat_id, argumento, aprovar=True),
            "rejeitar": lambda: self._decidir(chat_id, argumento, aprovar=False),
            "holdout": lambda: self._cmd_holdout_pedido(chat_id, argumento),
            "parar": lambda: self._cmd_parar(chat_id),
        }
        handler = handlers.get(comando)
        if handler is None:
            self._reply(chat_id, f"Nao conheco /{comando}. Manda /ajuda.")
            return
        handler()

    def _reply(self, chat_id: int, texto: str, **kwargs) -> None:
        try:
            self.client.send_message(chat_id, texto, **kwargs)
        except TelegramError as exc:
            log.error("falha a responder: %s", exc)

    # -- comandos --------------------------------------------------------
    def _cmd_tarefa(self, chat_id: int, argumento: str) -> None:
        if not argumento:
            self._reply(chat_id, "Usa: /tarefa <o que queres que eu investigue>")
            return
        task_id = self.store.enqueue_task(chat_id, argumento)
        self._reply(chat_id, f"📥 Tarefa aceite: `{task_id}`")

    def _cmd_estado(self, chat_id: int) -> None:
        study = self.store.open_study()
        linhas = ["*Estado*"]
        if study is None:
            linhas.append("Nenhum estudo aberto. Manda-me uma tarefa para abrir um.")
        else:
            usados = self.store.trial_count(study["id"])
            orcamento = self.config.protocol.max_trials_per_study
            linhas += [
                f"Estudo: `{study['id']}`",
                f"Objetivo: {study['goal']}",
                f"Ensaios: {usados}/{orcamento}",
                f"Baseline: {'definida' if study['baseline'] else 'POR DEFINIR (corre /baseline)'}",
            ]
        em_fila = [t for t in self.store.list_tasks(50) if t["status"] in ("queued", "running")]
        pendentes = self.store.pending_approvals()
        linhas.append(f"Tarefas em curso: {len(em_fila)}")
        linhas.append(f"A aguardar decisao tua: {len(pendentes)}")
        for p in pendentes[:5]:
            linhas.append(f"  • `{p['id']}`")
        linhas.append(
            f"\nHoldout: {self.config.protocol.holdout_start} a "
            f"{self.config.protocol.holdout_end} — intocado por ensaios automaticos."
        )
        self._reply(chat_id, "\n".join(linhas))

    def _cmd_ensaios(self, chat_id: int) -> None:
        study = self.store.open_study()
        ensaios = self.store.list_experiments(study["id"] if study else None, limit=10)
        if not ensaios:
            self._reply(chat_id, "Ainda nao ha ensaios.")
            return
        linhas = ["*Ultimos ensaios*"]
        for e in ensaios:
            if e["status"] != "done":
                linhas.append(f"`{e['id']}` — {e['status']}")
                continue
            metrics = json.loads(e["metrics"]) if e["metrics"] else {}
            sharpe = metrics.get("validation", {}).get("sharpe_anualizado")
            verdict = json.loads(e["verdict"]) if e["verdict"] else {}
            marca = "🟢" if verdict.get("passed") else "🔴"
            sharpe_txt = f"{sharpe:.2f}" if sharpe is not None else "?"
            linhas.append(
                f"{marca} `{e['id']}` Sharpe OOS {sharpe_txt} "
                f"DSR {verdict.get('dsr', 0):.2f} [{e['approval']}]"
            )
        self._reply(chat_id, "\n".join(linhas))

    def _cmd_estudo(self, chat_id: int, argumento: str) -> None:
        if not argumento:
            self._reply(chat_id, "Usa: /estudo <objetivo do novo estudo>")
            return
        atual = self.store.open_study()
        if atual:
            self.store.close_study(atual["id"], "fechado por ordem do utilizador")
        study_id = self.store.create_study(name=argumento[:60], goal=argumento)
        self._reply(
            chat_id,
            f"📚 Estudo novo: `{study_id}`\nContagem de ensaios reiniciada.\n\n"
            "Lembra-te que reiniciar a contagem so e honesto se a hipotese for "
            "mesmo nova. Se e a mesma busca a continuar, o DSR do estudo novo "
            "esta a mentir-te por omissao.",
        )

    def _cmd_baseline(self, chat_id: int) -> None:
        study = self.store.open_study()
        if study is None:
            self._reply(chat_id, "Nao ha estudo aberto. Manda-me uma tarefa primeiro.")
            return
        params_file = self.config.target.path / self.config.target.params_file
        if not params_file.is_file():
            self._reply(
                chat_id,
                f"Nao encontrei `{self.config.target.params_file}` no projeto-alvo. "
                "E dai que leio os parametros atuais para medir a baseline.",
            )
            return
        self._reply(chat_id, "⏳ A medir a baseline na janela de validacao...")
        try:
            params = json.loads(params_file.read_text(encoding="utf-8"))
            metrics = self.orq.measure_baseline(params, study["id"])
        except (SandboxError, json.JSONDecodeError, ValueError) as exc:
            self._reply(chat_id, f"⚠️ Baseline falhou: {exc}")
            return
        self._reply(
            chat_id,
            f"📏 *Baseline definida*\n"
            f"Sharpe OOS: {metrics.sharpe_annualised:.2f}\n"
            f"Drawdown: {metrics.max_drawdown * 100:.1f}%\n"
            f"Trades: {metrics.trades}\n\n"
            f"E este numero que qualquer proposta tem de bater em pelo menos "
            f"{self.config.gate.require_oos_improvement_pct:.0f}%.",
        )

    def _cmd_parar(self, chat_id: int) -> None:
        cancelados = self.store.cancel_queued_tasks()
        self._reply(
            chat_id,
            f"🛑 {cancelados} tarefas canceladas. "
            "O ensaio que ja estava a correr vai ate ao fim.",
        )

    def _cmd_holdout_pedido(self, chat_id: int, exp_id: str) -> None:
        if not exp_id:
            self._reply(chat_id, "Usa: /holdout <id do ensaio>")
            return
        self._reply(chat_id, self._texto_aviso_holdout(exp_id),
                    reply_markup=holdout_confirm_keyboard(exp_id))

    def _texto_aviso_holdout(self, exp_id: str) -> str:
        p = self.config.protocol
        return (
            f"⚠️ *Correr o holdout em* `{exp_id}`\n\n"
            f"Janela: {p.holdout_start} a {p.holdout_end}\n\n"
            "Isto so se faz uma vez. A partir do momento em que olhas para o "
            "resultado, o holdout passou a fazer parte da tua escolha e deixa "
            "de ser uma medida independente. Nao ha como o repor.\n\n"
            "So o faz quando ja tiveres decidido que e este o candidato."
        )

    def _decidir(self, chat_id: int, exp_id: str, *, aprovar: bool) -> None:
        if not exp_id:
            self._reply(chat_id, "Usa: /aprovar <id> ou /rejeitar <id>")
            return
        exp = self.store.get_experiment(exp_id)
        if exp is None:
            self._reply(chat_id, f"Nao encontro `{exp_id}`.")
            return
        if exp["approval"] not in ("pending", "approved"):
            self._reply(chat_id, f"`{exp_id}` nao esta a aguardar decisao (esta: {exp['approval']}).")
            return

        if not aprovar:
            self.store.set_approval(exp_id, "rejected")
            self._reply(chat_id, f"❌ `{exp_id}` descartado.")
            return

        self.store.set_approval(exp_id, "approved")
        try:
            ramo = self.orq.apply_approved(exp_id)
        except (SandboxError, ValueError) as exc:
            self._reply(chat_id, f"⚠️ Nao consegui aplicar: {exc}")
            return
        self._reply(
            chat_id,
            f"✅ Escrito no ramo `{ramo}` do teu projeto.\n\n"
            f"Nao fiz merge. Ve o diff e decide:\n"
            f"`git diff main..{ramo}`\n"
            f"`git merge {ramo}`",
        )

    # -- botoes ----------------------------------------------------------
    def _on_callback(self, callback: dict) -> None:
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        callback_id = callback["id"]
        if not self._autorizado(chat_id):
            self.client.answer_callback(callback_id, "Nao autorizado.", alert=True)
            return

        acao, _, exp_id = callback.get("data", "").partition(":")

        if acao == "ap":
            self.client.answer_callback(callback_id, "A aplicar...")
            self.client.edit_reply_markup(chat_id, message_id, None)
            self._decidir(chat_id, exp_id, aprovar=True)
        elif acao == "rj":
            self.client.answer_callback(callback_id, "Descartado.")
            self.client.edit_reply_markup(chat_id, message_id, None)
            self._decidir(chat_id, exp_id, aprovar=False)
        elif acao == "ho":
            self.client.answer_callback(callback_id)
            self._reply(chat_id, self._texto_aviso_holdout(exp_id),
                        reply_markup=holdout_confirm_keyboard(exp_id))
        elif acao == "hx":
            self.client.answer_callback(callback_id, "Cancelado.")
            self.client.edit_reply_markup(chat_id, message_id, None)
        elif acao == "hc":
            self.client.answer_callback(callback_id, "A correr o holdout...")
            self.client.edit_reply_markup(chat_id, message_id, None)
            self._executar_holdout(chat_id, exp_id)
        else:
            self.client.answer_callback(callback_id, "Botao desconhecido.")

    def _executar_holdout(self, chat_id: int, exp_id: str) -> None:
        try:
            metrics = self.orq.run_holdout(exp_id)
        except (SandboxError, ValueError) as exc:
            self._reply(chat_id, f"⚠️ Holdout nao correu: {exc}")
            return
        exp = self.store.get_experiment(exp_id)
        oos = json.loads(exp["metrics"]).get("validation", {}) if exp["metrics"] else {}
        sharpe_val = oos.get("sharpe_anualizado", 0.0)
        queda = metrics.sharpe_annualised - sharpe_val
        veredito = (
            "aguentou" if queda > -0.5 else "caiu bastante face a validacao — desconfia"
        )
        self._reply(
            chat_id,
            f"🔍 *Holdout de* `{exp_id}` *(queimado)*\n"
            f"Sharpe: {metrics.sharpe_annualised:.2f} "
            f"(validacao era {sharpe_val:.2f}, {queda:+.2f})\n"
            f"Drawdown: {metrics.max_drawdown * 100:.1f}%\n"
            f"Trades: {metrics.trades}\n\n"
            f"Leitura: {veredito}.\n\n"
            f"Este holdout esta gasto. Para outra medicao independente precisas "
            f"de dados que ainda nao existiam quando fizeste esta busca.",
        )
