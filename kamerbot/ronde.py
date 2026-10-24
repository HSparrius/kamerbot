"""Eén ronde: aanbod ophalen, filteren, kiezen, intrekken, reageren, agenda bijwerken en melden."""
from __future__ import annotations

import hashlib
import html
import json
import logging

import httpx

from . import selectie
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
    tekst = (
        f'<a href="{k.url}">{html.escape(k.adres)}</a> · €{k.brutto_huur:.0f} · {m2} · '
        f"{k.soort} · {pos} · sluit {k.deadline:%a %d-%m %H:%M}"
    )
    if k.oordeel is not None:
        tekst += f"\n   🤖 {k.oordeel.cijfer}/10: {html.escape(k.oordeel.reden)}"
    return tekst


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


def vergeet_reactie(db: Opslag, k: Kamer) -> None:
    """Na intrekken door de bot: niet meer als gereageerd onthouden, anders slaat hij de kamer voorgoed over,
    ook als hij later wél weer past (bijv. na een filterfout, zoals Otterstraat 2B k10 op 18 okt 2026).
    Trek je zelf in op de site, dan blijft "gereageerd" staan en reageert de bot niet opnieuw."""
    db.wis("ssh", str(k.flow_id), "gereageerd")


def ronde(cfg: Config, ssh: SSHClient | None = None, tg: Telegram | None = None,
          beoordelaar: Beoordelaar | None = None, agenda: Agenda | None = None) -> str:
    """Geeft een korte samenvatting terug (voor /status en het log). Gooit RondeMislukt als hij niet kan draaien."""
    tg = tg or Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    db = Opslag(cfg.data_dir / "kamerbot.db")
    ssh = ssh or inloggen(cfg, db, tg)
    beoordelaar = beoordelaar or Beoordelaar(cfg.anthropic_api_key, cfg.profiel, db)
    if agenda is None:
        agenda = maak_agenda(cfg, db, tg)

    kandidaten, nieuw, gereageerd = [], [], []
    aanbod = ssh.aanbod()
    for k in aanbod:
        # Altijd de details ophalen: ook een kamer buiten je filters kan een reactie van je hebben
        # (zelf geplaatst), en die moet de bot zien om hem in te trekken of uit je agenda te halen.
        ssh.details(k)
        if k.al_gereageerd:
            k.oordeel = beoordelaar.beoordeel(k)  # voor de tijd in de agenda
            # Zelf gereageerd en blijkt het huis toch iets anders te zoeken: meteen intrekken. Reacties die
            # je zelf op de site plaatste laat de bot staan; dat is jouw keuze.
            fout = selectie.eisenfilter(k, cfg.filters) or selectie.oordeelfilter(k)
            if fout and cfg.auto_reageren and db.heeft("ssh", str(k.flow_id), "gereageerd"):
                try:
                    ssh.trek_in(k)
                except httpx.HTTPStatusError as e:
                    log.warning("Intrekken %s mislukt: HTTP %s", k.adres, e.response.status_code)
                else:
                    vergeet_reactie(db, k)
                    agenda_weg(agenda, db, tg, k)
                    tg.stuur(f"↩️ Reactie ingetrokken ({html.escape(fout)}):\n{regel(k)}")
                    continue
            gereageerd.append(k)
            continue
        if selectie.basisfilter(k, cfg.filters):
            continue
        reden = selectie.detailfilter(k, cfg.filters)
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

    # Eerst uitzoeken op welke kamers je echt kunt reageren (SSH zegt ja), snelst verlopend eerst.
    vrij = cfg.max_actief - ssh.actieve_reacties()
    # Alleen reacties die de bot zelf plaatste; wat je zelf op de site doet, blijft staan (Hidde, 18 okt 2026).
    kansloos = sorted((k for k in selectie.kansloos(gereageerd, cfg) if db.heeft("ssh", str(k.flow_id), "gereageerd")),
                      key=lambda k: k.positie, reverse=True)
    mogelijk = []
    for k in selectie.kies(kandidaten, len(kandidaten), cfg):
        if len(mogelijk) >= max(vrij, 0) + len(kansloos):
            break
        if db.heeft("ssh", str(k.flow_id), "gereageerd"):
            continue
        mag, reden = ssh.kan_reageren(k)
        if not mag or "mismatch" in reden:
            log.info("SSH zegt nee op %s: %s", k.adres, reden)
            continue
        mogelijk.append(k)

    # Gezakt tot buiten de kansgrens: alleen intrekken als je die plek nodig hebt voor een kamer waar je
    # wél kans maakt en waar SSH je op laat reageren (Hidde, 18 okt 2026). Anders blijft de reactie staan.
    ingetrokken = 0
    for k in kansloos[:max(len(mogelijk) - vrij, 0)]:
        if cfg.auto_reageren:
            try:
                ssh.trek_in(k)
            except httpx.HTTPStatusError as e:
                log.warning("Intrekken %s mislukt: HTTP %s", k.adres, e.response.status_code)
                continue
            vergeet_reactie(db, k)
            ingetrokken += 1
            vrij += 1
            gereageerd.remove(k)
            agenda_weg(agenda, db, tg, k)
            tg.stuur(f"↩️ Reactie ingetrokken, je zakte naar plek {k.positie} (grens {cfg.max_positie}) "
                     f"en de plek was nodig:\n{regel(k)}")
            continue
        sleutel = f"kansloos-{k.positie}"
        if not db.heeft("ssh", str(k.flow_id), sleutel) and tg.stuur(
            f"⚠️ Je bent gezakt naar plek {k.positie} (grens {cfg.max_positie}):\n{regel(k)}\n"
            "Overweeg je reactie in te trekken, dan komt er een plek vrij."
        ):
            db.zet("ssh", str(k.flow_id), sleutel)

    keuze = mogelijk[:max(vrij, 0)]
    log.info("%d kandidaten, %d plekken vrij, %d gekozen", len(kandidaten), vrij, len(keuze))

    for k in keuze:
        if not cfg.auto_reageren:
            if not db.heeft("ssh", str(k.flow_id), "voorstel"):
                if tg.stuur(f"👉 Zou nu reageren op:\n{regel(k)}\n(AUTO_REAGEREN staat uit)"):
                    db.zet("ssh", str(k.flow_id), "voorstel")
            continue
        try:
            ssh.reageer(k)
        except httpx.HTTPStatusError as e:
            # Eén kamer die net dichtging of weigert, mag de rest van de ronde niet tegenhouden.
            log.warning("Reageren op %s mislukt: HTTP %s", k.adres, e.response.status_code)
            if not db.heeft("ssh", str(k.flow_id), "reageerfout"):
                tg.stuur(f"⚠️ Reageren lukte niet (HTTP {e.response.status_code}):\n{regel(k)}")
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
