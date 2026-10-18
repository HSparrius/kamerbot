"""Filteren, scoren en kiezen waar de beperkte reactieplekken naartoe gaan.

Uitgangspunt: SSH rangschikt op inschrijfduur, dus je positie hangt niet af van
hoe vroeg je reageert (afgeleid uit de HAR: PotentialPosition vóór reageren is
gelijk aan CurrentPosition erna). Daarom wacht de bot tot de deadline dichtbij
is en kiest dan de beste kamers. Zo houd je plekken vrij voor betere kamers die
later binnenkomen.
"""
from __future__ import annotations

import re
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


_VROUW = r"(vrouw|vrouwen|vrouwelijke?|meid|meiden|meisje|meisjes|dame|dames|girl|girls|woman|women|female|lady|ladies)"
# Huizen die alleen een vrouw zoeken, ook als SSH het geslachtsveld leeg laat (Van Lieflandlaan, 18 okt 2026).
_ALLEEN_VROUW = [
    re.compile(rf"\b(alleen|enkel|uitsluitend|only|just)\b[^.!?\n]{{0,40}}\b{_VROUW}\b"),
    re.compile(rf"\b(zoeken|zoekt|zoek|looking for|op zoek naar|searching for)\b[^.!?\n]{{0,40}}\b{_VROUW}\b"),
    re.compile(r"\b(meidenhuis|vrouwenhuis|damesh?uis|girls?[ -]?house|all[ -]female|female[ -]only|women[ -]only)\b"),
    re.compile(r"\b(huisgenote|roommate|housemate|huisgenoot|bewoner|bewoonster)\s*\((v|f)\)"),
]
# "man of vrouw", "m/v": dan is iedereen welkom.
_IEDEREEN = re.compile(r"\b(man|jongen|guy|male|boy)\b\s*(of|or|/|en|and)\s*\b(vrouw|meid|girl|female|woman)\b"
                       r"|\b(vrouw|meid|girl|female|woman)\b\s*(of|or|/|en|and)\s*\b(man|jongen|guy|male|boy)\b"
                       r"|\bm/v\b|\bv/m\b|\bgender (doesn'?t|does not) matter\b|\bgeslacht maakt niet uit\b")


_ZOEK = re.compile(r"\b(zoeken|zoekt|zoek|looking for|op zoek naar|searching for|gezocht|wanted)\b")
_MAN = re.compile(r"\b(man|mannen|mannelijke?|jongen|jongens|gozer|kerel|heer|guy|guys|male|boy|boys|men)\b")


def zoekt_alleen_vrouw(tekst: str) -> bool:
    t = tekst.lower()
    if _IEDEREEN.search(t):
        return False
    # Zoeken ze in dezelfde zin ook een man ("een 6e en 7e vrouw en een 8e man"), dan is het een gemengde
    # vraag. Welke kamer voor wie is, staat dan per kamer in het geslachtsveld van SSH.
    for zin in re.split(r"[.!?\n]", t):
        zoek = _ZOEK.search(zin)
        if zoek and _MAN.search(zin, zoek.end()):
            return False
    return any(p.search(t) for p in _ALLEEN_VROUW)


def detailfilter(k: Kamer, f: Filters) -> str | None:
    """Filter op wat pas op de kamerpagina staat."""
    if k.al_gereageerd:
        return "al gereageerd"
    return eisenfilter(k, f)


def eisenfilter(k: Kamer, f: Filters) -> str | None:
    """Wat het huis eist (geslacht, woorden in het verhaaltje), los van of je al reageerde."""
    if zoekt_alleen_vrouw(k.verhaaltje):
        return "verhaaltje: zoekt een vrouw"
    if (k.geslacht or None) not in f.toegestane_geslachten:
        return f"zoekt {k.geslacht}"
    tekst = k.verhaaltje.lower()
    for w in f.uitsluit_woorden:
        if w in tekst:
            return f"verhaaltje: '{w}'"
    return None


def oordeelfilter(k: Kamer) -> str | None:
    """Afwijzen als het taalmodel zegt dat je niet past bij wie ze zoeken."""
    if k.oordeel is not None and not k.oordeel.past:
        return f"past niet: {k.oordeel.reden}"
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
    cijfer = getattr(k.oordeel, "cijfer", None)
    if cijfer is not None:
        s += 3 * (cijfer - 5)  # taalmodel: hoe goed je past bij wie ze zoeken
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
    # Eerst wat het snelst verloopt, binnen dezelfde deadline de hoogste score.
    rijp.sort(key=lambda k: (k.deadline, -score(k)))
    return rijp[: max(vrije_plekken, 0)]


def kansloos(gereageerd: list[Kamer], cfg: Config) -> list[Kamer]:
    """Kamers waar je al op reageerde maar inmiddels buiten de kansgrens bent gezakt."""
    return [k for k in gereageerd if k.positie and k.positie > cfg.max_positie]
