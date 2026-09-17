"""Tests für die HTTP-Schnittstelle.

Laufen ohne Ollama und ohne Datei auf der Platte: Modell und Ablage werden
hereingereicht - die Attrappe und eine Datenbank im Arbeitsspeicher. Genau
dafür war die schmale Anbieter-Schnittstelle gedacht.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from belegleser.ablage import Ablage
from belegleser.api import baue_app
from belegleser.modell import Attrappe
from belegleser.testdaten import erzeuge_rechnung, schreibe_pdf

GUTE_ANTWORT = {
    "rechnungsnummer": "2026-0042",
    "rechnungsdatum": "2026-03-14",
    "lieferant_name": "Muster GmbH",
    "lieferant_uid": "ATU12345678",
    "positionen": [
        {"bezeichnung": "Beratung", "menge": 2, "einzelpreis": 150.00, "gesamtpreis": 300.00}
    ],
    "steuerzeilen": [{"satz": 20, "nettobetrag": 300.00, "ust_betrag": 60.00}],
    "nettobetrag": 300.00,
    "ust_betrag": 60.00,
    "bruttobetrag": 360.00,
}


@pytest.fixture
def beleg_pdf(tmp_path) -> tuple[Path, dict]:
    """Ein echtes PDF mit bekannten Sollwerten."""
    import random

    zufall = random.Random(7)
    rechnung = erzeuge_rechnung(zufall, gemischt=False)
    pfad = tmp_path / "beleg.pdf"
    schreibe_pdf(rechnung, pfad, zufall)
    return pfad, json.loads(rechnung.model_dump_json())


def baue(antworten: list[str]) -> TestClient:
    # ":memory:" - die Datenbank existiert nur waehrend des Tests.
    return TestClient(baue_app(modell=Attrappe(antworten), ablage=Ablage(":memory:")))


def test_gesundheit_meldet_bereit():
    antwort = baue([json.dumps(GUTE_ANTWORT)]).get("/gesundheit")
    assert antwort.status_code == 200
    assert antwort.json()["bereit"] is True


def test_beleg_abgeben_und_wiederfinden(beleg_pdf):
    pfad, soll = beleg_pdf
    # Die Attrappe antwortet mit den echten Sollwerten des erzeugten Belegs.
    klient = baue([json.dumps(soll)])

    with pfad.open("rb") as f:
        antwort = klient.post("/belege", files={"datei": ("beleg.pdf", f, "application/pdf")})

    assert antwort.status_code == 201
    vorgang = antwort.json()
    assert vorgang["status"] == "ok"
    assert vorgang["rechnung"]["rechnungsnummer"] == soll["rechnungsnummer"]

    # Der Vorgang muss danach unter seiner Kennung auffindbar sein - sonst
    # waere die Ablage sinnlos.
    nochmal = klient.get(f"/belege/{vorgang['id']}")
    assert nochmal.status_code == 200
    assert nochmal.json()["id"] == vorgang["id"]


def test_beanstandeter_beleg_landet_in_der_warteschlange(beleg_pdf):
    pfad, soll = beleg_pdf
    falsch = dict(soll, nettobetrag="99999.00")
    klient = baue([json.dumps(falsch)])

    with pfad.open("rb") as f:
        antwort = klient.post("/belege", files={"datei": ("beleg.pdf", f, "application/pdf")})
    assert antwort.json()["status"] == "pruefen"

    warteschlange = klient.get("/warteschlange").json()
    assert warteschlange["anzahl"] == 1
    assert warteschlange["vorgaenge"][0]["befunde"]


def test_freigabe_haelt_fest_wer_hingesehen_hat(beleg_pdf):
    pfad, soll = beleg_pdf
    klient = baue([json.dumps(dict(soll, nettobetrag="99999.00"))])

    with pfad.open("rb") as f:
        vorgang = klient.post(
            "/belege", files={"datei": ("beleg.pdf", f, "application/pdf")}
        ).json()

    antwort = klient.post(
        f"/belege/{vorgang['id']}/freigeben",
        json={"person": "u.veljic", "anmerkung": "Betrag im Original geprüft"},
    )
    assert antwort.status_code == 200
    frei = antwort.json()
    assert frei["status"] == "freigegeben"
    assert frei["freigegeben_von"] == "u.veljic"
    assert frei["freigegeben_am"]

    # Nach der Freigabe ist die Warteschlange leer.
    assert klient.get("/warteschlange").json()["anzahl"] == 0


def test_freigabe_kann_werte_berichtigen(beleg_pdf):
    """Wer nachsieht, trägt die richtige Zahl ein - das ist der Zweck der
    Warteschlange."""
    pfad, soll = beleg_pdf
    klient = baue([json.dumps(dict(soll, nettobetrag="99999.00"))])

    with pfad.open("rb") as f:
        vorgang = klient.post(
            "/belege", files={"datei": ("beleg.pdf", f, "application/pdf")}
        ).json()

    frei = klient.post(
        f"/belege/{vorgang['id']}/freigeben",
        json={"person": "u.veljic", "daten": soll},
    ).json()
    assert frei["rechnung"]["nettobetrag"] == soll["nettobetrag"]


def test_nur_pdf_wird_angenommen():
    klient = baue([json.dumps(GUTE_ANTWORT)])
    antwort = klient.post(
        "/belege", files={"datei": ("beleg.png", b"kein pdf", "image/png")}
    )
    assert antwort.status_code == 400
    assert "PDF" in antwort.json()["detail"]


def test_leere_datei_wird_abgelehnt():
    klient = baue([json.dumps(GUTE_ANTWORT)])
    antwort = klient.post("/belege", files={"datei": ("leer.pdf", b"", "application/pdf")})
    assert antwort.status_code == 400


def test_unbekannte_kennung_gibt_404():
    klient = baue([json.dumps(GUTE_ANTWORT)])
    assert klient.get("/belege/gibtsnicht").status_code == 404
    assert (
        klient.post("/belege/gibtsnicht/freigeben", json={"person": "x"}).status_code == 404
    )


def test_kennzahlen_zaehlen_mit(beleg_pdf):
    pfad, soll = beleg_pdf
    klient = baue([json.dumps(soll)])

    leer = klient.get("/kennzahlen").json()
    assert leer["vorgaenge_gesamt"] == 0

    with pfad.open("rb") as f:
        klient.post("/belege", files={"datei": ("beleg.pdf", f, "application/pdf")})

    nachher = klient.get("/kennzahlen").json()
    assert nachher["vorgaenge_gesamt"] == 1
    assert nachher["ohne_handarbeit"] == 1.0


def test_pdf_wird_nicht_auf_der_platte_behalten(beleg_pdf, tmp_path):
    """Belege enthalten Geschäftsdaten. Gespeichert werden die ausgelesenen
    Felder, nicht die Datei."""
    import tempfile

    pfad, soll = beleg_pdf
    vorher = set(Path(tempfile.gettempdir()).glob("*.pdf"))

    klient = baue([json.dumps(soll)])
    with pfad.open("rb") as f:
        klient.post("/belege", files={"datei": ("beleg.pdf", f, "application/pdf")})

    nachher = set(Path(tempfile.gettempdir()).glob("*.pdf"))
    assert nachher <= vorher, "Das hochgeladene PDF liegt noch im Temp-Ordner"
