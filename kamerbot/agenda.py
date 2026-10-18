"""Hospiteeravonden in je Google Agenda (Hidde, 18 okt 2026: hoofdagenda).

Zelfde service-account als de ritbot: de sleutel staat als JSON op één regel in GOOGLE_SERVICE_ACCOUNT_JSON,
en je deelt de agenda (GCAL_AGENDA_ID) met het client_email van dat account ("Wijzigingen aanbrengen in
afspraken"). Elke kamer krijgt een vaste afspraak-id, zodat bijwerken nooit dubbele afspraken geeft.

Datum uit SSH (ViewingDate), tijd uit het verhaaltje via het taalmodel; zonder tijd 19:30–22:00 met
"(tijd onbekend)". Fouten worden gemeld maar houden de ronde nooit tegen.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .ssh import NL, Kamer, _datum

SCOPE = "https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
STANDAARD = ("19:30", "22:00")


class AgendaFout(Exception):
    """Agenda bijwerken mislukt. De tekst bevat nooit de sleutel of het token."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_jwt(info: dict[str, Any], nu: float) -> str:
    try:
        key = serialization.load_pem_private_key(str(info["private_key"]).encode(), password=None)
    except ValueError as e:
        raise AgendaFout("sleutel van het service-account is onleesbaar (GOOGLE_SERVICE_ACCOUNT_JSON)") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AgendaFout("sleutel van het service-account is geen RSA-sleutel")
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = str(info["private_key_id"])
    claims = {"iss": info["client_email"], "scope": SCOPE, "aud": info.get("token_uri") or TOKEN_URL,
              "iat": int(nu), "exp": int(nu) + 3600}
    body = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{body}.{_b64(key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


def lees_sleutel(raw: str) -> dict[str, Any]:
    try:
        info = json.loads(raw)
    except ValueError as e:
        raise AgendaFout("GOOGLE_SERVICE_ACCOUNT_JSON is geen geldige JSON") from e
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise AgendaFout("GOOGLE_SERVICE_ACCOUNT_JSON mist client_email of private_key")
    signed_jwt(info, time.time())  # meteen bij de start zeggen als de sleutel niet klopt
    return info


def afspraak_id(k: Kamer) -> str:
    # Google wil 5–1024 tekens uit 0-9 en a-v.
    return f"kamerbot{k.flow_id}"


def hospiteerdag(k: Kamer) -> date | None:
    if not k.bezichtiging:
        return None
    return _datum(k.bezichtiging).astimezone(NL).date()


def afspraak(k: Kamer) -> dict[str, Any] | None:
    """De afspraak voor deze kamer, of None als SSH geen datum geeft."""
    dag = hospiteerdag(k)
    if dag is None:
        return None
    o = k.oordeel
    begin, eind = (getattr(o, "hospiteer_begin", None), getattr(o, "hospiteer_eind", None))
    onbekend = not begin
    if onbekend:
        begin, eind = STANDAARD
    start = datetime.combine(dag, datetime.strptime(begin, "%H:%M").time(), NL)
    stop = datetime.combine(dag, datetime.strptime(eind, "%H:%M").time(), NL) if eind else start + timedelta(hours=2, minutes=30)
    if stop <= start:
        stop = start + timedelta(hours=2, minutes=30)
    titel = f"{k.soort or 'Hospiteren'} {k.adres}" + (" (tijd onbekend)" if onbekend else "")
    uitleg = [k.url]
    if k.positie:
        uitleg.append(f"Plek {k.positie} van {k.aantal_reacties}")
    if getattr(o, "reden", None):
        uitleg.append(o.reden)
    return {
        "summary": titel,
        "location": f"{k.adres}, {k.stad.title() or 'Utrecht'}",
        "description": "\n".join(uitleg),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Amsterdam"},
        "end": {"dateTime": stop.isoformat(), "timeZone": "Europe/Amsterdam"},
    }


class Agenda:
    def __init__(self, sleutel_json: str, agenda_id: str, http: httpx.Client | None = None):
        self.info = lees_sleutel(sleutel_json)
        self.cal = quote(agenda_id, safe="")
        self.http = http or httpx.Client(timeout=15)
        self._token: str | None = None
        self._tot = 0.0

    def _auth(self) -> str:
        nu = time.time()
        if self._token and nu < self._tot - 60:
            return self._token
        try:
            r = self.http.post(self.info.get("token_uri") or TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed_jwt(self.info, nu)})
        except httpx.HTTPError as e:
            raise AgendaFout(f"Google Agenda niet bereikbaar ({type(e).__name__})") from e
        if r.status_code != 200 or not r.json().get("access_token"):
            raise AgendaFout(f"inloggen bij Google Agenda: HTTP {r.status_code}")
        self._token = r.json()["access_token"]
        self._tot = nu + float(r.json().get("expires_in", 3600))
        return self._token

    def _call(self, methode: str, pad: str, body: dict | None = None) -> int:
        try:
            r = self.http.request(methode, f"{API}/calendars/{self.cal}/events{pad}", json=body,
                                  headers={"Authorization": f"Bearer {self._auth()}"})
        except httpx.HTTPError as e:
            raise AgendaFout(f"Google Agenda niet bereikbaar ({type(e).__name__})") from e
        return r.status_code

    def zet(self, event_id: str, body: dict[str, Any]) -> None:
        """Bijwerken, of aanmaken als hij er nog niet is. Een eerder verwijderde afspraak komt terug."""
        status = self._call("PUT", f"/{event_id}", {**body, "status": "confirmed"})
        if status == 404:
            status = self._call("POST", "", {**body, "id": event_id})
        if status in (403, 404):
            raise AgendaFout(f"geen toegang tot de agenda (HTTP {status}); deel hem met {self.info['client_email']}"
                             " (wijzigingen aanbrengen in afspraken) en controleer GCAL_AGENDA_ID")
        if status not in (200, 201):
            raise AgendaFout(f"afspraak opslaan: HTTP {status}")

    def verwijder(self, event_id: str) -> None:
        status = self._call("DELETE", f"/{event_id}")
        if status not in (200, 204, 404, 410):
            raise AgendaFout(f"afspraak verwijderen: HTTP {status}")
