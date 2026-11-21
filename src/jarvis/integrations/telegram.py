"""Phone remote: talk to JARVIS from Telegram (free). Only your own chat id is accepted.
Setup: create a bot with @BotFather, then  .\\jarvis.ps1 set-key telegram  and set telegram.allowed_chat_id
(message your bot once; JARVIS shows your chat id in the HUD/log)."""

import threading

import requests
from loguru import logger

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBridge:
    def __init__(self, token: str, allowed_chat_id: int | None, on_message, on_unknown=None):
        self.token, self.allowed = token, int(allowed_chat_id) if allowed_chat_id else None
        self.on_message, self.on_unknown = on_message, on_unknown or (lambda chat_id, name: None)
        self._stop = threading.Event()
        self._offset = 0

    def _call(self, method: str, **params):
        r = requests.post(API.format(token=self.token, method=method), json=params, timeout=40)
        r.raise_for_status()
        return r.json()["result"]

    def send(self, chat_id: int, text: str) -> None:
        try:
            self._call("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception:
            logger.exception("Telegram send failed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for upd in self._call("getUpdates", offset=self._offset, timeout=30):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text, chat = msg.get("text"), msg.get("chat", {})
                    if not text:
                        continue
                    if self.allowed and chat.get("id") == self.allowed:
                        self.on_message(chat["id"], text)
                    else:
                        logger.warning("Telegram message from unknown chat {}", chat.get("id"))
                        self.on_unknown(chat.get("id"), chat.get("first_name", ""))
                        self.send(chat.get("id"), "Sorry, I only talk to my owner.")
            except Exception as e:
                logger.debug("Telegram poll error: {}", e)
                self._stop.wait(5)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="telegram").start()

    def stop(self) -> None:
        self._stop.set()


def remote_confirm(tool, args) -> bool:
    """Actions that need approval can't be approved from the phone."""
    return False

