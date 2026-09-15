"""Tests für die Plausibilitätsprüfungen.

Diese Tests sind der Beleg dafür, dass die Nachrechnungen wirklich greifen.
Ohne sie wäre "wir prüfen die Ausgabe des Modells" eine Behauptung.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from belegleser.schema import Position, Rechnung


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
        nettobetrag="349.90",
        ust_satz="20",
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
        Rechnung(**rechnung(nettobetrag="449.90", ust_betrag="89.98", bruttobetrag="539.88"))


def test_umsatzsteuer_muss_zum_satz_passen():
    with pytest.raises(ValidationError, match="USt ergibt|bei 20 % USt"):
        Rechnung(**rechnung(ust_betrag="34.99", bruttobetrag="384.89"))


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
        Rechnung(**rechnung(ust_satz="19", ust_betrag="66.48", bruttobetrag="416.38"))


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


def test_schema_laesst_sich_als_json_schema_ausgeben():
    """Dieselbe Klasse liefert die Vorgabe für das Sprachmodell.

    Damit können Vorgabe und Prüfung nicht auseinanderlaufen - es gibt nur eine
    Quelle für beides.
    """
    schema = Rechnung.model_json_schema()
    assert "rechnungsnummer" in schema["properties"]
    assert "positionen" in schema["properties"]
