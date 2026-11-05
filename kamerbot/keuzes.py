"""Per kamer in het aanbod: wat de laatste ronde besloot en waarom (voor /keuzes, Hidde 4 nov 2026).

De ronde zelf vult dit tijdens het beslissen (ronde.py), dus de redenen hier zijn precies de redenen
waarop de bot handelde; er is geen tweede kopie van de beslislogica die kan gaan afwijken.
Na de ronde staat het in data/keuzes.json; /keuzes leest dat en groepeert op reden.
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .ssh import NL, Kamer

# Volgorde en kop van de groepen in /keuzes.
GROEPEN = {
    "gekozen": "✅ Gekozen: nu op gereageerd",
    "blijft": "📌 Al op gereageerd, blijft staan",
    "wacht": "⏳ Nog te veel tijd over (wacht op 24u-voorrang)",
    "geen_plek": "🚫 Geen kans: plek te hoog t.o.v. je andere reacties",
    "bezichtiging": "👀 Bezichtiging buiten de top",
    "vrouw": "♀️ Zoekt een vrouw",
    "woord": "📝 Verhaaltje sluit je uit",
    "leeftijd": "🎂 Andere leeftijd gezocht",
    "taalmodel": "🤖 Past niet volgens het taalmodel",
    "locatie": "📍 Uitgesloten locatie (IBB, sterren, Science Park)",
    "duur": "💸 Te duur",
    "fiets": "🚲 Te ver fietsen",
    "ssh_nee": "⛔ SSH laat je niet reageren",
    "zelf_ingetrokken": "🙅 Door jou ingetrokken, bot reageert niet opnieuw",
    "fout": "⚠️ Ging mis of al gesloten",
    "stad": "🏙 Andere stad",
}
# Groepen die alleen als aantal verschijnen (interesseert je niet per kamer).
ALLEEN_AANTAL = {"stad"}

DAGEN = ("ma", "di", "wo", "do", "vr", "za", "zo")


def _dag(t: datetime, tijd: bool = False) -> str:
    return f"{DAGEN[t.weekday()]} {t:%d-%m}" + (f" {t:%H:%M}" if tijd else "")


MAX_BERICHT = 3900  # Telegram kapt af bij 4096 tekens; houd marge voor de HTML.


def groep_van(reden: str) -> str:
    """Welke groep bij een afwijzing uit selectie.py hoort (de teksten die de filters teruggeven)."""
    r = reden.lower()
    if r.startswith("stad "):
        return "stad"
    if r.startswith("huur "):
        return "duur"
    if r.startswith("straat "):
        return "locatie"
    if "min fietsen" in r:
        return "fiets"
    if r.startswith("bezichtiging"):
        return "bezichtiging"
    if r == "verhaaltje: zoekt een vrouw" or r.startswith("zoekt "):
        return "vrouw"
    if r.startswith("verhaaltje:"):
        return "woord"
    if r.startswith("leeftijd:"):
        return "leeftijd"
    if r.startswith("past niet"):
        return "taalmodel"
    if r.startswith("sluit pas"):
        return "wacht"
    if r.startswith("plek "):
        return "geen_plek"
    if r == "al gereageerd":
        return "blijft"
    return "fout"


class Keuzes:
    """Wat de ronde per kamer besloot. Een latere zet() voor dezelfde kamer overschrijft de eerdere."""

    def __init__(self):
        self.kamers: dict[int, tuple[Kamer, str, str]] = {}

    def zet(self, k: Kamer, groep: str, detail: str = "") -> None:
        self.kamers[k.flow_id] = (k, groep, detail)

    def afgewezen(self, k: Kamer, reden: str, extra: str = "") -> None:
        """Afgevallen op een filter: groep uit de reden, de reden zelf als toelichting."""
        self.zet(k, groep_van(reden), f"{reden}{extra}")

    def wacht_op_voorrang(self, nu: datetime) -> None:
        """Een kamer zonder plek die op de wachtlijst beter staat dan een kamer die wél een reactie heeft,
        verloor alleen door de 24-uursvoorrang: die heeft nog te veel tijd over, geen te hoge plek."""
        from .selectie import VOORRANG_UUR, rang
        houders = [k for k, g, _ in self.kamers.values() if g in ("gekozen", "blijft")]
        for k, g, detail in list(self.kamers.values()):
            if g != "geen_plek" or not rang(k, nu)[0]:
                continue
            slechter = [h for h in houders if not rang(h, nu)[0] and (h.positie or 10_000) > (k.positie or 10_000)]
            if slechter:
                uitleg = (f"plek {k.positie} is beter dan {slechter[0].adres} (plek {slechter[0].positie}), "
                          f"maar die sluit binnen {VOORRANG_UUR} uur en gaat voor")
                self.zet(k, "wacht", f"{detail} · {uitleg}" if detail.startswith("↩️") else uitleg)

    def groep(self, k: Kamer) -> str | None:
        rij = self.kamers.get(k.flow_id)
        return rij[1] if rij else None

    def bewaar(self, pad: Path, nu: datetime | None = None) -> None:
        nu = nu or datetime.now(timezone.utc)
        data = {"tijd": nu.isoformat(), "kamers": [
            {"flow_id": k.flow_id, "adres": k.adres, "url": k.url, "huur": k.brutto_huur, "soort": k.soort,
             "positie": k.positie, "aantal": k.aantal_reacties, "deadline": k.deadline.isoformat(),
             "fiets": k.fietsminuten, "groep": groep, "detail": detail}
            for k, groep, detail in self.kamers.values()
        ]}
        pad.parent.mkdir(parents=True, exist_ok=True)
        tmp = pad.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, pad)


def _regel(r: dict) -> str:
    pos = f"#{r['positie']} van {r['aantal']}" if r.get("positie") else "plek ?"
    sluit = datetime.fromisoformat(r["deadline"]).astimezone(NL)
    tekst = (f'• <a href="{r["url"]}">{html.escape(r["adres"])}</a> · €{r["huur"]:.0f} · {pos} · '
             f"sluit {_dag(sluit)}")
    if r.get("soort") == "Bezichtiging":
        tekst += " · bezichtiging"
    if r.get("detail"):
        tekst += f"\n   <i>{html.escape(r['detail'])}</i>"
    return tekst


def berichten(pad: Path) -> list[str]:
    """De tekst voor /keuzes, verdeeld over berichten die Telegram nog accepteert."""
    if not pad.exists():
        return ["Nog geen ronde met keuzes bewaard. Stuur /nu om er een te draaien."]
    data = json.loads(pad.read_text(encoding="utf-8"))
    per_groep: dict[str, list[dict]] = {}
    for r in data["kamers"]:
        per_groep.setdefault(r["groep"] if r["groep"] in GROEPEN else "fout", []).append(r)
    tijd = datetime.fromisoformat(data["tijd"]).astimezone(NL)
    gekozen = len(per_groep.get("gekozen", [])) + len(per_groep.get("blijft", []))
    blokken = [f"🧭 <b>Keuzes van de ronde van {_dag(tijd, True)}</b>\n"
               f"{len(data['kamers'])} kamers in het aanbod, op {gekozen} heb je een reactie. "
               "Stuur /nu voor een verse ronde."]
    for groep, kop in GROEPEN.items():
        rijen = per_groep.get(groep)
        if not rijen:
            continue
        if groep in ALLEEN_AANTAL:
            blokken.append(f"<b>{kop}</b> ({len(rijen)})")
            continue
        rijen.sort(key=lambda r: (r["deadline"], r.get("positie") or 10_000))
        blokken.append(f"<b>{kop}</b> ({len(rijen)})\n" + "\n".join(_regel(r) for r in rijen))
    return _knip("\n\n".join(blokken))


def _knip(tekst: str) -> list[str]:
    """Op regelgrenzen splitsen, zodat een link of <i> nooit middendoor gaat."""
    delen, huidig = [], ""
    for regel in tekst.split("\n"):
        if huidig and len(huidig) + 1 + len(regel) > MAX_BERICHT:
            delen.append(huidig)
            huidig = regel
        else:
            huidig = f"{huidig}\n{regel}" if huidig else regel
    if huidig:
        delen.append(huidig)
    return delen
