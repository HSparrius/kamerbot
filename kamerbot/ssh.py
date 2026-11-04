"""Client voor de API achter sshxl.nl (achterhaald uit HAR-opnames, oktober 2026)."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

BASE = "https://www.sshxl.nl"
NL = ZoneInfo("Europe/Amsterdam")
WACHT_S = (5, 15)  # pauze voor de 2e en 3e poging

log = logging.getLogger("kamerbot")


class NietIngelogd(Exception):
    pass


@dataclass
class Kamer:
    flow_id: int
    wocas_id: str
    soort: str  # "Hospiteren" of "Bezichtiging"
    type: str  # "Kamer" of "Woning"
    brutto_huur: float
    deadline: datetime
    straat: str = ""
    huisnummer: str = ""
    locatie: str = ""  # kamernummer binnen het huis, bijv. "k10"
    stad: str = ""
    postcode: str = ""
    m2: float | None = None
    # Onderstaande velden komen pas uit offer/{flow_id}.
    geslacht: str | None = None
    verhaaltje: str = ""
    positie: int | None = None
    aantal_reacties: int | None = None
    bezichtiging: str | None = None
    al_gereageerd: bool = False
    # Geschatte fietstijd naar Utrecht Centraal (afstand.py), None = onbekend.
    fietsminuten: float | None = None
    # Uitslag van beoordeling.py (Oordeel), als het taalmodel aan staat.
    oordeel: object | None = None

    @property
    def url(self) -> str:
        return f"{BASE}/nl/aanbod/{self.flow_id}"

    @property
    def adres(self) -> str:
        return f"{self.straat} {self.huisnummer} {self.locatie}".strip()


def _datum(s: str) -> datetime:
    # SSH geeft zeven decimalen. Met "Z" is het UTC, zonder is het Nederlandse tijd
    # (ExpireBy "…T23:59:59" = je kunt reageren tot middernacht).
    utc = s.endswith("Z")
    s = s.rstrip("Z")
    if "." in s:
        hoofd, frac = s.split(".", 1)
        s = f"{hoofd}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc if utc else NL)


def kamers_uit_lijst(offers: list[dict], details: list[dict]) -> list[Kamer]:
    per_eenheid = {str(d["EenheidNummer"]): d for d in details}
    kamers = []
    for o in offers:
        k = Kamer(
            flow_id=o["FlowId"],
            wocas_id=str(o["WocasId"]),
            soort=o["Kind"],
            type=o["UnitType"],
            brutto_huur=float(o["BruttoHuur"]),
            deadline=_datum(o["ExpireBy"]),
        )
        d = per_eenheid.get(k.wocas_id)
        if d:
            adres = (d.get("ADRES_H") or [{}])[0]
            k.straat = (adres.get("Straatnaam") or "").strip()
            k.huisnummer = f"{adres.get('Nummer') or ''}{adres.get('Letter') or ''}"
            k.locatie = (adres.get("Locatie") or "").strip()
            k.stad = (adres.get("Plaats") or "").strip().upper()
            k.postcode = (adres.get("Postcode") or "").strip()
            opp = d.get("ASP_TOTAAL_C") or []
            k.m2 = opp[0].get("TotOpp") if opp else None
        kamers.append(k)
    return kamers


def vul_details_aan(k: Kamer, offer: dict) -> Kamer:
    vloer = offer.get("FloorInformation") or {}
    k.geslacht = vloer.get("PreferenceGender")
    k.verhaaltje = vloer.get("Description") or ""
    k.aantal_reacties = offer.get("ApplicantCount")
    # PotentialPosition = waar je zou staan; CurrentPosition > 0 = je hebt al gereageerd,
    # en dan is dat je actuele plek (die kan zakken als mensen met meer inschrijftijd reageren).
    k.al_gereageerd = bool(offer.get("CurrentPosition"))
    k.positie = (offer.get("CurrentPosition") if k.al_gereageerd else offer.get("PotentialPosition")) or None
    k.bezichtiging = offer.get("ViewingDate")
    return k


def veld(d: dict, *namen: str):
    """Waarde van het eerste veld dat bestaat, zonder op hoofdletters te letten (FlowId of flowId),
    ook één laag dieper (bijv. {"Offer": {"FlowId": ...}}): de vorm van reactions is niet uit een HAR bekend."""
    if not isinstance(d, dict):
        return None
    klein = {str(k).lower(): v for k, v in d.items()}
    for n in namen:
        if klein.get(n.lower()) not in (None, ""):
            return klein[n.lower()]
    for v in d.values():
        if isinstance(v, dict):
            gevonden = veld(v, *namen)
            if gevonden not in (None, ""):
                return gevonden
    return None


@dataclass
class Reactie:
    """Eén regel van "Mijn reacties" (GET /api/v1/reactions; velden uit de JS van sshxl.nl, 4 nov 2026).
    Status "invited" = uitgenodigd, "hospiteren" = avond ingepland; "deleted"/"closed" met een StatusReason
    zoals HospiterenSomeoneHasBeenSelected (huis koos een ander) of HospiterenNoEligbleUser (geen top 15)."""
    flow_id: int
    status: str
    reden: str = ""
    soort: str = ""
    wocas_id: str = ""
    kijkdatum: str | None = None
    positie: int | None = None
    aantal: int | None = None

    @property
    def uitgenodigd(self) -> bool:
        return self.status in ("invited", "hospiteren")


def reacties_uit(lijst: object) -> list[Reactie]:
    if isinstance(lijst, dict):  # {"Items": [...]} of iets dergelijks
        lijst = next((v for v in lijst.values() if isinstance(v, list)), [])
    uit = []
    for d in lijst or []:
        flow = veld(d, "FlowId", "OfferFlowId")
        status = veld(d, "Status")
        if flow is None or status is None:
            continue
        try:
            flow = int(flow)
        except (TypeError, ValueError):
            continue
        pos = veld(d, "CurrentAdjustedPosition", "CurrentPosition")
        uit.append(Reactie(
            flow_id=flow, status=str(status).strip().lower(), reden=str(veld(d, "StatusReason") or ""),
            soort=str(veld(d, "Kind") or ""), wocas_id=str(veld(d, "WocasId") or ""),
            kijkdatum=veld(d, "ViewingOn", "ViewingDate", "HospiteerDate"),
            positie=pos if isinstance(pos, int) and pos > 0 else None,
            aantal=veld(d, "ApplicantCount")))
    if lijst and not uit:
        # Wel reacties, maar geen FlowId/Status te vinden: SSH heeft de vorm veranderd. Liever niets
        # doen dan alle afspraken weghalen.
        raise ValueError("reacties van SSH zonder FlowId of Status")
    return uit


class SSHClient:
    def __init__(self, cookie_file: Path):
        self.cookie_file = cookie_file
        self.http = httpx.Client(
            base_url=BASE,
            timeout=httpx.Timeout(45, connect=10),
            headers={
                "accept": "application/json, */*",
                "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
                "origin": BASE,
                "referer": f"{BASE}/nl/aanbod",
            },
        )
        self.laad_cookies()

    def laad_cookies(self) -> None:
        if not self.cookie_file.exists():
            return
        for c in json.loads(self.cookie_file.read_text()):
            self.http.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        xsrf = next((c["value"] for c in json.loads(self.cookie_file.read_text()) if "xsrf" in c["name"].lower()), None)
        if xsrf:
            self.http.headers["x-xsrf-token"] = xsrf

    def _get(self, pad: str, **params) -> object:
        # SSH is soms traag of hapert even (ReadTimeout, 3 nov 2026). Een GET verandert niets, dus
        # gewoon nog eens proberen; pas na drie keer geeft de ronde het op.
        # Een 500 van SSH (3 nov 2026, offer/1478441) is meestal ook tijdelijk.
        for poging in range(3):
            try:
                r = self.http.get(pad, params=params or None)
                if r.status_code < 500 or poging == 2:
                    break
                fout = f"HTTP {r.status_code}"
            except httpx.TransportError as e:
                if poging == 2:
                    raise
                fout = type(e).__name__
            log.warning("SSH %s: %s, nieuwe poging", pad, fout)
            time.sleep(WACHT_S[poging])
        if r.status_code in (401, 403):
            raise NietIngelogd(pad)
        r.raise_for_status()
        return r.json() if r.content else None

    def ingelogd(self) -> bool:
        try:
            return bool(self._get("/api/v1/access").get("LoggedIn"))
        except (NietIngelogd, httpx.HTTPError):
            return False

    def aanbod(self) -> list[Kamer]:
        offers = self._get("/api/v1/offer")
        ids = ",".join(str(o["WocasId"]) for o in offers)
        details = self._get("/api/v1/offer/getOffersDetails", wocasIds=ids) if ids else []
        return kamers_uit_lijst(offers, details)

    def details(self, k: Kamer) -> Kamer:
        return vul_details_aan(k, self._get(f"/api/v1/offer/{k.flow_id}"))

    def reacties(self) -> list[Reactie]:
        return reacties_uit(self._get("/api/v1/reactions"))

    def kamer(self, flow_id: int) -> Kamer:
        """Een kamer die niet meer in het aanbod staat (deadline voorbij), voor de agenda."""
        offer = self._get(f"/api/v1/offer/{flow_id}")
        try:
            details = self._get("/api/v1/offer/getOffersDetails", wocasIds=str(offer["WocasId"]))
        except httpx.HTTPError:
            details = []
        k = kamers_uit_lijst([offer], details)[0]
        return vul_details_aan(k, offer)

    def kan_reageren(self, k: Kamer) -> tuple[bool, str]:
        r = self._get(f"/api/v1/offer/canapply/{k.flow_id}")
        return bool(r.get("canApply")), r.get("reason") or ""

    def actieve_reacties(self) -> int:
        return int(self._get("/api/v1/reactions/active-count")["NumberOfActiveApplications"])

    def reageer(self, k: Kamer) -> None:
        r = self.http.post(
            f"/api/v1/reactions/{k.flow_id}",
            content=b"",
            headers={"content-type": "application/json", "referer": k.url},
        )
        if r.status_code in (401, 403):
            raise NietIngelogd("reageren")
        r.raise_for_status()

    def trek_in(self, k: Kamer) -> None:
        """Reactie intrekken (uit HAR 18 okt 2026: DELETE op hetzelfde pad als reageren)."""
        r = self.http.delete(f"/api/v1/reactions/{k.flow_id}", headers={"referer": f"{BASE}/nl/mijn-ssh/reacties/{k.flow_id}"})
        if r.status_code in (401, 403):
            raise NietIngelogd("intrekken")
        r.raise_for_status()
