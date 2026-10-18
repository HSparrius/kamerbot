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
        "Sorbonnelaan", "Yalelaan", "Kyotolaan", "Harvardlaan", "Coimbrapad", "Oxfordlaan",
    )
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
    # Pas reageren als de deadline binnen dit aantal uur valt (zie selectie.py).
    # Met rondes tussen 9:00 en 18:00 en tussen 22:00 en 00:00 betekent 18 uur:
    # reageren op de dag van de deadline.
    reageer_venster_uur: float = float(os.environ.get("REAGEER_VENSTER_UUR", "18"))
    # Je maakt kans tot en met deze positie (volgens Hidde de eerste 30).
    max_positie: int = int(os.environ.get("SSH_MAX_POSITIE", "30"))
    # Op False: alleen melden via Telegram, niets versturen.
    auto_reageren: bool = os.environ.get("AUTO_REAGEREN", "0") == "1"
    # Taalmodel dat het verhaaltje leest (beoordeling.py); leeg = uit.
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    # Wie je bent, voor het taalmodel; leeg = het standaardprofiel in beoordeling.py.
    profiel: str = field(default_factory=lambda: os.environ.get("PROFIEL", ""))
    # Hospiteeravonden in je agenda (agenda.py); beide leeg = uit.
    google_service_account_json: str = field(default_factory=lambda: os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    gcal_agenda_id: str = field(default_factory=lambda: os.environ.get("GCAL_AGENDA_ID", ""))
    filters: Filters = field(default_factory=Filters)
