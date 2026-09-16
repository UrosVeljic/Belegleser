"""Tests für die Plausibilitätsprüfungen.

Diese Tests sind der Beleg dafür, dass die Nachrechnungen wirklich greifen.
Ohne sie wäre "wir prüfen die Ausgabe des Modells" eine Behauptung.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from belegleser.schema import Position, Rechnung, Steuerzeile


def rechnung(**abweichungen):
    """Eine in sich stimmige Rechnung, einzelne Felder überschreibbar.

    2 × 150,00 = 300,00 plus 1 × 49,90 = 49,90  ->  Netto 349,90
    349,90 bei 20 % USt = 69,98                 ->  Brutto 419,88
    """
    grund = dict(
        rechnungsnummer="2026-0042",
        rechnungsdatum="2026-03-14",
        lieferant_name="Muster GmbH",
        lieferant_uid="ATU12345678",
        positionen=[
            {"bezeichnung": "Beratung", "menge": "2", "einzelpreis": "150.00", "gesamtpreis": "300.00"},
            {"bezeichnung": "Material", "menge": "1", "einzelpreis": "49.90", "gesamtpreis": "49.90"},
        ],
        steuerzeilen=[{"satz": "20", "nettobetrag": "349.90", "ust_betrag": "69.98"}],
        nettobetrag="349.90",
        ust_betrag="69.98",
        bruttobetrag="419.88",
    )
    grund.update(abweichungen)
    return grund


def test_stimmige_rechnung_geht_durch():
    r = Rechnung(**rechnung())
    assert r.nettobetrag == Decimal("349.90")
    assert r.lieferant_uid == "ATU12345678"


def test_positionen_muessen_den_nettobetrag_ergeben():
    # Klassischer Lesefehler: eine Ziffer verrutscht.
    with pytest.raises(ValidationError, match="Positionen ergeben"):
        Rechnung(
            **rechnung(
                nettobetrag="449.90",
                ust_betrag="89.98",
                bruttobetrag="539.88",
                steuerzeilen=[{"satz": "20", "nettobetrag": "449.90", "ust_betrag": "89.98"}],
            )
        )


def test_umsatzsteuer_muss_zum_satz_passen():
    with pytest.raises(ValidationError, match="ergibt"):
        Rechnung(
            **rechnung(
                ust_betrag="34.99",
                bruttobetrag="384.89",
                steuerzeilen=[{"satz": "20", "nettobetrag": "349.90", "ust_betrag": "34.99"}],
            )
        )


def test_brutto_muss_summe_aus_netto_und_ust_sein():
    with pytest.raises(ValidationError, match="Bruttobetrag lautet"):
        Rechnung(**rechnung(bruttobetrag="400.00"))


def test_mehrere_fehler_werden_zusammen_gemeldet():
    # Netto falsch -> zieht USt und Brutto mit. Alle drei sollen in der
    # Meldung stehen, nicht nur der erste.
    with pytest.raises(ValidationError) as fehler:
        Rechnung(**rechnung(nettobetrag="500.00"))
    text = str(fehler.value)
    assert "Positionen ergeben" in text
    assert "USt" in text


def test_ungueltiger_ust_satz_wird_abgelehnt():
    # 19 % ist der deutsche Satz - in Österreich gibt es ihn nicht.
    with pytest.raises(ValidationError, match="kein gültiger USt-Satz"):
        Rechnung(
            **rechnung(
                ust_betrag="66.48",
                bruttobetrag="416.38",
                steuerzeilen=[{"satz": "19", "nettobetrag": "349.90", "ust_betrag": "66.48"}],
            )
        )


def test_uid_format_wird_geprueft():
    with pytest.raises(ValidationError, match="keine österreichische UID"):
        Rechnung(**rechnung(lieferant_uid="DE123456789"))


def test_uid_wird_normalisiert():
    r = Rechnung(**rechnung(lieferant_uid="atu 1234 5678"))
    assert r.lieferant_uid == "ATU12345678"


def test_uid_darf_fehlen():
    # Kleinbetragsrechnungen brauchen keine UID des Lieferanten.
    r = Rechnung(**rechnung(lieferant_uid=None))
    assert r.lieferant_uid is None


def test_position_muss_aufrechnen():
    with pytest.raises(ValidationError, match="ergibt"):
        Position(bezeichnung="Beratung", menge="2", einzelpreis="150.00", gesamtpreis="250.00")


def test_rechnung_ohne_positionen_wird_abgelehnt():
    with pytest.raises(ValidationError):
        Rechnung(**rechnung(positionen=[], nettobetrag="0", ust_betrag="0", bruttobetrag="0"))


# --- Mehrere Steuersätze ---------------------------------------------------


def hotelrechnung(**abweichungen):
    """Naechtigung mit 13 %, Getraenke mit 20 % - der Alltagsfall.

    196,00 bei 13 %  ->  25,48
     20,00 bei 20 %  ->   4,00
    netto 216,00, USt 29,48, brutto 245,48
    """
    grund = dict(
        rechnungsnummer="2026-0100",
        rechnungsdatum="2026-05-02",
        lieferant_name="Hotel Alpenblick GmbH",
        positionen=[
            {"bezeichnung": "Nächtigung", "menge": "2", "einzelpreis": "98.00",
             "gesamtpreis": "196.00", "ust_satz": "13"},
            {"bezeichnung": "Getränke", "menge": "4", "einzelpreis": "5.00",
             "gesamtpreis": "20.00", "ust_satz": "20"},
        ],
        steuerzeilen=[
            {"satz": "13", "nettobetrag": "196.00", "ust_betrag": "25.48"},
            {"satz": "20", "nettobetrag": "20.00", "ust_betrag": "4.00"},
        ],
        nettobetrag="216.00",
        ust_betrag="29.48",
        bruttobetrag="245.48",
    )
    grund.update(abweichungen)
    return grund


def test_zwei_steuersaetze_gehen_durch():
    r = Rechnung(**hotelrechnung())
    assert [str(s) for s in r.ust_saetze] == ["13", "20"]


def test_aufschluesselung_muss_den_nettobetrag_ergeben():
    # Eine Steuerzeile fehlt - die Summe passt dann nicht mehr.
    daten = hotelrechnung(
        steuerzeilen=[{"satz": "13", "nettobetrag": "196.00", "ust_betrag": "25.48"}]
    )
    with pytest.raises(ValidationError, match="Steueraufschlüsselung ergibt netto"):
        Rechnung(**daten)


def test_positionen_muessen_zur_jeweiligen_steuerzeile_passen():
    """Die schaerfste Pruefung: Sie rechnet pro Satz nach.

    Hier wurde eine Position dem falschen Satz zugeordnet - in Summe stimmt
    alles, nur die Aufteilung nicht. Ohne diese Pruefung ginge das durch.
    """
    daten = hotelrechnung(
        positionen=[
            {"bezeichnung": "Nächtigung", "menge": "2", "einzelpreis": "98.00",
             "gesamtpreis": "196.00", "ust_satz": "20"},
            {"bezeichnung": "Getränke", "menge": "4", "einzelpreis": "5.00",
             "gesamtpreis": "20.00", "ust_satz": "13"},
        ]
    )
    with pytest.raises(ValidationError, match="Positionen mit"):
        Rechnung(**daten)


def test_doppelter_steuersatz_wird_abgelehnt():
    """Zwei Zeilen mit demselben Satz heisst: eine wurde doppelt gelesen."""
    daten = hotelrechnung(
        steuerzeilen=[
            {"satz": "13", "nettobetrag": "108.00", "ust_betrag": "14.04"},
            {"satz": "13", "nettobetrag": "108.00", "ust_betrag": "14.04"},
        ],
        nettobetrag="216.00",
        ust_betrag="28.08",
        bruttobetrag="244.08",
    )
    with pytest.raises(ValidationError, match="mehrfach vor"):
        Rechnung(**daten)


def test_steuerzeile_rechnet_fuer_sich():
    with pytest.raises(ValidationError, match="Steuerzeile 13 %"):
        Steuerzeile(satz="13", nettobetrag="196.00", ust_betrag="39.20")


def test_schema_laesst_sich_als_json_schema_ausgeben():
    """Dieselbe Klasse liefert die Vorgabe für das Sprachmodell.

    Damit können Vorgabe und Prüfung nicht auseinanderlaufen - es gibt nur eine
    Quelle für beides.
    """
    schema = Rechnung.model_json_schema()
    assert "rechnungsnummer" in schema["properties"]
    assert "positionen" in schema["properties"]


# --- Rabatt und Skonto -----------------------------------------------------


def mit_rabatt(**abweichungen):
    """1.000,00 minus 10 % Rabatt = 900,00 netto, 20 % USt = 180,00."""
    grund = dict(
        rechnungsnummer="2026-0200",
        rechnungsdatum="2026-06-01",
        lieferant_name="Grosshandel Wien GmbH",
        positionen=[
            {"bezeichnung": "Ware", "menge": "10", "einzelpreis": "100.00",
             "gesamtpreis": "1000.00"},
        ],
        rabatt={"bezeichnung": "Mengenrabatt", "prozent": "10", "betrag": "100.00"},
        steuerzeilen=[{"satz": "20", "nettobetrag": "900.00", "ust_betrag": "180.00"}],
        nettobetrag="900.00",
        ust_betrag="180.00",
        bruttobetrag="1080.00",
    )
    grund.update(abweichungen)
    return grund


def test_rabatt_geht_durch():
    r = Rechnung(**mit_rabatt())
    assert r.rabatt.betrag == Decimal("100.00")
    assert r.nettobetrag == Decimal("900.00")


def test_rabatt_muss_zur_prozentangabe_passen():
    # 10 % von 1.000 sind 100, nicht 150.
    daten = mit_rabatt(
        rabatt={"bezeichnung": "Rabatt", "prozent": "10", "betrag": "150.00"},
        steuerzeilen=[{"satz": "20", "nettobetrag": "850.00", "ust_betrag": "170.00"}],
        nettobetrag="850.00", ust_betrag="170.00", bruttobetrag="1020.00",
    )
    with pytest.raises(ValidationError, match="Rabatt 10 % auf"):
        Rechnung(**daten)


def test_vergessener_rabatt_wird_erkannt():
    """Der Abzug steht auf dem Beleg, wurde aber nicht gelesen."""
    daten = mit_rabatt(rabatt=None)
    with pytest.raises(ValidationError, match="Positionen ergeben"):
        Rechnung(**daten)


def test_faelschlich_abgezogenes_skonto_wird_erkannt():
    """Der wichtigste Test dieses Abschnitts.

    Skonto ist eine Zahlungsbedingung, keine Minderung. Zieht ein Extraktor es
    trotzdem ab - hier 3 % von 900 -, passt die Rechenkette nicht mehr. Ohne
    Nachrechnung ginge das durch jede Formatpruefung.
    """
    daten = mit_rabatt(
        skonto={"prozent": "3", "tage": "14"},
        steuerzeilen=[{"satz": "20", "nettobetrag": "873.00", "ust_betrag": "174.60"}],
        nettobetrag="873.00", ust_betrag="174.60", bruttobetrag="1047.60",
    )
    with pytest.raises(ValidationError, match="minus Rabatt"):
        Rechnung(**daten)


def test_skonto_aendert_die_summen_nicht():
    r = Rechnung(**mit_rabatt(skonto={"prozent": "3", "tage": "14"}))
    assert r.bruttobetrag == Decimal("1080.00")
    assert r.skonto.tage == 14
