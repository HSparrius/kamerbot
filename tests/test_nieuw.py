import json
import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kamerbot import selectie
from kamerbot.agenda import Agenda, afspraak, afspraak_id
from kamerbot.beoordeling import Beoordelaar, Oordeel
from kamerbot.config import Config
from kamerbot.dienst import Planning
from kamerbot.opslag import Opslag
from kamerbot.ronde import ronde
from kamerbot.ssh import NL, Kamer


def kamer(flow_id=1, **kw):
    k = Kamer(flow_id=flow_id, wocas_id=str(flow_id), soort="Hospiteren", type="Kamer", brutto_huur=500,
              deadline=datetime.now(timezone.utc) + timedelta(hours=5), straat="Biltstraat", huisnummer=str(flow_id),
              stad="UTRECHT", m2=12, geslacht="none", verhaaltje="Wij zoeken een gezellige huisgenoot.",
              positie=10, aantal_reacties=40, bezichtiging="2026-10-25T12:00:00Z")
    for a, v in kw.items():
        setattr(k, a, v)
    return k


# ------------------------------------------------------------------ taalmodel
class FakeClaude:
    def __init__(self, uitslag, stop="end_turn"):
        self.aantal = 0
        self.uitslag, self.stop = uitslag, stop
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **kw):
        self.aantal += 1
        self.kw = kw
        return SimpleNamespace(stop_reason=self.stop, parsed_output=self.uitslag)


def oordeel(**kw):
    base = dict(past=True, cijfer=7, reden="Gezellig Nederlandstalig huis", hospiteer_begin="20:00", hospiteer_eind=None)
    return Oordeel(**{**base, **kw})


def test_beoordeling_wordt_bewaard_per_verhaaltje(tmp_path):
    claude = FakeClaude(oordeel())
    b = Beoordelaar("", "", Opslag(tmp_path / "db"), client=claude)
    k = kamer()
    assert b.beoordeel(k).cijfer == 7
    assert b.beoordeel(k).hospiteer_begin == "20:00"
    assert claude.aantal == 1
    assert claude.kw["model"] == "claude-opus-5-5" and claude.kw["fallbacks"] == "default"
    k.verhaaltje += " Nieuw: hospiteren om 21:00."
    b.beoordeel(k)
    assert claude.aantal == 2


def test_beoordeling_schoont_rare_tijden_en_cijfers(tmp_path):
    b = Beoordelaar("", "", Opslag(tmp_path / "db"), client=FakeClaude(oordeel(cijfer=14, hospiteer_begin="acht uur", hospiteer_eind="22:00")))
    o = b.beoordeel(kamer())
    assert o.cijfer == 10 and o.hospiteer_begin is None and o.hospiteer_eind is None


def test_geen_sleutel_of_weigering_geeft_geen_oordeel(tmp_path):
    assert Beoordelaar("", "", Opslag(tmp_path / "a")).beoordeel(kamer()) is None
    b = Beoordelaar("", "", Opslag(tmp_path / "b"), client=FakeClaude(None, stop="refusal"))
    assert b.beoordeel(kamer()) is None


def test_oordeel_telt_mee_in_filter_en_score():
    goed, slecht = kamer(oordeel=oordeel(cijfer=9)), kamer(oordeel=oordeel(past=False, cijfer=1, reden="Zoekt werkenden"))
    assert selectie.oordeelfilter(goed) is None
    assert "Zoekt werkenden" in selectie.oordeelfilter(slecht)
    assert selectie.score(goed) > selectie.score(kamer())


# ------------------------------------------------------------------ agenda
def test_afspraak_met_en_zonder_tijd():
    a = afspraak(kamer(oordeel=oordeel(hospiteer_begin="20:00", hospiteer_eind="21:30")))
    assert a["start"]["dateTime"] == "2026-10-25T20:00:00+01:00"  # wintertijd vanaf 25 okt
    assert a["end"]["dateTime"] == "2026-10-25T21:30:00+01:00"
    assert "tijd onbekend" not in a["summary"]
    b = afspraak(kamer())
    assert b["start"]["dateTime"].startswith("2026-10-25T19:30") and b["end"]["dateTime"].startswith("2026-10-25T22:00")
    assert b["summary"] == "Hospiteren Biltstraat 1 (tijd onbekend)"
    assert afspraak(kamer(bezichtiging=None)) is None
    assert afspraak_id(kamer(flow_id=1478666)) == "kamerbot1478666"


def sleutel():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return json.dumps({"client_email": "bot@x.iam.gserviceaccount.com", "private_key": pem.decode()})


def test_agenda_maakt_aan_als_hij_er_niet_is():
    verzoeken = []

    def antwoord(req):
        verzoeken.append((req.method, req.url.raw_path.decode()))
        if "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(404 if req.method == "PUT" else 200)

    ag = Agenda(sleutel(), "hidde@gmail.com", http=httpx.Client(transport=httpx.MockTransport(antwoord)))
    ag.zet("kamerbot1", afspraak(kamer()))
    ag.verwijder("kamerbot1")
    assert [m for m, _ in verzoeken] == ["POST", "PUT", "POST", "DELETE"]
    assert verzoeken[1][1] == "/calendar/v3/calendars/hidde%40gmail.com/events/kamerbot1"


# ------------------------------------------------------------------ ronde
class FakeSSH:
    def __init__(self, kamers, actief):
        self.kamers, self.actief = kamers, actief
        self.ingetrokken, self.gereageerd = [], []

    def aanbod(self):
        return self.kamers

    def details(self, k):
        return k

    def kan_reageren(self, k):
        return True, ""

    def actieve_reacties(self):
        return self.actief - len(self.ingetrokken)

    def trek_in(self, k):
        self.ingetrokken.append(k.flow_id)

    def reageer(self, k):
        self.gereageerd.append(k.flow_id)


class FakeTG:
    def __init__(self):
        self.berichten = []

    def stuur(self, t):
        self.berichten.append(t)
        return True


class FakeAgenda:
    def __init__(self):
        self.gezet, self.weg = [], []

    def zet(self, i, body):
        self.gezet.append(i)

    def verwijder(self, i):
        self.weg.append(i)


def test_ronde_trekt_kansloze_reactie_in_en_gebruikt_de_plek(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    gezakt = kamer(1, al_gereageerd=True, positie=35)
    goed = kamer(2, al_gereageerd=True, positie=8)
    nieuw = kamer(3)
    niet_passend = kamer(4)
    ssh, tg, ag = FakeSSH([gezakt, goed, nieuw, niet_passend], actief=7), FakeTG(), FakeAgenda()
    uitslagen = {4: oordeel(past=False, cijfer=1, reden="Zoekt internationals")}

    class B:
        def beoordeel(self, k):
            return uitslagen.get(k.flow_id)

    s = ronde(cfg, ssh=ssh, tg=tg, beoordelaar=B(), agenda=ag)
    assert ssh.ingetrokken == [1]
    assert ssh.gereageerd == [3]  # vrijgekomen plek meteen gebruikt; 4 past niet
    assert ag.weg == ["kamerbot1"]
    assert sorted(ag.gezet) == ["kamerbot2", "kamerbot3"]
    assert "1 ingetrokken" in s
    assert any("ingetrokken" in b for b in tg.berichten)
    # Tweede ronde: niets dubbel in de agenda.
    ag2 = FakeAgenda()
    ronde(cfg, ssh=FakeSSH([goed], actief=7), tg=tg, beoordelaar=B(), agenda=ag2)
    assert ag2.gezet == []


def test_ronde_zonder_auto_trekt_niet_in(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, False
    ssh, tg = FakeSSH([kamer(1, al_gereageerd=True, positie=35)], actief=5), FakeTG()

    class B:
        def beoordeel(self, k):
            return None

    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=B(), agenda=FakeAgenda())
    assert ssh.ingetrokken == []
    assert any("Overweeg" in b for b in tg.berichten)


# ------------------------------------------------------------------ dienst
def test_mislukte_ronde_wordt_na_10_minuten_opnieuw_geprobeerd(tmp_path):
    p = Planning(Opslag(tmp_path / "db"), random.Random(1))
    nu = datetime(2026, 10, 18, 22, 30, tzinfo=NL)
    p.moment(nu.date(), "avond")
    p.klaar(nu.date(), "avond")
    assert p.opnieuw(nu, "avond") == nu + timedelta(minutes=10)
    assert p.aan_de_beurt(nu + timedelta(minutes=10)) == "avond"
    assert p.opnieuw(datetime(2026, 10, 18, 23, 28, tzinfo=NL), "avond") is None
