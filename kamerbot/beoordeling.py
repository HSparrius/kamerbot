"""Het verhaaltje bij een kamer laten lezen door Claude (Hidde, 18 okt 2026).

Twee dingen komen eruit:
- past de kamer bij je (wie zoeken ze, past dat bij je profiel) met een cijfer 0–10 en een reden;
- de tijd van de hospiteeravond, als die in de tekst staat (SSH geeft alleen een datum).

Zonder ANTHROPIC_API_KEY doet de bot dit niet en werkt alles zoals eerst. Een uitslag wordt per kamer en
per versie van het verhaaltje bewaard, zodat elke tekst maar één keer wordt gelezen.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from pydantic import BaseModel, Field

from .opslag import Opslag
from .ssh import Kamer

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"

STANDAARD_PROFIEL = (
    "Nederlandstalige mannelijke student in Utrecht, lid van een studentenroeivereniging en een heerendispuut. "
    "Zoekt een gezellig studentenhuis waar mensen samen eten en dingen doen."
)

SYSTEEM = (
    "Je beoordeelt studentenkamers in Utrecht voor één kamerzoeker. Je krijgt zijn profiel en de tekst die het huis "
    "bij de kamer heeft geschreven. Beoordeel of hij past bij wie het huis zoekt: geslacht, taal, studie of werk, "
    "leeftijd, sfeer. Een huis dat alleen vrouwen, alleen internationals of alleen werkenden zoekt, past niet. "
    "Zegt de tekst niets over wie ze zoeken, dan past hij (cijfer rond 5). "
    "Staat er een tijd voor de hospiteeravond of kennismaking, geef dan begin en eind als HH:MM (24 uur); "
    "staat er alleen een begintijd, laat het eind leeg. Verzin geen tijden."
)


class Oordeel(BaseModel):
    past: bool = Field(description="Past de kamerzoeker bij wie het huis zoekt?")
    cijfer: int = Field(description="0 = past totaal niet, 10 = precies wie ze zoeken")
    reden: str = Field(description="Eén korte zin in het Nederlands")
    hospiteer_begin: str | None = Field(description="Begintijd hospiteeravond als HH:MM, of null")
    hospiteer_eind: str | None = Field(description="Eindtijd hospiteeravond als HH:MM, of null")


_TIJD = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _schoon(o: Oordeel) -> Oordeel:
    o.cijfer = max(0, min(10, o.cijfer))
    if o.hospiteer_begin and not _TIJD.match(o.hospiteer_begin):
        o.hospiteer_begin = None
    if not o.hospiteer_begin or (o.hospiteer_eind and not _TIJD.match(o.hospiteer_eind)):
        o.hospiteer_eind = None
    return o


class Beoordelaar:
    def __init__(self, api_key: str, profiel: str, db: Opslag, client=None):
        self.profiel = profiel or STANDAARD_PROFIEL
        self.db = db.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS beoordeling (flow_id TEXT, hash TEXT, oordeel TEXT, PRIMARY KEY (flow_id, hash))"
        )
        if client is None and api_key:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120)
        self.client = client

    @property
    def aan(self) -> bool:
        return self.client is not None

    def _hash(self, k: Kamer) -> str:
        return hashlib.sha256(f"{self.profiel}\n{k.verhaaltje}".encode()).hexdigest()[:16]

    def beoordeel(self, k: Kamer) -> Oordeel | None:
        """None als het niet lukt of als er geen verhaaltje is; de bot gaat dan door zonder oordeel."""
        if not self.aan or not k.verhaaltje.strip():
            return None
        h = self._hash(k)
        rij = self.db.execute("SELECT oordeel FROM beoordeling WHERE flow_id=? AND hash=?", (str(k.flow_id), h)).fetchone()
        if rij:
            return Oordeel.model_validate_json(rij[0])
        try:
            r = self.client.beta.messages.parse(
                model=MODEL,
                max_tokens=4000,
                system=SYSTEEM,
                messages=[{
                    "role": "user",
                    "content": f"Profiel kamerzoeker:\n{self.profiel}\n\n"
                    f"Kamer: {k.adres}, {k.soort}, €{k.brutto_huur:.0f}\n\nTekst van het huis:\n{k.verhaaltje}",
                }],
                output_format=Oordeel,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as e:  # netwerk, limiet, sleutel: nooit de ronde laten vallen
            log.warning("Beoordeling %s mislukt: %s", k.flow_id, type(e).__name__)
            return None
        if r.stop_reason == "refusal" or r.parsed_output is None:
            log.warning("Beoordeling %s: geen uitslag (%s)", k.flow_id, r.stop_reason)
            return None
        o = _schoon(r.parsed_output)
        self.db.execute(
            "INSERT OR REPLACE INTO beoordeling (flow_id, hash, oordeel) VALUES (?, ?, ?)",
            (str(k.flow_id), h, json.dumps(o.model_dump())),
        )
        self.db.commit()
        return o
