"""Eén ronde: aanbod ophalen, filteren, kiezen, intrekken, reageren, agenda bijwerken en melden."""
from __future__ import annotations

import hashlib
import html
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import selectie
from .afstand import Geo
from .agenda import Agenda, AgendaFout, afspraak, afspraak_id
from .beoordeling import Beoordelaar
from .config import Config
from .login import log_in
from .opslag import Opslag
from .ssh import Kamer, SSHClient
from .telegram import Telegram

log = logging.getLogger("kamerbot")


class RondeMislukt(Exception):
    """De ronde kon niet draaien (bijv. inloggen mislukt); de dienst probeert het later opnieuw."""


def regel(k: Kamer) -> str:
    pos = f"#{k.positie} van {k.aantal_reacties}" if k.positie else "positie ?"
    m2 = f"{k.m2:.0f} m²" if k.m2 else ""
    fiets = f" · 🚲 {k.fietsminuten:.0f} min" if k.fietsminuten is not None else ""
    tekst = (
        f'<a href="{k.url}">{html.escape(k.adres)}</a> · €{k.brutto_huur:.0f} · {m2} · '
        f"{k.soort} · {pos}{fiets} · sluit {k.deadline:%a %d-%m %H:%M}"
    )
    if k.oordeel is not None:
        tekst += f"\n   🤖 {k.oordeel.cijfer}/10: {html.escape(k.oordeel.reden)}"
    return tekst


def waarom(k: Kamer) -> str:
    """Waarom een nieuwe kamer voorgaat, voor de intrekmelding."""
    if k.deadline - datetime.now(timezone.utc) <= timedelta(hours=selectie.VOORRANG_UUR):
        return f"sluit binnen {selectie.VOORRANG_UUR} uur"
    return f"betere plek, sluit {k.deadline:%a %d-%m}"


def inloggen(cfg: Config, db: Opslag, tg: Telegram) -> SSHClient:
    cookies = cfg.data_dir / "ssh-cookies.json"
    ssh = SSHClient(cookies)
    if ssh.ingelogd():
        return ssh
    try:
        log_in(cfg.ssh_email, cfg.ssh_password, cookies)
    except Exception as e:
        log.exception("Inloggen mislukt")
        if not db.heeft("ssh", "login", "fout"):
            tg.stuur(f"⚠️ Kamerbot kan niet inloggen bij SSH: {html.escape(str(e))[:200]}")
            db.zet("ssh", "login", "fout")
        raise RondeMislukt("inloggen mislukt") from e
    db.db.execute("DELETE FROM kamers WHERE bron='ssh' AND id='login'")
    db.db.commit()
    return SSHClient(cookies)


def overzicht(cfg: Config, ssh: SSHClient | None = None, tg: Telegram | None = None) -> str:
    """Waar je nu op gereageerd hebt en op welke plek je staat, eerst wat het eerst sluit (voor /overzicht)."""
    tg = tg or Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    db = Opslag(cfg.data_dir / "kamerbot.db")
    ssh = ssh or inloggen(cfg, db, tg)
    reacties = []
    for k in ssh.aanbod():
        try:
            ssh.details(k)
        except httpx.HTTPError as e:
            log.warning("Details %s overgeslagen: %s", k.adres, httpfout(e))
            continue
        if k.al_gereageerd:
            reacties.append(k)
    if not reacties:
        return "📋 Je hebt nu geen reacties open bij SSH."
    reacties.sort(key=lambda k: k.deadline)
    regels = []
    for i, k in enumerate(reacties, 1):
        pos = f"#{k.positie} van {k.aantal_reacties}" if k.positie else "positie ?"
        zelf = "" if db.heeft("ssh", str(k.flow_id), "gereageerd") else " · zelf geplaatst"
        regels.append(
            f'{i}. <b>{pos}</b> · <a href="{k.url}">{html.escape(k.adres)}</a> · €{k.brutto_huur:.0f} · '
            f"{k.soort} · sluit {k.deadline:%a %d-%m %H:%M}{zelf}"
        )
    return f"📋 Je SSH-reacties ({len(reacties)} van {cfg.max_actief}), eerst wat het eerst sluit:\n" + "\n".join(regels)


def maak_agenda(cfg: Config, db: Opslag, tg: Telegram) -> Agenda | None:
    if not (cfg.google_service_account_json and cfg.gcal_agenda_id):
        return None
    try:
        return Agenda(cfg.google_service_account_json, cfg.gcal_agenda_id)
    except AgendaFout as e:
        agendafout(db, tg, e)
        return None


def agendafout(db: Opslag, tg: Telegram, e: Exception) -> None:
    log.warning("Agenda: %s", e)
    if not db.heeft("agenda", "fout", str(e)[:80]) and tg.stuur(f"📅 Agenda bijwerken lukt niet: {html.escape(str(e))}"):
        db.zet("agenda", "fout", str(e)[:80])


def agenda_bij(agenda: Agenda | None, db: Opslag, tg: Telegram, k: Kamer) -> None:
    """Afspraak zetten als hij nieuw of veranderd is (sleutel = vingerafdruk van de inhoud)."""
    if agenda is None:
        return
    body = afspraak(k)
    if body is None:
        return
    vingerafdruk = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    if db.heeft("agenda", str(k.flow_id), vingerafdruk):
        return
    try:
        agenda.zet(afspraak_id(k), body)
    except AgendaFout as e:
        agendafout(db, tg, e)
        return
    db.zet("agenda", str(k.flow_id), vingerafdruk)
    db.db.execute("DELETE FROM kamers WHERE bron='agenda' AND id='fout'")
    db.db.commit()


def agenda_weg(agenda: Agenda | None, db: Opslag, tg: Telegram, k: Kamer) -> None:
    if agenda is None:
        return
    try:
        agenda.verwijder(afspraak_id(k))
    except AgendaFout as e:
        agendafout(db, tg, e)
        return
    db.db.execute("DELETE FROM kamers WHERE bron='agenda' AND id=?", (str(k.flow_id),))
    db.db.commit()


def httpfout(e: httpx.HTTPError) -> str:
    """"HTTP 409" of "ReadTimeout": kort genoeg voor een Telegram-regel."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def vergeet_reactie(db: Opslag, k: Kamer) -> None:
    """Na intrekken door de bot: niet meer als gereageerd onthouden, anders slaat hij de kamer voorgoed over,
    ook als hij later wél weer past (bijv. na een filterfout, zoals Otterstraat 2B k10 op 18 okt 2026).
    Trek je zelf in op de site, dan blijft "gereageerd" staan en reageert de bot niet opnieuw."""
    db.wis("ssh", str(k.flow_id), "gereageerd")


def ronde(cfg: Config, ssh: SSHClient | None = None, tg: Telegram | None = None,
          beoordelaar: Beoordelaar | None = None, agenda: Agenda | None = None, geo: Geo | None = None) -> str:
    """Geeft een korte samenvatting terug (voor /status en het log). Gooit RondeMislukt als hij niet kan draaien."""
    tg = tg or Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    db = Opslag(cfg.data_dir / "kamerbot.db")
    ssh = ssh or inloggen(cfg, db, tg)
    beoordelaar = beoordelaar or Beoordelaar(cfg.anthropic_api_key, cfg.profiel, db)
    if agenda is None:
        agenda = maak_agenda(cfg, db, tg)
    geo = geo or Geo(db.db)

    kandidaten, nieuw, gereageerd = [], [], []
    aanbod = ssh.aanbod()
    for k in aanbod:
        # Altijd de details ophalen: ook een kamer buiten je filters kan een reactie van je hebben
        # (zelf geplaatst), en die moet de bot zien om hem in te trekken of uit je agenda te halen.
        try:
            ssh.details(k)
        except httpx.HTTPError as e:
            # Eén kamer waarvan SSH de details niet geeft, mag de rest van de ronde niet tegenhouden.
            log.warning("Details %s overgeslagen: %s", k.adres, httpfout(e))
            continue
        if k.al_gereageerd:
            k.oordeel = beoordelaar.beoordeel(k)  # voor de tijd in de agenda
            # Zelf gereageerd en blijkt het huis toch iets anders te zoeken: meteen intrekken. Reacties die
            # je zelf op de site plaatste laat de bot staan; dat is jouw keuze.
            fout = selectie.eisenfilter(k, cfg.filters) or selectie.oordeelfilter(k)
            if fout and cfg.auto_reageren and db.heeft("ssh", str(k.flow_id), "gereageerd"):
                try:
                    ssh.trek_in(k)
                except httpx.HTTPError as e:
                    log.warning("Intrekken %s mislukt: %s", k.adres, httpfout(e))
                else:
                    vergeet_reactie(db, k)
                    agenda_weg(agenda, db, tg, k)
                    tg.stuur(f"↩️ Reactie ingetrokken ({html.escape(fout)}):\n{regel(k)}")
                    continue
            gereageerd.append(k)
            continue
        if selectie.basisfilter(k, cfg.filters):
            continue
        reden = selectie.fietsfilter(k, cfg.filters, geo) or selectie.detailfilter(k, cfg.filters)
        if not reden:
            k.oordeel = beoordelaar.beoordeel(k)
            reden = selectie.oordeelfilter(k)
        if reden:
            log.info("Afgewezen %s: %s", k.adres, reden)
            continue
        kandidaten.append(k)
        if not db.heeft("ssh", str(k.flow_id), "gemeld"):
            nieuw.append(k)

    if nieuw:
        nieuw.sort(key=selectie.score, reverse=True)
        verstuurd = tg.stuur(f"🏠 {len(nieuw)} nieuwe SSH-kamer(s):\n" + "\n".join(f"{selectie.score(k)} · {regel(k)}" for k in nieuw[:20]))
        # Alleen als gemeld markeren als Telegram het echt heeft afgeleverd.
        if verstuurd:
            for k in nieuw:
                db.zet("ssh", str(k.flow_id), "gemeld")

    # Plekken voor de bot: wat vrij is plus zijn eigen reacties. Wat je zelf op de site doet, telt mee
    # in het aantal maar blijft altijd staan (Hidde, 18 okt 2026).
    vrij = max(cfg.max_actief - ssh.actieve_reacties(), 0)
    eigen = [k for k in gereageerd if db.heeft("ssh", str(k.flow_id), "gereageerd")]
    plekken = vrij + len(eigen)
    # Kamers waar SSH je op laat reageren, beste eerst (binnen 24 uur sluitend, dan laagste plek).
    mogelijk = []
    for k in selectie.kies(kandidaten, len(kandidaten), cfg):
        if len(mogelijk) >= plekken:
            break
        if db.heeft("ssh", str(k.flow_id), "gereageerd"):
            continue
        mag, reden = ssh.kan_reageren(k)
        if not mag or "mismatch" in reden:
            log.info("SSH zegt nee op %s: %s", k.adres, reden)
            continue
        mogelijk.append(k)

    # Te veel kamers voor 7 plekken: de kamer waar je het slechtst staat valt af, ook als dat een
    # bestaande reactie is (Hidde, 3 nov 2026), maar alleen voor een kamer die niet later sluit
    # (Hidde, 4 nov 2026). Intrekken kost niets: je plek hangt af van je inschrijfduur, dus later
    # opnieuw reageren geeft dezelfde plek.
    ruil, keuze = selectie.verdeel(eigen, mogelijk, plekken)
    ingetrokken = 0
    for k, voor in ruil:
        if not cfg.auto_reageren:
            continue
        try:
            ssh.trek_in(k)
        except httpx.HTTPError as e:
            log.warning("Intrekken %s mislukt: %s", k.adres, httpfout(e))
            keuze.remove(voor)  # die plek komt dan niet vrij
            continue
        vergeet_reactie(db, k)
        ingetrokken += 1
        gereageerd.remove(k)
        agenda_weg(agenda, db, tg, k)
        tg.stuur(f"↩️ Reactie ingetrokken (plek {k.positie}) voor {html.escape(voor.adres)} "
                 f"(plek {voor.positie}, {waarom(voor)}):\n{regel(k)}")
    if not cfg.auto_reageren:
        keuze = keuze[:vrij]
    log.info("%d kandidaten, %d plekken vrij, %d gekozen", len(kandidaten), vrij, len(keuze))

    for k in keuze:
        if not cfg.auto_reageren:
            if not db.heeft("ssh", str(k.flow_id), "voorstel"):
                if tg.stuur(f"👉 Zou nu reageren op:\n{regel(k)}\n(AUTO_REAGEREN staat uit)"):
                    db.zet("ssh", str(k.flow_id), "voorstel")
            continue
        try:
            ssh.reageer(k)
        except httpx.HTTPError as e:
            # Eén kamer die net dichtging, weigert of niet antwoordt, mag de rest van de ronde niet tegenhouden.
            # Bij een timeout kan de reactie toch zijn aangekomen; dat ziet de volgende ronde.
            log.warning("Reageren op %s mislukt: %s", k.adres, httpfout(e))
            if not db.heeft("ssh", str(k.flow_id), "reageerfout"):
                tg.stuur(f"⚠️ Reageren lukte niet ({httpfout(e)}):\n{regel(k)}")
                db.zet("ssh", str(k.flow_id), "reageerfout")
            continue
        db.zet("ssh", str(k.flow_id), "gereageerd")
        tg.stuur(f"✅ Gereageerd op:\n{regel(k)}")
        gereageerd.append(k)

    for k in gereageerd:
        agenda_bij(agenda, db, tg, k)
    # Reactie weg (zelf ingetrokken op de site) terwijl de kamer nog in het aanbod staat: afspraak weg.
    # Staat de kamer niet meer in het aanbod, dan is de deadline voorbij en kan de avond nog doorgaan.
    nog_actief = {k.flow_id for k in gereageerd}
    for k in aanbod:
        if k.flow_id not in nog_actief and db.db.execute(
                "SELECT 1 FROM kamers WHERE bron='agenda' AND id=?", (str(k.flow_id),)).fetchone():
            agenda_weg(agenda, db, tg, k)

    samenvatting = f"{len(kandidaten)} kandidaten, {vrij} plekken vrij, {len(keuze)} gekozen"
    if ingetrokken:
        samenvatting += f", {ingetrokken} ingetrokken"
    return samenvatting
