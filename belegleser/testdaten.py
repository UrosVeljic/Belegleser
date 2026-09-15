"""Erzeugt synthetische Rechnungen als PDF - samt bekannter Sollwerte.

Warum selbst erzeugen statt echte Belege sammeln:

1. Echte Rechnungen enthalten personenbezogene Daten und Geschäftsgeheimnisse.
   Sie gehören in kein öffentliches Repository.
2. Bei einem gesammelten Beleg muss jemand von Hand eintippen, was richtig
   gewesen wäre. Das ist mühsam und selbst fehleranfällig.
3. Hier ist es umgekehrt: Wir würfeln zuerst die Werte, rechnen die Rechnung
   daraus aus und drucken sie anschliessend. Die Wahrheit steht also schon
   fest, bevor das PDF existiert.

Damit lässt sich ein Testsatz beliebiger Grösse erzeugen, und die Messung der
Genauigkeit braucht keine Handarbeit.

Bewusst eingebaute Stolpersteine, weil sie in der Praxis am häufigsten zu
Lesefehlern führen:

- deutsche Zahlenschreibweise: 1.234,56 statt 1,234.56
- Umlaute in Bezeichnungen
- wechselnde Beschriftungen: "Rechnungs-Nr.", "Rechnungsnummer", "RE-Nr."
- Kleinbetragsrechnungen ohne UID-Nummer
- verschiedene Umsatzsteuersätze im selben Testsatz
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from belegleser.schema import Position, Rechnung

CENT = Decimal("0.01")

LIEFERANTEN = [
    "Wiener Bürotechnik GmbH",
    "Ostermayer & Söhne KG",
    "Kärntner Werkstoffe AG",
    "Grünfeld Consulting e.U.",
    "Donau Logistik GmbH",
    "Salzburger Möbelhaus GmbH",
]

LEISTUNGEN = [
    ("Beratungsstunde", Decimal("120.00"), Decimal("20")),
    ("Softwarelizenz Jahresabo", Decimal("480.00"), Decimal("20")),
    ("Bürostuhl ergonomisch", Decimal("349.00"), Decimal("20")),
    ("Druckerpapier A4, Karton", Decimal("42.50"), Decimal("20")),
    ("Schulungsunterlagen", Decimal("28.00"), Decimal("10")),
    ("Fachbuch Datenschutz", Decimal("64.90"), Decimal("10")),
    ("Nächtigung Einzelzimmer", Decimal("98.00"), Decimal("13")),
    ("Wartungspauschale Server", Decimal("890.00"), Decimal("20")),
    ("Kabelkanal, 2 m", Decimal("15.80"), Decimal("20")),
    ("Übersetzung DE-EN, Seite", Decimal("55.00"), Decimal("20")),
]

# Verschiedene Beschriftungen fuer dasselbe Feld. Ein Extraktor, der nur eine
# davon kennt, faellt bei den anderen durch - genau das wollen wir messen.
BESCHRIFTUNG_NUMMER = ["Rechnungsnummer", "Rechnungs-Nr.", "RE-Nr.", "Beleg-Nr."]
BESCHRIFTUNG_DATUM = ["Rechnungsdatum", "Datum", "Belegdatum"]


def eur(betrag: Decimal) -> str:
    """Formatiert einen Betrag in deutscher Schreibweise: 1.234,56"""
    gerundet = betrag.quantize(CENT, rounding=ROUND_HALF_UP)
    ganz, _, nach = f"{gerundet:.2f}".partition(".")
    negativ = ganz.startswith("-")
    ganz = ganz.lstrip("-")
    mit_punkten = f"{int(ganz):,}".replace(",", ".")
    return f"{'-' if negativ else ''}{mit_punkten},{nach}"


def erzeuge_rechnung(zufall: random.Random) -> Rechnung:
    """Würfelt eine in sich stimmige Rechnung.

    Die Reihenfolge ist wichtig: erst die Positionen, dann daraus Netto, USt
    und Brutto ausrechnen. So kann per Konstruktion nichts unstimmig sein - und
    genau deshalb ist das Ergebnis als Sollwert brauchbar.
    """
    # Alle Positionen einer Rechnung teilen sich denselben Steuersatz. Gemischte
    # Steuersaetze auf einem Beleg gibt es zwar, sie brauchen aber eine
    # Aufschluesselung pro Satz - das hebe ich fuer spaeter auf.
    satz = zufall.choice([Decimal("20"), Decimal("20"), Decimal("10"), Decimal("13")])
    auswahl = [l for l in LEISTUNGEN if l[2] == satz]
    anzahl = zufall.randint(1, 4)

    positionen: list[Position] = []
    for bezeichnung, preis, _ in zufall.sample(auswahl, k=min(anzahl, len(auswahl))):
        menge = Decimal(zufall.randint(1, 12))
        gesamt = (menge * preis).quantize(CENT, rounding=ROUND_HALF_UP)
        positionen.append(
            Position(
                bezeichnung=bezeichnung,
                menge=menge,
                einzelpreis=preis,
                gesamtpreis=gesamt,
            )
        )

    netto = sum((p.gesamtpreis for p in positionen), start=Decimal("0")).quantize(CENT)
    ust = (netto * satz / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    brutto = (netto + ust).quantize(CENT)

    # Kleinbetragsrechnungen bis 400 Euro brauchen keine UID des Lieferanten.
    uid = None if brutto < Decimal("400") and zufall.random() < 0.5 else (
        f"ATU{zufall.randint(10_000_000, 99_999_999)}"
    )

    return Rechnung(
        rechnungsnummer=f"{zufall.randint(2024, 2026)}-{zufall.randint(1, 9999):04d}",
        rechnungsdatum=date(2026, 1, 1) + timedelta(days=zufall.randint(0, 250)),
        lieferant_name=zufall.choice(LIEFERANTEN),
        lieferant_uid=uid,
        positionen=positionen,
        nettobetrag=netto,
        ust_satz=satz,
        ust_betrag=ust,
        bruttobetrag=brutto,
    )


def schreibe_pdf(rechnung: Rechnung, pfad: Path, zufall: random.Random) -> None:
    """Druckt die Rechnung als PDF.

    Bewusst schlicht gehalten: Es geht nicht um Gestaltung, sondern darum, dass
    die Angaben so auf dem Blatt stehen, wie sie auf echten Belegen stehen -
    mit wechselnden Beschriftungen und deutscher Zahlenschreibweise.
    """
    pfad.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pfad), pagesize=A4)
    breite, hoehe = A4
    y = hoehe - 30 * mm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, rechnung.lieferant_name)
    y -= 6 * mm

    c.setFont("Helvetica", 9)
    if rechnung.lieferant_uid:
        c.drawString(20 * mm, y, f"UID: {rechnung.lieferant_uid}")
        y -= 10 * mm
    else:
        y -= 4 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, "RECHNUNG")
    y -= 8 * mm

    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, y, f"{zufall.choice(BESCHRIFTUNG_NUMMER)}: {rechnung.rechnungsnummer}")
    y -= 5 * mm
    datum = rechnung.rechnungsdatum.strftime("%d.%m.%Y")
    c.drawString(20 * mm, y, f"{zufall.choice(BESCHRIFTUNG_DATUM)}: {datum}")
    y -= 12 * mm

    # Tabellenkopf
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Bezeichnung")
    c.drawRightString(120 * mm, y, "Menge")
    c.drawRightString(150 * mm, y, "Einzelpreis")
    c.drawRightString(180 * mm, y, "Gesamt")
    y -= 2 * mm
    c.line(20 * mm, y, 180 * mm, y)
    y -= 5 * mm

    c.setFont("Helvetica", 9)
    for p in rechnung.positionen:
        c.drawString(20 * mm, y, p.bezeichnung)
        c.drawRightString(120 * mm, y, str(int(p.menge)))
        c.drawRightString(150 * mm, y, eur(p.einzelpreis))
        c.drawRightString(180 * mm, y, eur(p.gesamtpreis))
        y -= 5 * mm

    y -= 3 * mm
    c.line(110 * mm, y, 180 * mm, y)
    y -= 6 * mm

    c.drawRightString(150 * mm, y, "Nettobetrag")
    c.drawRightString(180 * mm, y, eur(rechnung.nettobetrag))
    y -= 5 * mm
    c.drawRightString(150 * mm, y, f"USt {int(rechnung.ust_satz)} %")
    c.drawRightString(180 * mm, y, eur(rechnung.ust_betrag))
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(150 * mm, y, "Gesamtbetrag")
    c.drawRightString(180 * mm, y, f"{eur(rechnung.bruttobetrag)} EUR")

    c.showPage()
    c.save()


def erzeuge_testsatz(anzahl: int, ordner: Path, startwert: int = 42) -> list[tuple[Path, Rechnung]]:
    """Erzeugt `anzahl` Belege samt Sollwerten.

    `startwert` macht den Testsatz reproduzierbar: Derselbe Startwert liefert
    immer dieselben Belege. Ohne das liesse sich nicht sagen, ob eine bessere
    Messung an einer Verbesserung liegt oder an leichteren Zufallsbelegen.
    """
    zufall = random.Random(startwert)
    ergebnis = []
    for i in range(1, anzahl + 1):
        rechnung = erzeuge_rechnung(zufall)
        pfad = ordner / f"beleg_{i:03d}.pdf"
        schreibe_pdf(rechnung, pfad, zufall)
        (ordner / f"beleg_{i:03d}.json").write_text(
            rechnung.model_dump_json(indent=2), encoding="utf-8"
        )
        ergebnis.append((pfad, rechnung))
    return ergebnis
