"""Fietstijd naar Utrecht Centraal, geschat uit de hemelsbrede afstand.

Coördinaten komen van de PDOK Locatieserver (gratis, van de overheid, geen sleutel) op postcode +
huisnummer en worden bewaard, zodat elk adres maar één keer wordt opgezocht. Fietstijd =
hemelsbreed × 1,3 (omrijden over de weg) bij 15 km/u: 10 minuten is dan ongeveer 1,9 km hemelsbreed.
"""
from __future__ import annotations

import logging
import math
import sqlite3

import httpx

log = logging.getLogger("kamerbot")

UTRECHT_CENTRAAL = (52.0894, 5.1101)  # stationshal
OMRIJFACTOR = 1.3
FIETS_KMU = 15.0
PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = (map(math.radians, p) for p in (a, b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def fietsminuten(p: tuple[float, float]) -> float:
    return km(p, UTRECHT_CENTRAAL) * OMRIJFACTOR / FIETS_KMU * 60


class Geo:
    def __init__(self, db: sqlite3.Connection, http: httpx.Client | None = None):
        self.db = db
        self.http = http or httpx.Client(timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS locatie (sleutel TEXT PRIMARY KEY, lat REAL, lon REAL)")

    def punt(self, postcode: str, huisnummer: str) -> tuple[float, float] | None:
        """None als het adres niet te vinden is; dan wordt het de volgende ronde opnieuw geprobeerd."""
        postcode = postcode.replace(" ", "").upper()
        if not postcode:
            return None
        sleutel = f"{postcode} {huisnummer}".strip()
        rij = self.db.execute("SELECT lat, lon FROM locatie WHERE sleutel=?", (sleutel,)).fetchone()
        if rij:
            return rij
        try:
            r = self.http.get(PDOK, params={"q": sleutel, "fq": "type:adres", "rows": 1, "fl": "centroide_ll"})
            r.raise_for_status()
            docs = r.json()["response"]["docs"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("Adres %s opzoeken mislukt: %s", sleutel, e)
            return None
        if not docs:
            # Op huisnummer niet gevonden (bijv. een letter die PDOK anders schrijft): dan de postcode zelf.
            if huisnummer:
                return self.punt(postcode, "")
            return None
        lon, lat = map(float, docs[0]["centroide_ll"].removeprefix("POINT(").rstrip(")").split())
        self.db.execute("INSERT OR REPLACE INTO locatie VALUES (?, ?, ?)", (sleutel, lat, lon))
        self.db.commit()
        return lat, lon
