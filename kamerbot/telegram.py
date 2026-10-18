"""Meldingen naar Telegram via de Bot API."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def stuur(self, tekst: str) -> bool:
        """Geeft True als het bericht is aangekomen."""
        if not (self.token and self.chat_id):
            log.info("Telegram niet ingesteld, bericht:\n%s", tekst)
            return False
        r = httpx.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": tekst, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code != 200:
            log.error("Telegram fout %s: %s", r.status_code, r.text[:200])
            return False
        return True
