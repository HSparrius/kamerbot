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
from .ssh import Kamer, Reactie, SSHClient
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
    actief = ssh.actieve_reacties()
    reacties, mislukt = [], 0
    for k in ssh.aanbod():
        try:
            ssh.details(k)
        except httpx.HTTPError as e:
            log.warning("Details %s overgeslagen: %s", k.adres, httpfout(e))
            mislukt += 1
            continue
        if k.al_gereageerd:
            reacties.append(k)
    reacties.sort(key=lambda k: k.deadline)
    regels = [f"📋 Je SSH-reacties: {actief} van {cfg.max_actief} plekken bezet, eerst wat het eerst sluit:"]
    for i, k in enumerate(reacties, 1):
        pos = f"#{k.positie} van {k.aantal_reacties}" if k.positie else "positie ?"
        zelf = "" if db.heeft("ssh", str(k.flow_id), "gereageerd") else " · zelf geplaatst"
        regels.append(
            f'{i}. <b>{pos}</b> · <a href="{k.url}">{html.escape(k.adres)}</a> · €{k.brutto_huur:.0f} · '
            f"{k.soort} · sluit {k.deadline:%a %d-%m %H:%M}{zelf}"
        )
    if not reacties:
        regels.append("Geen reacties op kamers in het huidige aanbod.")
    # SSH telt een reactie op een gesloten kamer nog even mee, terwijl de kamer al uit het aanbod is.
    if actief > len(reacties):
        regels.append(f"+ {actief - len(reacties)} reactie(s) op kamers die niet meer in het aanbod staan "
                      "(deadline waarschijnlijk voorbij).")
    if mislukt:
        regels.append(f"⚠️ {mislukt} kamer(s) konden niet worden opgehaald, dus de lijst kan onvolledig zijn.")
    return "\n".join(regels)


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


# Waarom een reactie geen hospiteeravond (meer) heeft, in de woorden van "Mijn reacties" op sshxl.nl.
REDENEN = {
    "hospiterensomeonehasbeenselected": "het huis heeft je niet uitgekozen",
    "hospiterennoeligbleuser": "niet genoeg inschrijftijd (geen top 15)",
    "eligibleuserdeclinedbyuser": "door jou afgemeld",
    "eligibleuserdeclinedbysystem": "afgemeld door SSH",
    "eligibleuserdeclinedbyemployee": "afgemeld door SSH",
}


def reden_tekst(r: Reactie | None) -> str:
    if r is None:
        return "geen reactie meer"
    if r.reden and r.reden.lower() in REDENEN:
        return REDENEN[r.reden.lower()]
    if r.status == "deleted":
        return "reactie ingetrokken"
    if r.status == "closed":
        return f"gesloten ({r.reden})" if r.reden else "gesloten zonder uitnodiging"
    return "nog geen uitnodiging"


def kamer_voor(ssh: SSHClient, r: Reactie, aanbod: dict[int, Kamer]) -> Kamer:
    """De kamer bij een uitnodiging: uit het aanbod, of los opgehaald als de deadline al voorbij is."""
    k = aanbod.get(r.flow_id)
    if k is None:
        try:
            k = ssh.kamer(r.flow_id)
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as e:
            log.warning("Kamer %s ophalen mislukt: %s", r.flow_id, type(e).__name__)
            k = Kamer(flow_id=r.flow_id, wocas_id=r.wocas_id, soort=r.soort or "Hospiteren", type="Kamer",
                      brutto_huur=0, deadline=datetime.now(timezone.utc), straat=f"SSH-kamer {r.flow_id}")
    if r.kijkdatum:
        k.bezichtiging = r.kijkdatum
    if r.positie:
        k.positie, k.aantal_reacties = r.positie, r.aantal or k.aantal_reacties
    return k


def agenda_rijen(db: Opslag) -> list[int]:
    """De afspraken die de bot zelf in je agenda zette (alleen die raakt hij aan)."""
    return [int(i) for (i,) in db.db.execute("SELECT DISTINCT id FROM kamers WHERE bron='agenda'") if i.isdigit()]


def bijwerken_agenda(agenda: Agenda | None, db: Opslag, tg: Telegram, ssh: SSHClient,
                     aanbod: dict[int, Kamer], beoordelaar: Beoordelaar) -> None:
    """Alleen hospiteeravonden waarvoor je echt bent uitgenodigd (Hidde, 4 nov 2026). SSH nodigt pas uit
    als de reactietermijn voorbij is (top 15, drie dagen vóór de avond), dus een reactie alleen is niet genoeg.
    Een afspraak van de bot zonder uitnodiging (niet gekozen, geen top 15, ingetrokken) gaat eruit."""
    if agenda is None:
        return
    try:
        reacties = {r.flow_id: r for r in ssh.reacties()}
    except (httpx.HTTPError, ValueError) as e:
        # Zonder de status weet de bot niet wat klopt: dan niets toevoegen en niets weghalen.
        fout = httpfout(e) if isinstance(e, httpx.HTTPError) else str(e)
        agendafout(db, tg, Exception(f"je reacties bij SSH ophalen lukt niet ({fout})"))
        return

    for r in reacties.values():
        if not r.uitgenodigd:
            continue
        k = kamer_voor(ssh, r, aanbod)
        if k.oordeel is None:
            k.oordeel = beoordelaar.beoordeel(k)  # voor de tijd van de avond, als die in het verhaaltje staat
        if not db.heeft("ssh", str(r.flow_id), "uitgenodigd"):
            if tg.stuur(f"🎉 Uitgenodigd voor de hospiteeravond:\n{regel(k)}"):
                db.zet("ssh", str(r.flow_id), "uitgenodigd")
        agenda_bij(agenda, db, tg, k)

    weg = []
    for flow in agenda_rijen(db):
        r = reacties.get(flow)
        if r is not None and r.uitgenodigd:
            continue
        event_id = f"kamerbot{flow}"
        try:
            event = agenda.lees(event_id)
            start = ((event or {}).get("start") or {}).get("dateTime")
            # Een avond die al geweest is laat de bot staan; die hoort bij je geschiedenis.
            if event and start and datetime.fromisoformat(start) < datetime.now(timezone.utc):
                event = None
            elif event:
                agenda.verwijder(event_id)
        except AgendaFout as e:
            agendafout(db, tg, e)
            return
        db.db.execute("DELETE FROM kamers WHERE bron='agenda' AND id=?", (str(flow),))
        db.db.commit()
        if event:
            weg.append(f"• {html.escape(event.get('summary') or event_id)}, {start[:10]}: {reden_tekst(r)}")
    if weg:
        tg.stuur("📅 Uit je agenda gehaald (niet uitgenodigd):\n" + "\n".join(weg))


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
            k.oordeel = beoordelaar.beoordeel(k)
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

    # Plekken voor de bot: wat vrij is plus de reacties die hij mag ruilen. Sinds 4 nov 2026 (Hidde) zijn dat
    # ook je handmatige reacties, tenzij INTREKKEN_HANDMATIG=0. Reacties op kamers die niet meer in het
    # aanbod staan (deadline voorbij) tellen bij SSH nog mee, maar de bot ziet en raakt ze niet.
    vrij = max(cfg.max_actief - ssh.actieve_reacties(), 0)
    eigen = [k for k in gereageerd
             if cfg.intrekken_handmatig or db.heeft("ssh", str(k.flow_id), "gereageerd")]
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
        zelf = "" if db.heeft("ssh", str(k.flow_id), "gereageerd") else ", zelf geplaatst"
        try:
            ssh.trek_in(k)
        except httpx.HTTPError as e:
            log.warning("Intrekken %s mislukt: %s", k.adres, httpfout(e))
            keuze.remove(voor)  # die plek komt dan niet vrij
            continue
        vergeet_reactie(db, k)
        ingetrokken += 1
        gereageerd.remove(k)
        tg.stuur(f"↩️ Reactie ingetrokken (plek {k.positie}{zelf}) voor {html.escape(voor.adres)} "
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

    bijwerken_agenda(agenda, db, tg, ssh, {k.flow_id: k for k in aanbod}, beoordelaar)

    samenvatting = f"{len(kandidaten)} kandidaten, {vrij} plekken vrij, {len(keuze)} gekozen"
    if ingetrokken:
        samenvatting += f", {ingetrokken} ingetrokken"
    return samenvatting
