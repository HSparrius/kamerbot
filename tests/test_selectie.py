import json
from datetime import datetime, timezone
from pathlib import Path

from kamerbot import selectie
from kamerbot.config import Config, Filters
from kamerbot.ssh import Kamer, kamers_uit_lijst, vul_details_aan

FIX = Path(__file__).parent / "fixtures"


def laad(naam):
    return json.loads((FIX / f"{naam}.json").read_text())


def kamers():
    return {k.flow_id: k for k in kamers_uit_lijst(laad("offers"), laad("details"))}


def test_lijst_wordt_gekoppeld_aan_adressen():
    ks = kamers()
    assert len(ks) == 36
    k = ks[1478671]
    assert (k.straat, k.stad, k.m2, k.brutto_huur, k.soort) == ("Cambridgelaan", "UTRECHT", 18.0, 728.5, "Hospiteren")


def test_basisfilter_houdt_ibb_pythagoraslaan_en_andere_steden_tegen():
    f = Filters()
    redenen = {fid: selectie.basisfilter(k, f) for fid, k in kamers().items()}
    assert "Ina Boudier" in redenen[1478664]
    assert "Pythagoraslaan" in redenen[1478666]
    assert "Zeist" in redenen[1478676]
    assert "Tilburg" in redenen[1478635]
    assert "Cambridgelaan" in redenen[next(i for i, k in kamers().items() if k.straat == "Cambridgelaan")]
    assert sum(r is None for r in redenen.values()) == 19  # 9 op Science Park


def test_detailfilter_houdt_vrouwenhuis_tegen():
    k = vul_details_aan(kamers()[1478666], laad("offer")["1478666"])
    assert k.geslacht == "female"
    assert selectie.detailfilter(k, Filters()) is not None


def test_cambridgelaan_al_gereageerd_niet_opnieuw():
    k = vul_details_aan(kamers()[1478671], laad("offer")["1478671"])
    # Fixture is opgenomen ná de reactie: positie bleef 7, teller ging naar 37.
    assert k.positie == 7 and k.aantal_reacties == 37
    assert k.al_gereageerd is True
    assert selectie.detailfilter(k, Filters()) == "al gereageerd"


def test_kies_wacht_tot_deadline_en_vult_alleen_vrije_plekken():
    cfg = Config()
    ks = [k for k in kamers().values() if selectie.basisfilter(k, cfg.filters) is None]
    for i, k in enumerate(ks):
        k.positie = i + 1
    nu = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    gekozen = selectie.kies(ks, 2, cfg, nu)
    assert len(gekozen) == 2
    assert all((k.deadline - nu).total_seconds() <= 18 * 3600 for k in gekozen)
    assert selectie.kies(ks, 0, cfg, nu) == []


def test_plekgrens_alleen_als_hij_gezet_is():
    cfg = Config()
    cfg.max_positie = 30
    k = kamers()[1478630]
    k.positie = 31
    nu = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    assert selectie.kies([k], 5, cfg, nu) == []
    cfg.max_positie = 0
    assert selectie.kies([k], 5, cfg, nu) == [k]


def test_leeg_geslacht_telt_als_geen_voorkeur():
    k = kamers()[1478608]
    k.geslacht = ""
    assert selectie.detailfilter(k, Filters()) is None


def test_deadline_is_nederlandse_tijd():
    k = kamers()[1478671]
    assert k.deadline.isoformat() == "2026-10-20T23:59:59+02:00"


def test_snelst_verlopende_eerst():
    cfg = Config()
    ks = [k for k in kamers().values() if selectie.basisfilter(k, cfg.filters) is None]
    for k in ks:
        k.positie = 5
    nu = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    cfg.reageer_venster_uur = 100
    gekozen = selectie.kies(ks, 3, cfg, nu)
    assert [k.deadline for k in gekozen] == sorted(k.deadline for k in gekozen)
    assert gekozen[0].deadline == min(k.deadline for k in ks)


def test_binnen_24_uur_gaat_voor_dan_laagste_plek():
    from datetime import timedelta
    cfg = Config()
    cfg.reageer_venster_uur = 999
    nu = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ks = list(kamers().values())[:4]
    for k, uur, plek in zip(ks, (60, 20, 10, 50), (2, 40, 25, 9)):
        k.deadline, k.positie = nu + timedelta(hours=uur), plek
    gekozen = selectie.kies(ks, 4, cfg, nu)
    assert [k.positie for k in gekozen] == [25, 40, 2, 9]


def test_bij_te_veel_valt_de_slechtste_plek_af():
    from datetime import timedelta
    nu = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ks = list(kamers().values())[:5]
    eigen_goed, eigen_slecht, eigen_later, nieuw_24u, nieuw_slecht = ks
    for k, uur, plek in ((eigen_goed, 30, 5), (eigen_slecht, 30, 60), (eigen_later, 70, 3),
                         (nieuw_24u, 10, 45), (nieuw_slecht, 40, 80)):
        k.deadline, k.positie = nu + timedelta(hours=uur), plek
    eigen, nieuw = [eigen_goed, eigen_slecht, eigen_later], [nieuw_24u, nieuw_slecht]
    # Plek genoeg: niets intrekken, overal op reageren.
    assert selectie.verdeel(eigen, nieuw, 5, nu) == ([], nieuw)
    # Eén plek te weinig: de slechtste plek buiten 24 uur (80) valt af.
    assert selectie.verdeel(eigen, nieuw, 4, nu) == ([], [nieuw_24u])
    # Nog een te weinig: dan je bestaande reactie op plek 60.
    assert selectie.verdeel(eigen, nieuw, 3, nu) == ([eigen_slecht], [nieuw_24u])
