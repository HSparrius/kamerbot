"""De bot als vaste dienst op de VPS (systemd): rondes inplannen en Telegram-commando's afhandelen.

Twee rondes per dag, elk op een willekeurig moment binnen een venster (Hidde, 18 okt 2026):
- overdag tussen 9:00 en 18:00;
- 's avonds tussen 22:00 en 23:30, om te kijken of je nog kans maakt vóór de deadline van 00:00.
Mislukt een ronde (bijv. inloggen), dan probeert de bot het 10 minuten later opnieuw, zolang dat nog
binnen het venster valt.

Commando's (alleen vanuit je eigen chat): /update, /status, /nu.

/update werkt zoals bij de ritbot: de bot schrijft data/update.request, een systemd-pad-unit
start deploy/update.sh, en dat script laat een melding achter in data/notices/.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta
from datetime import time as tijd

from .config import Config
from .opslag import Opslag
from .ronde import RondeMislukt, ronde
from .ssh import NL, NietIngelogd
from .telegram import Telegram

log = logging.getLogger(__name__)

VENSTERS = {
    "overdag": (tijd(9, 0), tijd(18, 0)),
    "avond": (tijd(22, 0), tijd(23, 30)),
}
OPNIEUW_NA = timedelta(minutes=10)
SPELING = timedelta(minutes=4)



def kies_moment(dag: date, venster: tuple[tijd, tijd], rng: random.Random) -> datetime:
    begin = datetime.combine(dag, venster[0], NL)
    eind = datetime.combine(dag, venster[1], NL)
    return begin + timedelta(seconds=rng.randint(0, int((eind - begin).total_seconds())))


class Planning:
    """Per dag en venster één tijdstip, bewaard in de database zodat een herstart niets verschuift."""

    def __init__(self, db: Opslag, rng: random.Random | None = None):
        self.db = db.db
        self.rng = rng or random.Random()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS planning (dag TEXT, venster TEXT, moment TEXT, gedaan INTEGER DEFAULT 0,"
            " PRIMARY KEY (dag, venster))"
        )

    def moment(self, dag: date, venster: str) -> tuple[datetime, bool]:
        rij = self.db.execute(
            "SELECT moment, gedaan FROM planning WHERE dag=? AND venster=?", (dag.isoformat(), venster)
        ).fetchone()
        if rij:
            return datetime.fromisoformat(rij[0]), bool(rij[1])
        m = kies_moment(dag, VENSTERS[venster], self.rng)
        self.db.execute(
            "INSERT INTO planning (dag, venster, moment) VALUES (?, ?, ?)", (dag.isoformat(), venster, m.isoformat())
        )
        self.db.commit()
        return m, False

    def aan_de_beurt(self, nu: datetime) -> str | None:
        """Het venster waarvan de ronde nu moet draaien, of None. Een gemist venster wordt niet ingehaald."""
        for venster, (_, eind) in VENSTERS.items():
            m, gedaan = self.moment(nu.date(), venster)
            if not gedaan and m <= nu <= datetime.combine(nu.date(), eind, NL) + SPELING:
                return venster
        return None

    def klaar(self, dag: date, venster: str) -> None:
        self.db.execute("UPDATE planning SET gedaan=1 WHERE dag=? AND venster=?", (dag.isoformat(), venster))
        self.db.commit()

    def opnieuw(self, nu: datetime, venster: str) -> datetime | None:
        """Na een mislukte ronde: 10 minuten later nog een keer, als dat binnen het venster valt."""
        m = nu + OPNIEUW_NA
        if m > datetime.combine(nu.date(), VENSTERS[venster][1], NL) + SPELING:
            return None
        self.db.execute(
            "UPDATE planning SET moment=?, gedaan=0 WHERE dag=? AND venster=?", (m.isoformat(), nu.date().isoformat(), venster)
        )
        self.db.commit()
        return m

    def volgende(self, nu: datetime) -> datetime | None:
        for dag in (nu.date(), nu.date() + timedelta(days=1)):
            for venster in VENSTERS:
                m, gedaan = self.moment(dag, venster)
                if not gedaan and m > nu:
                    return m
        return None


class Dienst:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = cfg.data_dir
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.db = Opslag(self.data / "kamerbot.db")
        self.planning = Planning(self.db)
        self.offset = 0
        self.laatste = "nog geen ronde gedraaid sinds de start"

    # ------------------------------------------------------------------ hoofdlus
    def draai(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "started").write_text(datetime.now(NL).isoformat() + "\n")
        log.info("Dienst gestart, volgende ronde %s", self.planning.volgende(datetime.now(NL)))
        while True:
            self.meldingen_doorsturen()
            nu = datetime.now(NL)
            venster = self.planning.aan_de_beurt(nu)
            if venster:
                self.planning.klaar(nu.date(), venster)
                if not self.doe_ronde(f"ronde {venster}"):
                    m = self.planning.opnieuw(nu, venster)
                    if m:
                        self.laatste += f" (nieuwe poging {m:%H:%M})"
            for bericht in self.tg.berichten(self.offset, wacht_s=30):
                self.offset = bericht["update_id"] + 1
                self.verwerk(bericht.get("message") or {})

    def doe_ronde(self, naam: str) -> bool:
        """True als de ronde gelukt is."""
        log.info("Start %s", naam)
        gelukt = False
        try:
            self.laatste = f"{datetime.now(NL):%a %d-%m %H:%M}: {ronde(self.cfg)}"
            gelukt = True
        except RondeMislukt as e:
            self.laatste = f"{datetime.now(NL):%a %d-%m %H:%M}: {e}"
        except NietIngelogd:
            (self.data / "ssh-cookies.json").unlink(missing_ok=True)
            self.laatste = "sessie verlopen, volgende ronde logt opnieuw in"
        except Exception as e:  # de dienst moet blijven draaien, wat er ook misgaat
            log.exception("%s mislukt", naam)
            self.laatste = f"mislukt: {type(e).__name__}"
            self.tg.stuur(f"❌ Kamerbot: {naam} mislukt ({type(e).__name__}). Kijk op de VPS: journalctl -u kamerbot -n 50")
        log.info("Klaar: %s", self.laatste)
        return gelukt

    # ------------------------------------------------------------------ telegram
    def verwerk(self, bericht: dict) -> None:
        if str((bericht.get("chat") or {}).get("id")) != str(self.cfg.telegram_chat_id):
            return  # alleen Hidde
        cmd = (bericht.get("text") or "").split()[0:1]
        cmd = cmd[0].split("@")[0].lower() if cmd else ""
        if cmd == "/update":
            (self.data / "update.request").write_text(datetime.now(NL).isoformat() + "\n")
            self.tg.stuur("⏳ Bijwerken gestart. Ik herstart zo en meld daarna welke versie draait. "
                          "Start de nieuwe versie niet, dan zet ik de vorige vanzelf terug.")
        elif cmd == "/nu":
            self.tg.stuur("🔎 Ronde gestart…")
            self.doe_ronde("ronde op verzoek")
            self.tg.stuur(f"Klaar: {self.laatste}")
        elif cmd == "/status":
            volgende = self.planning.volgende(datetime.now(NL))
            auto = "aan" if self.cfg.auto_reageren else "uit (alleen voorstellen)"
            self.tg.stuur(
                f"Laatste ronde: {self.laatste}\n"
                f"Volgende ronde: {f'{volgende:%a %d-%m %H:%M}' if volgende else 'onbekend'}\n"
                f"Automatisch reageren: {auto}\n"
                f"Kansgrens: plek {self.cfg.max_positie}"
            )
        elif cmd in ("/start", "/help"):
            self.tg.stuur("/status – laatste en volgende ronde\n/nu – nu een ronde draaien\n/update – nieuwste versie installeren")

    def meldingen_doorsturen(self) -> None:
        map_ = self.data / "notices"
        if not map_.is_dir():
            return
        for p in sorted(map_.glob("*.txt")):
            tekst = p.read_text(encoding="utf-8").strip()
            p.unlink(missing_ok=True)
            if tekst:
                self.tg.stuur(tekst[:3000])
