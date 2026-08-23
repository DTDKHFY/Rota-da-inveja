"""Cliente HTTP do Telegram. Sem SDK — a API de bots sao quatro endpoints.

Nota sobre Markdown: o parser do Telegram rebenta com asteriscos e underscores
soltos, e parte do texto que enviamos vem de um LLM. Em vez de tentar escapar
tudo (e falhar num caso raro, em producao, a meio da noite), tentamos com
formatacao e, se o Telegram recusar, reenviamos em texto simples. Perde-se o
negrito; nao se perde a mensagem.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("orq.telegram")


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, *, timeout: int = 40):
        if not token or ":" not in token:
            raise TelegramError(
                "token do Telegram invalido. Deve ser algo como 123456:ABC-DEF... "
                "Poe o valor em TELEGRAM_BOT_TOKEN no .env."
            )
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self._session = requests.Session()

    def _call(self, method: str, **payload: Any) -> Any:
        try:
            response = self._session.post(
                f"{self.base}/{method}", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TelegramError(f"{method}: falha de rede: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: resposta nao-JSON ({response.status_code})") from exc
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', body)}")
        return body.get("result")

    # -- envio -----------------------------------------------------------
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "Markdown",
    ) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            return self._call("sendMessage", **payload)
        except TelegramError as exc:
            if parse_mode is None:
                raise
            log.warning("Markdown recusado, reenvio em texto simples: %s", exc)
            payload.pop("parse_mode", None)
            return self._call("sendMessage", **payload)

    def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        try:
            self._call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup or {"inline_keyboard": []},
            )
        except TelegramError as exc:
            # Botoes ja removidos ou mensagem antiga demais. Nao vale um crash.
            log.debug("editMessageReplyMarkup ignorado: %s", exc)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self._call(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text=text[:200],
                show_alert=alert,
            )
        except TelegramError as exc:
            log.debug("answerCallbackQuery ignorado: %s", exc)

    # -- recepcao --------------------------------------------------------
    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            response = self._session.post(
                f"{self.base}/getUpdates", json=payload, timeout=timeout + 15
            )
            body = response.json()
        except requests.RequestException as exc:
            raise TelegramError(f"getUpdates: falha de rede: {exc}") from exc
        except ValueError as exc:
            raise TelegramError("getUpdates: resposta nao-JSON") from exc
        if not body.get("ok"):
            raise TelegramError(f"getUpdates: {body.get('description', body)}")
        return body.get("result", [])

    def get_me(self) -> dict:
        return self._call("getMe")


def approval_keyboard(exp_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aplicar (ramo novo)", "callback_data": f"ap:{exp_id}"},
                {"text": "❌ Descartar", "callback_data": f"rj:{exp_id}"},
            ],
            [{"text": "🔍 Correr holdout", "callback_data": f"ho:{exp_id}"}],
        ]
    }


def holdout_confirm_keyboard(exp_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Sim, queimar o holdout", "callback_data": f"hc:{exp_id}"},
                {"text": "Cancelar", "callback_data": f"hx:{exp_id}"},
            ]
        ]
    }
