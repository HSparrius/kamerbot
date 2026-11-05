"""Filteren, scoren en kiezen waar de beperkte reactieplekken naartoe gaan.

Uitgangspunt: SSH rangschikt op inschrijfduur, dus je positie hangt niet af van
hoe vroeg je reageert (afgeleid uit de HAR: PotentialPosition vóór reageren is
gelijk aan CurrentPosition erna). Daarom wacht de bot tot de deadline dichtbij
is en kiest dan de beste kamers. Zo houd je plekken vrij voor betere kamers die
later binnenkomen.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from . import afstand
from .config import Config, Filters
from .ssh import Kamer

log = logging.getLogger("kamerbot")


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


def fietsfilter(k: Kamer, f: Filters, geo) -> str | None:
    """Te ver van Utrecht Centraal. Is het adres niet te vinden, dan gaat de kamer door (liever een
    reactie te veel dan een goede kamer missen) en staat het in het log."""
    if not f.max_fietsminuten:
        return None
    p = geo.punt(k.postcode, k.huisnummer)
    if p is None:
        log.warning("Fietstijd onbekend voor %s (%s)", k.adres, k.postcode or "geen postcode")
        return None
    k.fietsminuten = afstand.fietsminuten(p)
    if k.fietsminuten > f.max_fietsminuten:
        return f"{k.fietsminuten:.0f} min fietsen naar CS"
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


def plekfilter(k: Kamer, cfg: Config) -> str | None:
    """Bezichtiging en je staat buiten de top BEZICHTIGING_MAX_PLEK (Hidde, 4 nov 2026). Je plek kan
    verschuiven als anderen reageren of intrekken, dus de bot kijkt elke ronde opnieuw. Plek onbekend = door."""
    grens = cfg.bezichtiging_max_plek
    if k.soort == "Bezichtiging" and grens and k.positie and k.positie > grens:
        return f"bezichtiging, plek {k.positie}, alleen top {grens}"
    return None


def oordeelfilter(k: Kamer, leeftijd: int = 0) -> str | None:
    """Afwijzen als je buiten de leeftijd valt die het huis noemt, of als het taalmodel zegt dat je niet past bij
    wie ze zoeken. De leeftijd controleert de bot zelf (Hidde, 5 nov 2026): het model leest alleen de grenzen."""
    if k.oordeel is None:
        return None
    lo, hi = getattr(k.oordeel, "min_leeftijd", None), getattr(k.oordeel, "max_leeftijd", None)
    if leeftijd and ((lo and leeftijd < lo) or (hi and leeftijd > hi)):
        gezocht = f"{lo}–{hi}" if lo and hi else f"vanaf {lo}" if lo else f"tot en met {hi}"
        return f"leeftijd: zoekt {gezocht} jaar, je bent {leeftijd}"
    if not k.oordeel.past:
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


# Kamers die binnen zoveel uur sluiten gaan voor (Hidde, 3 nov 2026).
VOORRANG_UUR = 24


def rang(k: Kamer, nu: datetime) -> tuple:
    """Lager is beter: eerst wat binnen 24 uur sluit, dan je plek op de wachtlijst (laagste nummer)."""
    return (k.deadline - nu > timedelta(hours=VOORRANG_UUR), k.positie or 10_000, k.deadline)


def niet_rijp(k: Kamer, cfg: Config, nu: datetime) -> str | None:
    """Waarom kies() een kandidaat (nog) overslaat, of None. Ook gebruikt voor /keuzes."""
    if k.deadline <= nu:
        return "gesloten"
    if cfg.reageer_venster_uur and k.deadline - nu > timedelta(hours=cfg.reageer_venster_uur):
        return f"sluit pas over meer dan {cfg.reageer_venster_uur:g} uur (REAGEER_VENSTER_UUR)"
    # Geen positiegrens meer, tenzij SSH_MAX_POSITIE gezet is.
    if cfg.max_positie and k.positie is not None and k.positie > cfg.max_positie:
        return f"plek {k.positie}, grens {cfg.max_positie} (SSH_MAX_POSITIE)"
    return None


def kies(kandidaten: list[Kamer], vrije_plekken: int, cfg: Config, nu: datetime | None = None) -> list[Kamer]:
    nu = nu or datetime.now(timezone.utc)
    rijp = [k for k in kandidaten if not niet_rijp(k, cfg, nu)]
    rijp.sort(key=lambda k: (rang(k, nu), -score(k)))
    return rijp[: max(vrije_plekken, 0)]


def verdeel(eigen: list[Kamer], nieuw: list[Kamer], plekken: int, nu: datetime | None = None,
            wacht: list[Kamer] | None = None) -> tuple[list[tuple[Kamer, Kamer]], list[Kamer]]:
    """Welke reacties van de bot intrekken en op welke nieuwe kamers reageren, met `plekken` plekken
    voor de bot (vrije plekken + zijn eigen reacties). De beste `plekken` kamers volgens rang() blijven;
    bij gelijke rang wint een bestaande reactie. Zo wordt er alleen ingetrokken als er te veel zijn.

    Ruilen mag alleen voor een kamer die niet later sluit dan de reactie die wijkt (Hidde, 4 nov 2026:
    Amsterdamsestraatweg #3, sluit do, werd ingetrokken voor Gerbrandypad #2, sluit vr). Een kamer die
    later sluit kan wachten: tegen die tijd is de andere reactie beslist, en je plek hangt niet af van
    hoe vroeg je reageert. Wacht hij te lang, dan krijgt hij in zijn laatste 24 uur voorrang.

    Geeft (ruil, keuze): ruil is een lijst (in te trekken reactie, kamer die zijn plek krijgt).
    Kamers die wel goed genoeg zijn maar wachten omdat ze later sluiten, komen in `wacht` (voor /keuzes)."""
    nu = nu or datetime.now(timezone.utc)
    alles = sorted([(rang(k, nu), 0, i, k) for i, k in enumerate(eigen)]
                   + [(rang(k, nu), 1, i, k) for i, k in enumerate(nieuw)], key=lambda t: t[:3])
    top = alles[: max(plekken, 0)]
    blijft = {id(t[3]) for t in top}
    gekozen = [t[3] for t in top if t[1] == 1]  # nieuwe kamers, beste eerst
    # Kandidaten om in te trekken, slechtste eerst.
    weg = [t[3] for t in reversed(alles) if t[1] == 0 and id(t[3]) not in blijft]
    vrij = max(plekken - len(eigen), 0)
    keuze, ruil = gekozen[:vrij], []
    for n in gekozen[vrij:]:
        oud = next((e for e in weg if e.deadline >= n.deadline), None)
        if oud is None:
            if wacht is not None:
                wacht.append(n)
            continue  # wachten: alles wat zou moeten wijken sluit eerder
        weg.remove(oud)
        ruil.append((oud, n))
        keuze.append(n)
    return ruil, keuze
