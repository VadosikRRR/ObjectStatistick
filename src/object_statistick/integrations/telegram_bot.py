"""Telegram adapter: subscription polling and report delivery."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import telebot
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..persistence.models import SubscriberRow

LOGGER = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class TelegramNotifier:
    """Delivers one report to every persisted chat id for one project."""

    def send(self, token: str | None, chat_ids: list[int], text: str, attachments: list[str]) -> None:
        if not token or not chat_ids:
            return
        bot = telebot.TeleBot(token)
        for chat_id in chat_ids:
            try:
                bot.send_message(chat_id, text)
                for attachment in attachments:
                    path = Path(attachment)
                    if not path.is_file():
                        continue
                    with path.open("rb") as file:
                        if path.suffix.lower() in IMAGE_EXTENSIONS:
                            bot.send_photo(chat_id, file)
                        else:
                            bot.send_document(chat_id, file)
            except Exception:
                # Leave report jobs unreported so the complete delivery can be retried.
                LOGGER.exception("Telegram report delivery failed for chat_id=%s", chat_id)
                raise


class TelegramSubscriptionPoller:
    """Long-polls one Telegram bot and persists chat ids on activation/messages."""

    def __init__(self, token: str, project_id: str, project_name: str, sessions: sessionmaker[Session]) -> None:
        self._bot = telebot.TeleBot(token)
        self._project_id = project_id
        self._project_name = project_name
        self._sessions = sessions
        self._thread: threading.Thread | None = None
        self._register_handlers()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=f"telegram-{self._project_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._bot.stop_polling()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _subscribe(self, chat_id: int) -> None:
        with self._sessions() as session:
            exists = session.scalar(select(SubscriberRow).where(
                SubscriberRow.project_id == self._project_id,
                SubscriberRow.chat_id == chat_id,
            ))
            if exists is None:
                session.add(SubscriberRow(
                    project_id=self._project_id,
                    chat_id=chat_id,
                    created_at=datetime.now(timezone.utc),
                ))
                session.commit()

    def _register_handlers(self) -> None:
        @self._bot.message_handler(commands=["start"])
        def on_start(message) -> None:
            self._subscribe(message.chat.id)
            self._bot.send_message(
                message.chat.id,
                f"Вы подписаны на отчёты объекта «{self._project_name}».",
            )

        @self._bot.message_handler(func=lambda _message: True)
        def on_message(message) -> None:
            self._subscribe(message.chat.id)

    def _run(self) -> None:
        try:
            self._bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception:
            LOGGER.exception("Telegram polling stopped for project %s", self._project_id)
