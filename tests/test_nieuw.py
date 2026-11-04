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
from kamerbot.ronde import overzicht, ronde
from kamerbot.ssh import NL, Kamer, Reactie, reacties_uit


# Eén vaste deadline voor alle testkamers, zoals bij SSH (alles sluit om 23:59).
DEADLINE = datetime.now(timezone.utc) + timedelta(hours=5)


def kamer(flow_id=1, **kw):
    k = Kamer(flow_id=flow_id, wocas_id=str(flow_id), soort="Hospiteren", type="Kamer", brutto_huur=500,
              deadline=DEADLINE, straat="Biltstraat", huisnummer=str(flow_id),
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
    assert claude.kw["model"] == "claude-sonnet-5-5" and claude.kw["fallbacks"] == "default"
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
    def __init__(self, kamers, actief, reacties=()):
        self.kamers, self.actief, self._reacties = kamers, actief, list(reacties)
        self.ingetrokken, self.gereageerd = [], []

    def reacties(self):
        return self._reacties

    def kamer(self, flow_id):
        raise httpx.ConnectError("weg")

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
    def __init__(self, bestaand=None):
        self.gezet, self.weg = [], []
        self.bestaand = dict(bestaand or {})

    def zet(self, i, body):
        self.gezet.append(i)
        self.bestaand[i] = body

    def lees(self, i):
        return self.bestaand.get(i)

    def verwijder(self, i):
        self.weg.append(i)
        self.bestaand.pop(i, None)


def test_ronde_trekt_kansloze_reactie_in_en_gebruikt_de_plek(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    cfg.intrekken_handmatig = False
    gezakt = kamer(1, al_gereageerd=True, positie=35)
    zelf_gezakt = kamer(5, al_gereageerd=True, positie=40)  # zelf op de site gereageerd: blijft staan
    Opslag(tmp_path / "kamerbot.db").zet("ssh", "1", "gereageerd")
    goed = kamer(2, al_gereageerd=True, positie=8)
    nieuw = kamer(3)
    niet_passend = kamer(4)
    ssh, tg, ag = FakeSSH([gezakt, zelf_gezakt, goed, nieuw, niet_passend], actief=7), FakeTG(), FakeAgenda()
    uitslagen = {4: oordeel(past=False, cijfer=1, reden="Zoekt internationals")}

    class B:
        def beoordeel(self, k):
            return uitslagen.get(k.flow_id)

    s = ronde(cfg, ssh=ssh, tg=tg, beoordelaar=B(), agenda=ag)
    assert ssh.ingetrokken == [1]
    assert ssh.gereageerd == [3]  # vrijgekomen plek meteen gebruikt; 4 past niet
    assert ag.gezet == []  # alleen na een uitnodiging in de agenda, en die is er (nog) niet
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

    Opslag(tmp_path / "kamerbot.db").zet("ssh", "1", "gereageerd")
    ssh = FakeSSH([kamer(1, al_gereageerd=True, positie=35), kamer(2)], actief=7)
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=B(), agenda=FakeAgenda())
    assert ssh.ingetrokken == []


def test_kansloze_reactie_blijft_staan_zolang_er_plek_is(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    ssh = FakeSSH([kamer(1, al_gereageerd=True, positie=31), kamer(2)], actief=3)

    class B:
        def beoordeel(self, k):
            return None

    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=B(), agenda=FakeAgenda())
    assert ssh.ingetrokken == []
    assert ssh.gereageerd == [2]


# ------------------------------------------------------------------ dienst
def test_mislukte_ronde_wordt_na_10_minuten_opnieuw_geprobeerd(tmp_path):
    p = Planning(Opslag(tmp_path / "db"), random.Random(1))
    nu = datetime(2026, 10, 18, 22, 30, tzinfo=NL)
    p.moment(nu.date(), "avond")
    p.klaar(nu.date(), "avond")
    assert p.opnieuw(nu, "avond") == nu + timedelta(minutes=10)
    assert p.aan_de_beurt(nu + timedelta(minutes=10)) == "avond"
    assert p.opnieuw(datetime(2026, 10, 18, 23, 28, tzinfo=NL), "avond") is None


def test_onleesbare_sleutel_geeft_agendafout():
    import pytest

    from kamerbot.agenda import AgendaFout, lees_sleutel

    kapot = json.loads(sleutel())
    kapot["private_key"] = kapot["private_key"].replace("\n", "n")  # zoals systemd het inleest
    with pytest.raises(AgendaFout):
        lees_sleutel(json.dumps(kapot))


def test_env_wordt_letterlijk_gelezen(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    key = json.loads(sleutel())
    (tmp_path / ".env").write_text("GOOGLE_SERVICE_ACCOUNT_JSON=" + json.dumps(key) + "\n")
    from kamerbot.agenda import lees_sleutel

    assert lees_sleutel(dotenv_values(tmp_path / ".env")["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]


class GeenOordeel:
    def beoordeel(self, k):
        return None


def test_intrekken_niet_als_ssh_nee_zegt_tegen_de_nieuwe_kamer(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    ssh = FakeSSH([kamer(1, al_gereageerd=True, positie=35), kamer(2)], actief=7)
    ssh.kan_reageren = lambda k: (False, "gesloten")
    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.ingetrokken == []
    assert ssh.gereageerd == []


def test_een_mislukte_reactie_houdt_de_rest_niet_tegen(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    ssh, tg = FakeSSH([kamer(2), kamer(3)], actief=3), FakeTG()
    echt = ssh.reageer

    def reageer(k):
        if k.flow_id == 2:
            raise httpx.HTTPStatusError("dicht", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(400))
        echt(k)

    ssh.reageer = reageer
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.gereageerd == [3]
    assert any("lukte niet" in b for b in tg.berichten)


def test_beoordelaar_stopt_na_drie_fouten(tmp_path):
    class Kapot:
        aantal = 0

        class beta:
            class messages:
                @staticmethod
                def parse(**kw):
                    Kapot.aantal += 1
                    raise RuntimeError("API plat")

    b = Beoordelaar("", "", Opslag(tmp_path / "db"), client=Kapot)
    for i in range(6):
        assert b.beoordeel(kamer(i)) is None
    assert Kapot.aantal == 3


def afspraak_op(dagen):
    start = (datetime.now(timezone.utc) + timedelta(days=dagen)).astimezone(NL).isoformat()
    return {"summary": f"Hospiteren over {dagen} dagen", "start": {"dateTime": start}}


def test_alleen_uitgenodigd_komt_in_de_agenda(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    open_ = kamer(2, al_gereageerd=True, positie=8)
    reacties = [Reactie(2, "invited", kijkdatum="2026-11-20T12:00:00Z"),
                Reactie(3, "closed", reden="HospiterenNoEligbleUser"),
                Reactie(4, "active"),
                Reactie(9, "hospiteren", kijkdatum="2026-11-21T12:00:00Z")]  # gesloten kamer, niet in aanbod
    ag, tg = FakeAgenda(), FakeTG()
    ronde(cfg, ssh=FakeSSH([open_], actief=4, reacties=reacties), tg=tg, beoordelaar=GeenOordeel(), agenda=ag)
    assert sorted(ag.gezet) == ["kamerbot2", "kamerbot9"]
    assert ag.bestaand["kamerbot2"]["start"]["dateTime"].startswith("2026-11-20T19:30")
    assert "SSH-kamer 9" in ag.bestaand["kamerbot9"]["summary"]  # ophalen mislukt: toch een afspraak
    assert sum("Uitgenodigd" in b for b in tg.berichten) == 2
    # Tweede ronde: niets dubbel, geen tweede melding.
    ag2 = FakeAgenda(ag.bestaand)
    ronde(cfg, ssh=FakeSSH([open_], actief=4, reacties=reacties), tg=tg, beoordelaar=GeenOordeel(), agenda=ag2)
    assert ag2.gezet == [] and ag2.weg == []
    assert sum("Uitgenodigd" in b for b in tg.berichten) == 2


def test_afspraak_zonder_uitnodiging_gaat_eruit(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    db = Opslag(tmp_path / "kamerbot.db")
    for flow in (5, 6, 7, 8, 10):
        db.zet("agenda", str(flow), "abc")
    ag = FakeAgenda({f"kamerbot{f}": afspraak_op(5) for f in (5, 6, 7, 10)} | {"kamerbot8": afspraak_op(-2),
                                                                             "kamerbot99": afspraak_op(3)})
    reacties = [Reactie(5, "closed", reden="HospiterenNoEligbleUser"),  # k15: geen top 15
                Reactie(6, "closed", reden="HospiterenSomeoneHasBeenSelected"),  # k10: niet gekozen
                Reactie(8, "closed", reden="HospiterenSomeoneHasBeenSelected"),  # avond al geweest
                Reactie(10, "deleted")]  # ingetrokken; 7 staat er niet eens meer in
    tg = FakeTG()
    ronde(cfg, ssh=FakeSSH([], actief=0, reacties=reacties), tg=tg, beoordelaar=GeenOordeel(), agenda=ag)
    assert sorted(ag.weg) == ["kamerbot10", "kamerbot5", "kamerbot6", "kamerbot7"]
    assert "kamerbot8" in ag.bestaand and "kamerbot99" in ag.bestaand  # verleden en niet van de bot: blijft
    melding = next(b for b in tg.berichten if "Uit je agenda" in b)
    assert "geen top 15" in melding and "niet uitgekozen" in melding and "ingetrokken" in melding
    assert not db.db.execute("SELECT 1 FROM kamers WHERE bron='agenda'").fetchone()


def test_onleesbare_reacties_laten_de_agenda_met_rust(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    Opslag(tmp_path / "kamerbot.db").zet("agenda", "5", "abc")
    ag = FakeAgenda({"kamerbot5": afspraak_op(5)})

    class Kapot(FakeSSH):
        def reacties(self):
            return reacties_uit([{"Iets": 1}])

    tg = FakeTG()
    ronde(cfg, ssh=Kapot([], actief=1), tg=tg, beoordelaar=GeenOordeel(), agenda=ag)
    assert ag.weg == [] and ag.gezet == []
    assert any("reacties bij SSH" in b for b in tg.berichten)


def test_reacties_uit_leest_de_velden_van_mijn_reacties():
    r = reacties_uit([{"flowId": "1478671", "status": "Invited", "kind": "Hospiteren", "viewingOn": "2026-11-20T12:00:00Z",
                       "CurrentAdjustedPosition": 7, "ApplicantCount": 37},
                      {"Offer": {"FlowId": 2}, "Status": "closed", "StatusReason": "HospiterenNoEligbleUser"}])
    assert [(x.flow_id, x.status, x.uitgenodigd) for x in r] == [(1478671, "invited", True), (2, "closed", False)]
    assert r[0].kijkdatum == "2026-11-20T12:00:00Z" and r[0].positie == 7 and r[1].reden == "HospiterenNoEligbleUser"
    assert reacties_uit([]) == []


def test_eigen_reactie_op_vrouwenhuis_wordt_ingetrokken(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    db = Opslag(tmp_path / "kamerbot.db")
    db.zet("ssh", "6", "gereageerd")
    bot = kamer(6, al_gereageerd=True, verhaaltje="Wij zoeken alleen een vrouw.")
    zelf = kamer(7, al_gereageerd=True, verhaaltje="Wij zoeken alleen een vrouw.")
    ssh, tg = FakeSSH([bot, zelf], actief=2), FakeTG()
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.ingetrokken == [6]  # die van de bot; je eigen reactie blijft staan
    assert any("zoekt een vrouw" in b for b in tg.berichten)


def test_na_intrekken_door_bot_mag_hij_later_weer_reageren(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    db = Opslag(tmp_path / "kamerbot.db")
    db.zet("ssh", "6", "gereageerd")
    k = kamer(6, al_gereageerd=True, verhaaltje="Wij zoeken alleen een vrouw.")
    ronde(cfg, ssh=FakeSSH([k], actief=1), tg=FakeTG(), beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert not db.heeft("ssh", "6", "gereageerd")
    # Het filter blijkt fout en de kamer past toch: volgende ronde reageert hij opnieuw.
    ssh = FakeSSH([kamer(6, verhaaltje="Wij zoeken een vrouw en een man.")], actief=0)
    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.gereageerd == [6]


def test_zelf_ingetrokken_reactie_van_de_bot_wordt_niet_opnieuw_geplaatst(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    Opslag(tmp_path / "kamerbot.db").zet("ssh", "6", "gereageerd")
    ssh = FakeSSH([kamer(6)], actief=0)
    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.gereageerd == []


# ------------------------------------------------------------------ fietstijd
def pdok(punten):
    """Nep-PDOK: postcode+huisnummer -> (lat, lon); telt de verzoeken."""
    verzoeken = []

    def antwoord(req):
        q = req.url.params["q"]
        verzoeken.append(q)
        p = punten.get(q)
        docs = [{"centroide_ll": f"POINT({p[1]} {p[0]})"}] if p else []
        return httpx.Response(200, json={"response": {"docs": docs}})

    return httpx.Client(transport=httpx.MockTransport(antwoord)), verzoeken


def test_fietsfilter_houdt_kamers_ver_van_cs_tegen(tmp_path):
    from kamerbot.afstand import Geo
    http, verzoeken = pdok({"3581AB 100": (52.0937, 5.1325), "3584SC 141": (52.0845, 5.1730)})
    geo = Geo(Opslag(tmp_path / "db").db, http)
    f = Config().filters
    f.max_fietsminuten = 10
    dichtbij = kamer(1, postcode="3581 AB", huisnummer="100")
    ver = kamer(2, postcode="3584 SC", huisnummer="141")
    assert selectie.fietsfilter(dichtbij, f, geo) is None
    assert 7 < dichtbij.fietsminuten < 10
    assert "min fietsen" in selectie.fietsfilter(ver, f, geo)
    selectie.fietsfilter(ver, f, geo)
    assert verzoeken == ["3581AB 100", "3584SC 141"]  # tweede keer uit de opslag


def test_fietsfilter_laat_onvindbaar_adres_door_en_valt_terug_op_postcode(tmp_path):
    from kamerbot.afstand import Geo
    http, verzoeken = pdok({"3581AB": (52.0937, 5.1325)})
    geo = Geo(Opslag(tmp_path / "db").db, http)
    f = Config().filters
    f.max_fietsminuten = 10
    assert selectie.fietsfilter(kamer(1, postcode="3581AB", huisnummer="2B"), f, geo) is None
    assert verzoeken == ["3581AB 2B", "3581AB"]
    onbekend = kamer(2, postcode="9999ZZ", huisnummer="1")
    assert selectie.fietsfilter(onbekend, f, geo) is None and onbekend.fietsminuten is None


def test_ronde_slaat_kamer_ver_van_cs_over(tmp_path):
    from kamerbot.afstand import Geo
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True
    cfg.filters.max_fietsminuten = 10
    http, _ = pdok({"3581AB 1": (52.0937, 5.1325), "3584SC 2": (52.0845, 5.1730)})
    ssh, tg = FakeSSH([kamer(1, postcode="3581AB"), kamer(2, postcode="3584SC")], actief=0), FakeTG()
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=GeenOordeel(), agenda=FakeAgenda(),
          geo=Geo(Opslag(tmp_path / "geo.db").db, http))
    assert ssh.gereageerd == [1]
    assert any("🚲 8 min" in b for b in tg.berichten)


# ------------------------------------------------------------------ timeouts
def test_ssh_probeert_get_opnieuw_na_timeout(tmp_path, monkeypatch):
    from kamerbot import ssh as sshmod
    monkeypatch.setattr(sshmod, "WACHT_S", (0, 0))
    pogingen = []

    def antwoord(req):
        pogingen.append(req.url.path)
        if len(pogingen) < 3:
            raise httpx.ReadTimeout("traag", request=req)
        return httpx.Response(200, json={"NumberOfActiveApplications": 4})

    c = sshmod.SSHClient(tmp_path / "geen-cookies.json")
    c.http = httpx.Client(base_url=sshmod.BASE, transport=httpx.MockTransport(antwoord))
    assert c.actieve_reacties() == 4 and len(pogingen) == 3

    pogingen.clear()
    c.http = httpx.Client(base_url=sshmod.BASE, transport=httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("traag", request=req))))
    try:
        c.actieve_reacties()
    except httpx.ReadTimeout as e:
        from kamerbot.dienst import oorzaak
        assert oorzaak(e) == "SSH reageert niet (ReadTimeout bij /api/v1/reactions/active-count)"
    else:
        raise AssertionError("na drie timeouts moet de fout doorkomen")


def test_telegram_timeout_laat_de_ronde_niet_vallen(monkeypatch):
    from kamerbot.telegram import Telegram

    def traag(*a, **kw):
        raise httpx.ReadTimeout("traag")

    monkeypatch.setattr(httpx, "post", traag)
    assert Telegram("t", "1").stuur("hoi") is False


def test_timeout_bij_reageren_houdt_de_rest_niet_tegen(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True

    class Traag(FakeSSH):
        def reageer(self, k):
            if k.flow_id == 2:
                raise httpx.ReadTimeout("traag")
            super().reageer(k)

    ssh, tg = Traag([kamer(2), kamer(3)], actief=3), FakeTG()
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.gereageerd == [3]
    assert any("ReadTimeout" in b for b in tg.berichten)


def test_ssh_500_wordt_opnieuw_geprobeerd_en_een_kamer_overgeslagen(tmp_path, monkeypatch):
    from kamerbot import ssh as sshmod
    monkeypatch.setattr(sshmod, "WACHT_S", (0, 0))
    codes = iter([500, 200])
    c = sshmod.SSHClient(tmp_path / "geen-cookies.json")
    c.http = httpx.Client(base_url=sshmod.BASE, transport=httpx.MockTransport(
        lambda req: httpx.Response(next(codes), json={"NumberOfActiveApplications": 2})))
    assert c.actieve_reacties() == 2

    cfg = Config()
    cfg.data_dir, cfg.auto_reageren = tmp_path, True

    class Kapot(FakeSSH):
        def details(self, k):
            if k.flow_id == 2:
                raise httpx.HTTPStatusError("500", request=httpx.Request("GET", "https://www.sshxl.nl/x"),
                                            response=httpx.Response(500))
            return k

    ssh = Kapot([kamer(2), kamer(3)], actief=3)
    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=GeenOordeel(), agenda=FakeAgenda())
    assert ssh.gereageerd == [3]


def test_overzicht_toont_reacties_op_volgorde_van_deadline(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.max_actief = tmp_path, 7
    nu = datetime.now(timezone.utc)
    Opslag(tmp_path / "kamerbot.db").zet("ssh", "1", "gereageerd")
    later = kamer(1, al_gereageerd=True, positie=2, aantal_reacties=5, deadline=nu + timedelta(days=2))
    eerder = kamer(2, al_gereageerd=True, positie=3, aantal_reacties=6, deadline=nu + timedelta(days=1))
    niet = kamer(3)
    tekst = overzicht(cfg, ssh=FakeSSH([later, eerder, niet], actief=2), tg=FakeTG())
    assert "2 van 7 plekken bezet" in tekst
    assert tekst.index("#3 van 6") < tekst.index("#2 van 5")
    assert "Biltstraat 3" not in tekst
    regel_eerder = [r for r in tekst.splitlines() if "#3 van 6" in r][0]
    assert "zelf geplaatst" in regel_eerder  # niet door de bot geplaatst
    assert "zelf geplaatst" not in [r for r in tekst.splitlines() if "#2 van 5" in r][0]


def test_overzicht_zonder_reacties(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    assert "Geen reacties" in overzicht(cfg, ssh=FakeSSH([kamer(1)], actief=0), tg=FakeTG())


def test_overzicht_telt_reacties_op_gesloten_kamers_mee(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.max_actief = tmp_path, 7
    tekst = overzicht(cfg, ssh=FakeSSH([kamer(1, al_gereageerd=True, positie=2)], actief=3), tg=FakeTG())
    assert "3 van 7 plekken bezet" in tekst
    assert "+ 2 reactie(s) op kamers die niet meer in het aanbod staan" in tekst


def test_ronde_ruilt_slechtste_handmatige_reactie(tmp_path):
    # 4 nov 2026: Jacobijnenstraat #30 (zelf geplaatst) moet wijken voor Amsterdamsestraatweg #3,
    # beide sluiten donderdag; Gerbrandypad #2 (van de bot, sluit vrijdag) blijft.
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    nu = datetime.now(timezone.utc)
    do, vr = nu + timedelta(hours=38), nu + timedelta(hours=62)
    Opslag(tmp_path / "kamerbot.db").zet("ssh", "5", "gereageerd")
    handmatig = [kamer(1, al_gereageerd=True, positie=30, deadline=do),
                 kamer(2, al_gereageerd=True, positie=14, deadline=do),
                 kamer(3, al_gereageerd=True, positie=15, deadline=do),
                 kamer(4, al_gereageerd=True, positie=9, deadline=do)]
    gerbrandy = kamer(5, al_gereageerd=True, positie=2, deadline=vr)
    amsterdamse = kamer(6, positie=3, deadline=do)

    class B:
        def beoordeel(self, k):
            return None

    # 7 bezet: 5 in het aanbod plus 2 op kamers die al gesloten zijn.
    ssh, tg = FakeSSH(handmatig + [gerbrandy, amsterdamse], actief=7), FakeTG()
    ronde(cfg, ssh=ssh, tg=tg, beoordelaar=B(), agenda=FakeAgenda())
    assert ssh.ingetrokken == [1]
    assert ssh.gereageerd == [6]
    assert any("plek 30, zelf geplaatst" in b for b in tg.berichten)


def test_handmatig_blijft_staan_als_het_uit_staat(tmp_path):
    cfg = Config()
    cfg.data_dir, cfg.auto_reageren, cfg.max_actief = tmp_path, True, 7
    cfg.intrekken_handmatig = False
    do = datetime.now(timezone.utc) + timedelta(hours=38)

    class B:
        def beoordeel(self, k):
            return None

    ssh = FakeSSH([kamer(1, al_gereageerd=True, positie=30, deadline=do), kamer(6, positie=3, deadline=do)], actief=7)
    ronde(cfg, ssh=ssh, tg=FakeTG(), beoordelaar=B(), agenda=FakeAgenda())
    assert ssh.ingetrokken == [] and ssh.gereageerd == []
