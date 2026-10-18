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

    def berichten(self, offset: int, wacht_s: int = 30) -> list[dict]:
        """Nieuwe berichten via long polling; leeg bij een fout (de dienst probeert het gewoon opnieuw)."""
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                json={"offset": offset, "timeout": wacht_s, "allowed_updates": ["message"]},
                timeout=wacht_s + 10,
            )
            r.raise_for_status()
            return r.json().get("result", [])
        except httpx.HTTPError as e:
            log.warning("getUpdates: %s", type(e).__name__)
            return []
