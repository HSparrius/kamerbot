"""Instellingen. Geheimen komen uit de omgeving (.env op de VPS), filters staan hier."""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Filters:
    max_brutto_huur: float = 1000.0
    steden: tuple[str, ...] = ("UTRECHT",)
    # Straten waar je niet wilt wonen: IBB, de sterrenflats en Utrecht Science Park (De Uithof).
    uitgesloten_straten: tuple[str, ...] = (
        "Ina Boudier-Bakkerlaan", "Pythagoraslaan",
        "Cambridgelaan", "Bolognalaan", "Leuvenlaan", "Leuvenplein", "Salamancapad", "Padualaan",
        "Heidelberglaan", "Uppsalalaan", "Budapestlaan", "Princetonlaan", "Princetonplein",
        "Sorbonnelaan", "Yalelaan", "Kyotolaan", "Harvardlaan", "Coimbrapad", "Oxfordlaan", "Bisschopssteeg",
    )
    # Maximaal zoveel minuten fietsen naar Utrecht Centraal (Hidde: 10 op 24 okt, 12 op 5 nov 2026); 0 = uit. Zie afstand.py.
    max_fietsminuten: float = float(os.environ.get("MAX_FIETSMINUTEN", "12"))
    # Wat SSH bij PreferenceGender kan zetten; alles behalve dit wordt overgeslagen.
    toegestane_geslachten: tuple[str | None, ...] = (None, "none", "male")
    # Woorden in het verhaaltje waardoor een kamer afvalt (kleine letters).
    uitsluit_woorden: tuple[str, ...] = (
        "kamerzoekende (v)",
        "alleen vrouwen",
        "only women",
        "female only",
        "english speaking house",
        "international house",
    )


@dataclass
class Config:
    ssh_email: str = field(default_factory=lambda: os.environ.get("SSH_EMAIL", ""))
    ssh_password: str = field(default_factory=lambda: os.environ.get("SSH_PASSWORD", ""))
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KAMERBOT_DATA", "data")))
    # Volgens Hidde maximaal 7 reacties tegelijk; de bot vraagt het actuele aantal op bij SSH.
    max_actief: int = int(os.environ.get("SSH_MAX_ACTIEF", "7"))
    # Pas reageren als de deadline binnen dit aantal uur valt (zie selectie.py); 0 = op alles meteen.
    # Standaard 0 (Hidde, 3 nov 2026): de bot reageert op elk passend aanbod en ruilt pas als de
    # plekken op zijn. Met een venster onder 24 uur doet de 24-uursvoorrang niets.
    reageer_venster_uur: float = float(os.environ.get("REAGEER_VENSTER_UUR", "0"))
    # Niet reageren boven deze plek op de wachtlijst; 0 = geen grens (Hidde, 3 nov 2026: bij te veel
    # kamers valt de slechtste plek af, zie selectie.verdeel).
    max_positie: int = int(os.environ.get("SSH_MAX_POSITIE", "0"))
    # Bij een bezichtiging (alle kandidaten komen kijken, de verhuurder kiest op volgorde) alleen reageren en
    # blijven staan als je hierbinnen staat; daarbuiten trekt de bot in (Hidde, 4 nov 2026). 0 = geen grens.
    # Hospiteren valt hier niet onder.
    bezichtiging_max_plek: int = int(os.environ.get("BEZICHTIGING_MAX_PLEK", "5"))
    # Ook reacties die je zelf op de site plaatste mogen wijken voor een betere kamer (Hidde, 4 nov 2026:
    # Jacobijnenstraat #30 bleef staan terwijl Amsterdamsestraatweg #3 eruit ging). 0 = die blijven staan.
    intrekken_handmatig: bool = os.environ.get("INTREKKEN_HANDMATIG", "1") == "1"
    # Op False: alleen melden via Telegram, niets versturen.
    auto_reageren: bool = os.environ.get("AUTO_REAGEREN", "0") == "1"
    # Taalmodel dat het verhaaltje leest (beoordeling.py); leeg = uit.
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    # Wie je bent, voor het taalmodel; leeg = het standaardprofiel in beoordeling.py.
    profiel: str = field(default_factory=lambda: os.environ.get("PROFIEL", ""))
    # Je leeftijd, voor huizen met een leeftijdseis (Hidde, 5 nov 2026: 24). Gaat mee naar het taalmodel en
    # selectie.oordeelfilter wijst een kamer af als je buiten de gevraagde leeftijd valt. 0 = onbekend.
    leeftijd: int = int(os.environ.get("LEEFTIJD", "24"))
    # Hospiteeravonden in je agenda (agenda.py); beide leeg = uit.
    google_service_account_json: str = field(default_factory=lambda: os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    gcal_agenda_id: str = field(default_factory=lambda: os.environ.get("GCAL_AGENDA_ID", ""))
    filters: Filters = field(default_factory=Filters)
