"""Filteren, scoren en kiezen waar de beperkte reactieplekken naartoe gaan.

Uitgangspunt: SSH rangschikt op inschrijfduur, dus je positie hangt niet af van
hoe vroeg je reageert (afgeleid uit de HAR: PotentialPosition vóór reageren is
gelijk aan CurrentPosition erna). Daarom wacht de bot tot de deadline dichtbij
is en kiest dan de beste kamers. Zo houd je plekken vrij voor betere kamers die
later binnenkomen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Config, Filters
from .ssh import Kamer


def basisfilter(k: Kamer, f: Filters) -> str | None:
    """Filter op wat al in de lijst staat. Geeft de reden van afwijzing, of None."""
    if k.stad and k.stad not in f.steden:
        return f"stad {k.stad.title()}"
    if k.brutto_huur > f.max_brutto_huur:
        return f"huur €{k.brutto_huur:.0f}"
    straat = k.straat.lower()
    for s in f.uitgesloten_straten:
        if s.lower() in straat:
            return f"straat {k.straat}"
    return None


def detailfilter(k: Kamer, f: Filters) -> str | None:
    """Filter op wat pas op de kamerpagina staat."""
    if k.al_gereageerd:
        return "al gereageerd"
    if (k.geslacht or None) not in f.toegestane_geslachten:
        return f"zoekt {k.geslacht}"
    tekst = k.verhaaltje.lower()
    for w in f.uitsluit_woorden:
        if w in tekst:
            return f"verhaaltje: '{w}'"
    return None


def score(k: Kamer) -> float:
    """Hoger is beter. Positie weegt het zwaarst: zonder uitnodiging heb je niks aan een mooie kamer."""
    s = 100.0
    if k.positie:
        s -= 4 * k.positie
    if k.m2:
        s += 0.8 * k.m2
    s -= 0.03 * k.brutto_huur
    if k.type == "Woning":
        s += 5
    return round(s, 1)


def kies(kandidaten: list[Kamer], vrije_plekken: int, cfg: Config, nu: datetime | None = None) -> list[Kamer]:
    nu = nu or datetime.now(timezone.utc)
    venster = timedelta(hours=cfg.reageer_venster_uur)
    rijp = [
        k
        for k in kandidaten
        if k.deadline - nu <= venster
        and k.deadline > nu
        and (k.positie is None or k.positie <= cfg.max_positie)
    ]
    rijp.sort(key=score, reverse=True)
    return rijp[: max(vrije_plekken, 0)]
