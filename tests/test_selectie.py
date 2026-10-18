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


def test_slechte_positie_wordt_overgeslagen():
    cfg = Config()
    k = kamers()[1478630]
    k.positie = 31
    nu = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    assert selectie.kies([k], 5, cfg, nu) == []


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


def test_kansloos_na_zakken():
    cfg = Config()
    k = kamers()[1478671]
    k.al_gereageerd, k.positie = True, 34
    assert selectie.kansloos([k], cfg) == [k]
    k.positie = 30
    assert selectie.kansloos([k], cfg) == []


def test_verhaaltje_dat_alleen_een_vrouw_zoekt():
    zoekt = selectie.zoekt_alleen_vrouw
    assert zoekt("Wij zijn een gezellig huis en zoeken alleen een vrouw als nieuwe huisgenoot.")
    assert zoekt("We are looking for a female housemate!")
    assert zoekt("Wij zoeken een nieuwe meid voor ons huis")
    assert zoekt("Gezellig meidenhuis zoekt nieuwe bewoonster")
    assert zoekt("Nieuwe huisgenote (v) gezocht")
    assert not zoekt("We zoeken een leuke huisgenoot, man of vrouw maakt niet uit.")
    assert not zoekt("Wij zoeken een gezellige huisgenoot die mee-eet. M/V")
    assert not zoekt("Ons huis bestaat uit drie jongens en twee meiden.")
    k = Kamer(flow_id=1, wocas_id="1", soort="Hospiteren", type="Kamer", brutto_huur=500,
              deadline=datetime.now(timezone.utc), geslacht=None,
              verhaaltje="Wij zoeken alleen een vrouw!")
    assert selectie.detailfilter(k, Filters()) == "verhaaltje: zoekt een vrouw"
