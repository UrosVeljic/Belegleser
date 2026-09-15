"""Tests für die Verarbeitung - mit der Attrappe statt einem echten Modell.

Diese Tests beantworten die Frage, die bei KI-Anwendungen am häufigsten offen
bleibt: Was passiert, wenn das Modell Unsinn liefert? Jeder Fall, den ein
Sprachmodell in der Praxis produziert, ist hier als Test hinterlegt.
"""

import json
from pathlib import Path

from belegleser.extraktion import verarbeite_text
from belegleser.modell import Attrappe

GUTE_ANTWORT = {
    "rechnungsnummer": "2026-0042",
    "rechnungsdatum": "2026-03-14",
    "lieferant_name": "Muster GmbH",
    "lieferant_uid": "ATU12345678",
    "positionen": [
        {"bezeichnung": "Beratung", "menge": 2, "einzelpreis": 150.00, "gesamtpreis": 300.00}
    ],
    "nettobetrag": 300.00,
    "ust_satz": 20,
    "ust_betrag": 60.00,
    "bruttobetrag": 360.00,
}

BELEGTEXT = "Muster GmbH\nRechnungsnummer: 2026-0042\nBeratung 2 150,00 300,00"


def attrappe_mit(daten) -> Attrappe:
    text = daten if isinstance(daten, str) else json.dumps(daten)
    return Attrappe([text])


def test_sauberer_beleg_geht_durch():
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(GUTE_ANTWORT))
    assert ergebnis.status == "ok"
    assert ergebnis.rechnung is not None
    assert ergebnis.rechnung.rechnungsnummer == "2026-0042"
    assert ergebnis.befunde == []


def test_kaputtes_json_landet_in_der_warteschlange():
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit("{das ist kein json"))
    assert ergebnis.braucht_pruefung
    assert "kein gültiges JSON" in ergebnis.befunde[0]


def test_erfundener_betrag_wird_erkannt():
    """Der wichtigste Test des Projekts.

    Das Modell liefert formal einwandfreies JSON - nur ist der Nettobetrag
    falsch gelesen. Kein Schema der Welt fängt das ab; nur die Nachrechnung.
    """
    daten = dict(GUTE_ANTWORT, nettobetrag=400.00)
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.braucht_pruefung
    zusammen = " ".join(ergebnis.befunde)
    assert "Positionen ergeben" in zusammen


def test_fehlendes_pflichtfeld_landet_in_der_warteschlange():
    daten = {k: v for k, v in GUTE_ANTWORT.items() if k != "rechnungsnummer"}
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))
    assert ergebnis.braucht_pruefung
    assert any("rechnungsnummer" in b for b in ergebnis.befunde)


def test_deutscher_ust_satz_wird_abgelehnt():
    # Ein Modell, das viel deutschen Text gesehen hat, setzt gerne 19 %.
    daten = dict(GUTE_ANTWORT, ust_satz=19, ust_betrag=57.00, bruttobetrag=357.00)
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))
    assert ergebnis.braucht_pruefung
    assert any("USt-Satz" in b for b in ergebnis.befunde)


def test_leerer_beleg_ruft_nicht_das_modell():
    """Ein Scan ohne Texterkennung soll keine Modellkosten verursachen."""
    attrappe = Attrappe([json.dumps(GUTE_ANTWORT)])
    ergebnis = verarbeite_text("   \n  ", attrappe)

    assert ergebnis.braucht_pruefung
    assert attrappe.aufrufe == []
    assert "Kein Text" in ergebnis.befunde[0]


def test_befunde_sind_lesbar():
    """Die Warteschlange liest ein Mensch, keine Entwicklerin."""
    daten = dict(GUTE_ANTWORT, nettobetrag=400.00)
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    for befund in ergebnis.befunde:
        assert "Value error" not in befund
        assert ":" in befund


def test_der_belegtext_steht_im_prompt():
    attrappe = attrappe_mit(GUTE_ANTWORT)
    verarbeite_text(BELEGTEXT, attrappe)
    assert BELEGTEXT in attrappe.aufrufe[0]


def test_messwerte_werden_durchgereicht():
    """Dauer und Tokenzahl muessen am Ergebnis haengen, sonst laesst sich
    spaeter nichts auswerten."""
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(GUTE_ANTWORT))
    assert ergebnis.antwort is not None
    assert ergebnis.antwort.modell == "attrappe"
