"""Onthoudt welke kamers al gemeld of gereageerd zijn, zodat Telegram niet dubbel piept."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class Opslag:
    def __init__(self, pad: Path):
        pad.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(pad)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kamers ("
            " bron TEXT, id TEXT, status TEXT, tijd TEXT DEFAULT CURRENT_TIMESTAMP,"
            " PRIMARY KEY (bron, id, status))"
        )

    def heeft(self, bron: str, id: str, status: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM kamers WHERE bron=? AND id=? AND status=?", (bron, id, status)).fetchone()
            is not None
        )

    def zet(self, bron: str, id: str, status: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO kamers (bron, id, status) VALUES (?, ?, ?)", (bron, id, status))
        self.db.commit()
