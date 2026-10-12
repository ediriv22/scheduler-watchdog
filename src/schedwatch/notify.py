from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


class ConsoleNotifier:
    def send(self, text: str) -> None:
        print(f"[alert] {text}")


class TelegramNotifier:
    """Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment. Nothing is stored in the repo."""

    def __init__(self) -> None:
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = os.environ["TELEGRAM_CHAT_ID"]

    def send(self, text: str) -> None:
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10).read()


def from_env() -> "ConsoleNotifier | TelegramNotifier":
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return TelegramNotifier()
    return ConsoleNotifier()
