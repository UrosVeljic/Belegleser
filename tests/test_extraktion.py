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
    "steuerzeilen": [{"satz": 20, "nettobetrag": 300.00, "ust_betrag": 60.00}],
    "nettobetrag": 300.00,
    "ust_betrag": 60.00,
    "bruttobetrag": 360.00,
}

# Der Text muss die Werte enthalten, die die Attrappe zurueckgibt - sonst
# meldet die Belegtreue-Pruefung sie zu Recht als erfunden.
BELEGTEXT = (
    "Muster GmbH\n"
    "UID: ATU12345678\n"
    "Rechnungsnummer: 2026-0042\n"
    "Beratung          2      150,00     300,00\n"
    "Nettobetrag 300,00\n"
    "USt 20 % 60,00\n"
    "Gesamtbetrag 360,00 EUR"
)


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
    daten = dict(
        GUTE_ANTWORT,
        steuerzeilen=[{"satz": 19, "nettobetrag": 300.00, "ust_betrag": 57.00}],
        ust_betrag=57.00,
        bruttobetrag=357.00,
    )
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


# --- Belegtreue ------------------------------------------------------------


def test_erfundene_rechnungsnummer_wird_erkannt():
    """Der Gegenspieler zur Nachrechnung.

    Eine Rechnungsnummer haengt mit keinem anderen Feld zusammen - die
    Nachrechnung kann sie nicht pruefen. Was man pruefen kann: ob sie im Beleg
    ueberhaupt vorkommt.
    """
    daten = dict(GUTE_ANTWORT, rechnungsnummer="9999-0001")
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.braucht_pruefung
    assert any("kommt im Beleg nicht vor" in b for b in ergebnis.befunde)


def test_uebersehene_uid_wird_aus_dem_beleg_ergaenzt():
    """Was ein striktes Format hat, findet man selbst.

    Eine oesterreichische UID ist ATU plus acht Ziffern. Uebersieht das Modell
    sie, wird sie eingesetzt - das ist kein Raten, sondern eine Ableitung aus
    dem Beleg.
    """
    daten = {k: v for k, v in GUTE_ANTWORT.items() if k != "lieferant_uid"}
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.status == "ok"
    assert ergebnis.rechnung.lieferant_uid == "ATU12345678"
    assert any("ergänzt" in e for e in ergebnis.ergaenzungen)


def test_ergaenzung_wird_protokolliert():
    """Sonst liesse sich die Leistung des Modells nicht mehr von der der
    Nachbesserung trennen."""
    daten = {k: v for k, v in GUTE_ANTWORT.items() if k != "lieferant_uid"}
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))
    assert ergebnis.ergaenzungen != []


def test_mehrere_uids_werden_nicht_geraten():
    """Stehen zwei UIDs auf dem Beleg - Lieferant und Empfaenger - ist nicht
    entscheidbar, welche gemeint ist. Dann wird nichts eingesetzt."""
    text = BELEGTEXT + "\nEmpfaenger UID: ATU87654321"
    daten = {k: v for k, v in GUTE_ANTWORT.items() if k != "lieferant_uid"}
    ergebnis = verarbeite_text(text, attrappe_mit(daten))

    assert ergebnis.braucht_pruefung
    assert ergebnis.rechnung.lieferant_uid is None
    assert any("nicht gelesen" in b for b in ergebnis.befunde)


def test_belegtreue_stoert_gueltige_belege_nicht():
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(GUTE_ANTWORT))
    assert ergebnis.status == "ok"
    assert ergebnis.befunde == []


def test_angehaengte_menge_wird_aus_der_bezeichnung_entfernt():
    """Beobachtet in der Messung: Das Modell haengt die Menge aus der
    Nachbarspalte an die Bezeichnung.

    Geprueft wird gegen den Belegtext: Der ist spaltenweise ausgerichtet, also
    kann "Beratung 2" mit einem Leerzeichen nicht aus einer Zeile stammen, in
    der Bezeichnung und Menge verschiedene Spalten sind.
    """
    daten = dict(
        GUTE_ANTWORT,
        positionen=[
            {"bezeichnung": "Beratung 2", "menge": 2, "einzelpreis": 150.00,
             "gesamtpreis": 300.00}
        ],
    )
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.status == "ok"
    assert ergebnis.rechnung.positionen[0].bezeichnung == "Beratung"
    assert any("bereinigt" in e for e in ergebnis.ergaenzungen)


def test_bezeichnung_die_echt_auf_eine_zahl_endet_bleibt():
    """Gegenprobe: 'Fachbuch Band 3' steht so im Beleg und darf nicht
    beschnitten werden."""
    text = BELEGTEXT.replace(
        "Beratung          2      150,00     300,00",
        "Fachbuch Band 3          2      150,00     300,00",
    )
    daten = dict(
        GUTE_ANTWORT,
        positionen=[
            {"bezeichnung": "Fachbuch Band 3", "menge": 2, "einzelpreis": 150.00,
             "gesamtpreis": 300.00}
        ],
    )
    ergebnis = verarbeite_text(text, attrappe_mit(daten))
    assert ergebnis.rechnung.positionen[0].bezeichnung == "Fachbuch Band 3"


# --- Vorbesserung ----------------------------------------------------------

RABATT_TEXT = (
    "Grosshandel Wien GmbH\n"
    "UID: ATU12345678\n"
    "Rechnungsnummer: 2026-0042\n"
    "Ware              10      100,00   1.000,00\n"
    "Zwischensumme  1.000,00\n"
    "Rabatt 10 %      -100,00\n"
    "Nettobetrag      900,00\n"
    "USt 20 %         180,00\n"
    "Gesamtbetrag  1.080,00 EUR"
)

RABATT_ANTWORT = {
    "rechnungsnummer": "2026-0042",
    "rechnungsdatum": "2026-03-14",
    "lieferant_name": "Grosshandel Wien GmbH",
    "lieferant_uid": "ATU12345678",
    "positionen": [
        {"bezeichnung": "Ware", "menge": 10, "einzelpreis": 100.00, "gesamtpreis": 1000.00}
    ],
    "steuerzeilen": [{"satz": 20, "nettobetrag": 900.00, "ust_betrag": 180.00}],
    "nettobetrag": 900.00,
    "ust_betrag": 180.00,
    "bruttobetrag": 1080.00,
}


def test_uebersehener_rabatt_wird_errechnet():
    """Beobachtet in der Messung: Das Modell liest den verminderten
    Nettobetrag richtig, uebernimmt den Abzug darueber aber nicht.

    Die Hoehe ist reine Arithmetik - Positionssumme minus Netto. Abgesichert
    wird sie dreifach: Die Luecke muss positiv sein, im Beleg muss ein
    Abzugswort stehen, und der errechnete Betrag muss dort auftauchen.
    """
    ergebnis = verarbeite_text(RABATT_TEXT, attrappe_mit(RABATT_ANTWORT))

    assert ergebnis.status == "ok"
    assert ergebnis.rechnung.rabatt is not None
    assert str(ergebnis.rechnung.rabatt.betrag) == "100.00"
    assert any("aus der Differenz" in e for e in ergebnis.ergaenzungen)


def test_rabatt_mit_betrag_null_wird_entfernt():
    """Manche Modelle druecken 'kein Rabatt' als Betrag 0 aus, statt das Feld
    wegzulassen. Ohne Vorbesserung scheitert daran die Schemapruefung."""
    daten = dict(GUTE_ANTWORT, rabatt={"bezeichnung": "Rabatt", "betrag": 0})
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.status == "ok"
    assert ergebnis.rechnung.rabatt is None
    assert any("Betrag 0 entfernt" in e for e in ergebnis.ergaenzungen)


def test_ohne_abzugswort_wird_nichts_erfunden():
    """Gegenprobe. Passt die Summe nicht und steht im Beleg kein Abzugswort,
    ist es ein Lesefehler - der gehoert in die Warteschlange, nicht zugedeckt."""
    text = RABATT_TEXT.replace("Rabatt 10 %      -100,00\n", "")
    ergebnis = verarbeite_text(text, attrappe_mit(RABATT_ANTWORT))

    assert ergebnis.braucht_pruefung
    assert any("Positionen ergeben" in b for b in ergebnis.befunde)


def test_betrag_muss_im_beleg_stehen():
    """Zweite Gegenprobe: Das Wort 'Rabatt' allein genuegt nicht - der
    errechnete Betrag muss im Beleg auch vorkommen."""
    text = RABATT_TEXT.replace("-100,00", "-99,00")
    ergebnis = verarbeite_text(text, attrappe_mit(RABATT_ANTWORT))
    assert ergebnis.braucht_pruefung


def test_zusammengesetzte_rabattwoerter_werden_erkannt():
    """Deutsche Belege schreiben 'Kundenrabatt', nicht 'Rabatt'.

    Die erste Fassung suchte mit Wortgrenzen nach 'rabatt' und fand davon
    keines - die Reparatur griff nur bei Belegen, auf denen schlicht 'Rabatt'
    stand. In der Messung waren das drei von zwoelf Belegen.
    """
    for wort in ["Kundenrabatt", "Mengenrabatt", "Sonderrabatt", "Nachlass"]:
        text = RABATT_TEXT.replace("Rabatt 10 %", f"{wort} 10 %")
        ergebnis = verarbeite_text(text, attrappe_mit(RABATT_ANTWORT))
        assert ergebnis.status == "ok", f"{wort} wurde nicht erkannt"
        assert ergebnis.rechnung.rabatt is not None


def test_skonto_gilt_nicht_als_abzug():
    """Gegenprobe: Skonto ist keine Minderung. Steht nur Skonto im Beleg und
    die Summe passt nicht, ist es ein Lesefehler."""
    text = RABATT_TEXT.replace("Rabatt 10 %      -100,00", "3 % Skonto binnen 14 Tagen")
    ergebnis = verarbeite_text(text, attrappe_mit(RABATT_ANTWORT))
    assert ergebnis.braucht_pruefung


# --- Befunde aus echten Rechnungen -----------------------------------------


def test_iban_wird_nicht_als_uid_uebernommen():
    """Beobachtet: Das Modell lieferte 'AT474300045101844020' als UID - das ist
    eine IBAN. Beide beginnen mit AT.

    Vorher warf die Formatpruefung den ganzen Beleg in die Warteschlange,
    obwohl die richtige UID im selben Text stand.
    """
    daten = dict(GUTE_ANTWORT, lieferant_uid="AT474300045101844020")
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(daten))

    assert ergebnis.status == "ok"
    assert ergebnis.rechnung.lieferant_uid == "ATU12345678"
    assert any("als UID verworfen" in e for e in ergebnis.ergaenzungen)


BRUTTO_TEXT = (
    "Wiener Linien GmbH & Co KG\n"
    "UID-Nr. ATU47055001\n"
    "Rechnungsnummer: 20260000025088\n"
    "Menge Bezeichnung      Betrag netto  USt in %  USt in EUR  Betrag brutto\n"
    "  1   Jahreskarte           424,55     10,00      42,45         467,00\n"
    "Rechnungsbetrag netto 424,55\n"
    "USt 42,45\n"
    "Rechnungsbetrag brutto 467,00"
)

BRUTTO_ANTWORT = {
    "rechnungsnummer": "20260000025088",
    "rechnungsdatum": "2026-02-09",
    "lieferant_name": "Wiener Linien GmbH & Co KG",
    "lieferant_uid": "ATU47055001",
    # Das Modell hat die Bruttospalte genommen:
    "positionen": [
        {"bezeichnung": "Jahreskarte", "menge": 1, "einzelpreis": 467.00,
         "gesamtpreis": 467.00, "ust_satz": 10}
    ],
    "steuerzeilen": [{"satz": 10, "nettobetrag": 424.55, "ust_betrag": 42.45}],
    "nettobetrag": 424.55,
    "ust_betrag": 42.45,
    "bruttobetrag": 467.00,
}


def test_bruttopositionen_werden_zurueckgerechnet():
    """Endkundenrechnungen fuehren beide Spalten. Nimmt das Modell die falsche,
    ergeben die Positionen den Bruttobetrag der Rechnung - daran ist es
    erkennbar, und netto = brutto / (1 + Satz/100) ist eine Division."""
    ergebnis = verarbeite_text(BRUTTO_TEXT, attrappe_mit(BRUTTO_ANTWORT))

    assert ergebnis.status == "ok"
    assert str(ergebnis.rechnung.positionen[0].gesamtpreis) == "424.55"
    assert any("Bruttobeträge" in e for e in ergebnis.ergaenzungen)


def test_ohne_steuersatz_wird_nicht_zurueckgerechnet():
    """Gegenprobe: Ohne Satz je Position ist die Umrechnung nicht eindeutig.
    Dann gehoert der Beleg in die Warteschlange, nicht zurechtgebogen."""
    ohne_satz = dict(BRUTTO_ANTWORT)
    ohne_satz["positionen"] = [
        {k: v for k, v in p.items() if k != "ust_satz"} for p in BRUTTO_ANTWORT["positionen"]
    ]
    ergebnis = verarbeite_text(BRUTTO_TEXT, attrappe_mit(ohne_satz))
    assert ergebnis.braucht_pruefung


def test_stimmige_nettopositionen_bleiben_unberuehrt():
    """Zweite Gegenprobe: Wo die Positionen bereits netto sind, darf nichts
    umgerechnet werden."""
    ergebnis = verarbeite_text(BELEGTEXT, attrappe_mit(GUTE_ANTWORT))
    assert ergebnis.status == "ok"
    assert str(ergebnis.rechnung.positionen[0].gesamtpreis) == "300.0"
    assert not any("Bruttobeträge" in e for e in ergebnis.ergaenzungen)
