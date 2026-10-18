"""Eén ronde: aanbod ophalen, filteren, kiezen, reageren en melden."""
from __future__ import annotations

import html
import logging

from . import selectie
from .config import Config
from .login import log_in
from .opslag import Opslag
from .ssh import Kamer, SSHClient
from .telegram import Telegram

log = logging.getLogger("kamerbot")


def regel(k: Kamer) -> str:
    pos = f"#{k.positie} van {k.aantal_reacties}" if k.positie else "positie ?"
    m2 = f"{k.m2:.0f} m²" if k.m2 else ""
    return (
        f'<a href="{k.url}">{html.escape(k.adres)}</a> · €{k.brutto_huur:.0f} · {m2} · '
        f"{k.soort} · {pos} · sluit {k.deadline:%a %d-%m %H:%M}"
    )


def ronde(cfg: Config) -> str:
    """Geeft een korte samenvatting terug (voor /status en het log)."""
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    db = Opslag(cfg.data_dir / "kamerbot.db")
    cookies = cfg.data_dir / "ssh-cookies.json"
    ssh = SSHClient(cookies)

    if not ssh.ingelogd():
        try:
            log_in(cfg.ssh_email, cfg.ssh_password, cookies)
        except Exception as e:
            log.exception("Inloggen mislukt")
            if not db.heeft("ssh", "login", "fout"):
                tg.stuur(f"⚠️ Kamerbot kan niet inloggen bij SSH: {html.escape(str(e))[:200]}")
                db.zet("ssh", "login", "fout")
            return "inloggen mislukt"
        ssh = SSHClient(cookies)
        db.db.execute("DELETE FROM kamers WHERE bron='ssh' AND id='login'")
        db.db.commit()

    kandidaten, nieuw, gereageerd = [], [], []
    for k in ssh.aanbod():
        if selectie.basisfilter(k, cfg.filters):
            continue
        ssh.details(k)
        if k.al_gereageerd:
            gereageerd.append(k)
        reden = selectie.detailfilter(k, cfg.filters)
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

    for k in selectie.kansloos(gereageerd, cfg):
        sleutel = f"kansloos-{k.positie}"
        if not db.heeft("ssh", str(k.flow_id), sleutel) and tg.stuur(
            f"⚠️ Je bent gezakt naar plek {k.positie} (grens {cfg.max_positie}):\n{regel(k)}\n"
            "Overweeg je reactie in te trekken, dan komt er een plek vrij."
        ):
            db.zet("ssh", str(k.flow_id), sleutel)

    vrij = cfg.max_actief - ssh.actieve_reacties()
    keuze = selectie.kies(kandidaten, vrij, cfg)
    log.info("%d kandidaten, %d plekken vrij, %d gekozen", len(kandidaten), vrij, len(keuze))

    for k in keuze:
        if db.heeft("ssh", str(k.flow_id), "gereageerd"):
            continue
        mag, reden = ssh.kan_reageren(k)
        if not mag or "mismatch" in reden:
            log.info("SSH zegt nee op %s: %s", k.adres, reden)
            continue
        if not cfg.auto_reageren:
            if not db.heeft("ssh", str(k.flow_id), "voorstel"):
                if tg.stuur(f"👉 Zou nu reageren op:\n{regel(k)}\n(AUTO_REAGEREN staat uit)"):
                    db.zet("ssh", str(k.flow_id), "voorstel")
            continue
        ssh.reageer(k)
        db.zet("ssh", str(k.flow_id), "gereageerd")
        tg.stuur(f"✅ Gereageerd op:\n{regel(k)}")

    return f"{len(kandidaten)} kandidaten, {vrij} plekken vrij, {len(keuze)} gekozen"
