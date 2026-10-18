import random
from datetime import date, datetime, timedelta

from kamerbot.config import Config
from kamerbot.dienst import VENSTERS, Dienst, Planning
from kamerbot.opslag import Opslag
from kamerbot.ssh import NL


def planning(tmp_path, seed=1):
    return Planning(Opslag(tmp_path / "db.sqlite"), random.Random(seed))


def test_momenten_vallen_binnen_de_vensters(tmp_path):
    p = planning(tmp_path)
    for i in range(30):
        dag = date(2026, 10, 18) + timedelta(days=i)
        for naam, (begin, eind) in VENSTERS.items():
            m, _ = p.moment(dag, naam)
            assert begin <= m.time() <= eind


def test_moment_blijft_hetzelfde_na_herstart(tmp_path):
    a, _ = planning(tmp_path, 1).moment(date(2026, 10, 18), "overdag")
    b, _ = planning(tmp_path, 99).moment(date(2026, 10, 18), "overdag")
    assert a == b


def test_aan_de_beurt_en_klaar(tmp_path):
    p = planning(tmp_path)
    dag = date(2026, 10, 18)
    m, _ = p.moment(dag, "overdag")
    assert p.aan_de_beurt(m - timedelta(minutes=1)) is None
    assert p.aan_de_beurt(m) == "overdag"
    p.klaar(dag, "overdag")
    assert p.aan_de_beurt(m) is None


def test_gemist_venster_wordt_niet_ingehaald(tmp_path):
    p = planning(tmp_path)
    assert p.aan_de_beurt(datetime(2026, 10, 18, 20, 0, tzinfo=NL)) is None


def test_alleen_commandos_van_eigen_chat(tmp_path, monkeypatch):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.telegram_chat_id = "42"
    d = Dienst(cfg)
    verstuurd = []
    monkeypatch.setattr(d.tg, "stuur", lambda t: verstuurd.append(t) or True)
    d.verwerk({"chat": {"id": 7}, "text": "/update"})
    assert not (tmp_path / "update.request").exists()
    d.verwerk({"chat": {"id": 42}, "text": "/update"})
    assert (tmp_path / "update.request").exists()
    d.verwerk({"chat": {"id": 42}, "text": "/status"})
    assert "Volgende ronde:" in verstuurd[-1] and "Kansgrens: plek 30" in verstuurd[-1]


def test_meldingen_van_updatescript_worden_doorgestuurd(tmp_path, monkeypatch):
    cfg = Config()
    cfg.data_dir = tmp_path
    d = Dienst(cfg)
    verstuurd = []
    monkeypatch.setattr(d.tg, "stuur", lambda t: verstuurd.append(t) or True)
    (tmp_path / "notices").mkdir()
    (tmp_path / "notices" / "update-1.txt").write_text("✅ Bijgewerkt")
    d.meldingen_doorsturen()
    assert verstuurd == ["✅ Bijgewerkt"]
    assert not list((tmp_path / "notices").iterdir())
