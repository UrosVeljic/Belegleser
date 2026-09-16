"""Vom PDF zum geprüften Datensatz - oder in die Prüf-Warteschlange.

Der Ablauf hat vier Stufen, und jede kann den Beleg aussortieren:

    PDF  ->  Text  ->  Modell  ->  JSON gültig?  ->  Beträge stimmig?  ->  ok
                                        |                  |
                                        +------------------+--> pruefen

Entscheidend ist, was am Ende herauskommt: entweder ein Datensatz, der alle
Prüfungen bestanden hat, oder ein Eintrag in der Warteschlange mit der Angabe,
woran es lag. Ein drittes gibt es nicht. Insbesondere gibt es kein "wahrscheinlich
richtig" - das wäre genau die stille Ungenauigkeit, die später in der Buchhaltung
auffällt statt hier.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pdfplumber
from pydantic import ValidationError

from belegleser.belegtreue import bessere_nach, pruefe_belegtreue, vorbessere
from belegleser.modell import Antwort, Modell
from belegleser.schema import Rechnung

Status = Literal["ok", "pruefen"]


@dataclass
class Ergebnis:
    """Was bei der Verarbeitung eines Belegs herausgekommen ist."""

    quelle: Path
    status: Status
    rechnung: Rechnung | None = None
    befunde: list[str] = field(default_factory=list)
    antwort: Antwort | None = None
    # Was nicht vom Modell kam, sondern deterministisch aus dem Beleg ergänzt
    # wurde. Ohne diese Liste liesse sich die Leistung des Modells nicht mehr
    # von der Leistung der Nachbesserung trennen.
    ergaenzungen: list[str] = field(default_factory=list)

    @property
    def braucht_pruefung(self) -> bool:
        return self.status == "pruefen"


def pdf_zu_text(pfad: Path) -> str:
    """Liest den Text aus einem PDF und erhält dabei die Spalten.

    `layout=True` ist hier der entscheidende Teil. Ohne diesen Schalter legt
    pdfplumber alle Spalten einer Tabellenzeile zu einer Zeichenkette zusammen:

        Übersetzung DE-EN, Seite 2 55,00 110,00

    Wo die Bezeichnung aufhört und die Menge beginnt, ist darin nicht mehr
    erkennbar - in der ersten Messung hat das Modell genau hier alles um eine
    Spalte verschoben und Menge 55 statt 2 gelesen. Mit layout=True bleiben die
    Spalten durch Leerzeichen getrennt:

        Übersetzung DE-EN, Seite     2      55,00    110,00

    Das ist der Unterschied zwischen "das Modell liest schlecht" und "wir haben
    ihm die Struktur weggenommen, bevor es lesen konnte".

    Funktioniert nur bei PDFs mit einer Textebene - also bei allem, was aus
    einem Programm heraus erzeugt wurde. Eingescannte Belege sind Bilder und
    brauchen vorher eine Texterkennung; das ist ein eigener Schritt und hier
    bewusst noch nicht enthalten.
    """
    with pdfplumber.open(pfad) as pdf:
        seiten = [seite.extract_text(layout=True) or "" for seite in pdf.pages]
    return _entschlacke("\n".join(seiten))


def _entschlacke(text: str) -> str:
    """Entfernt Leerraum, der keine Information trägt.

    layout=True füllt die ganze Seitenbreite mit Leerzeichen auf. Die Einrückung
    am Zeilenanfang trägt die Spaltenstruktur und bleibt deshalb stehen; alles
    nach dem letzten Zeichen und die leeren Zeilen dazwischen kosten nur Tokens.
    """
    zeilen = [z.rstrip() for z in text.split("\n")]
    behalten = [z for z in zeilen if z.strip()]
    return "\n".join(behalten).strip()


ANWEISUNG = """Du liest österreichische Rechnungen und gibst die Felder als JSON zurück.

Die Felder und wo sie üblicherweise stehen:

- rechnungsnummer: beschriftet als "Rechnungsnummer", "Rechnungs-Nr.", "RE-Nr."
  oder "Beleg-Nr."
- rechnungsdatum: beschriftet als "Rechnungsdatum", "Datum" oder "Belegdatum"
- lieferant_name: der ausstellende Betrieb, meist ganz oben
- lieferant_uid: die UID-Nummer des Lieferanten, beginnt mit ATU und steht
  meist direkt unter dem Firmennamen. Nur auf null setzen, wenn im Beleg
  wirklich keine steht.
- positionen: die Zeilen der Leistungstabelle. Steht in der Tabelle eine
  USt-Spalte, gehört ihr Wert als ust_satz zur jeweiligen Zeile.
- steuerzeilen: die Steueraufschlüsselung. Auf Belegen mit mehreren Sätzen
  steht sie als eigener Block ("netto 530,70  10 % USt  53,07"). Hat der Beleg
  nur einen Satz, gib trotzdem genau eine Steuerzeile zurück - mit dem
  Nettobetrag der Rechnung, dem Satz und dem Steuerbetrag.
- rabatt: ein Abzug zwischen Zwischensumme und Nettobetrag, beschriftet als
  "Rabatt", "Mengenrabatt" oder "Kundenrabatt". betrag ist immer positiv und
  meint die Höhe des Abzugs - das Minuszeichen auf dem Beleg gehört zur
  Darstellung, nicht in die Daten. Gibt es keinen Abzug, lass das Feld weg.
- skonto: die Zahlungsbedingung, etwa "3 % Skonto bei Zahlung binnen 14 Tagen".
  WICHTIG: Skonto wird NICHT abgezogen. Es ist eine Bedingung für später, keine
  Minderung der Rechnung. Übernimm die Prozentzahl und die Tage unverändert und
  lass alle Beträge so, wie sie auf dem Beleg stehen.
- nettobetrag, ust_betrag, bruttobetrag: die Gesamtsummen darunter.
  Der Bruttobetrag ist als "Gesamtbetrag" oder "Rechnungsbetrag" beschriftet.

Regeln:
- Gib ausschließlich Werte zurück, die im Beleg stehen. Rechne nichts aus und
  ergänze nichts.
- Die Tabelle ist in Spalten ausgerichtet: Bezeichnung, Menge, Einzelpreis,
  Gesamt. Die Bezeichnung kann Ziffern und Beistriche enthalten - trenn die
  Spalten am Leerraum, nicht am letzten Wort.
- Beträge in deutscher Schreibweise (1.234,56) bedeuten: Punkt trennt Tausender,
  Komma trennt die Nachkommastellen. Gib sie als Zahl mit Punkt zurück: 1234.56
- Das Datum im Format JJJJ-MM-TT.
- Der USt-Satz ist die Prozentzahl ohne Zeichen, also 20 statt "20 %".

Beleg:
---
{text}
---
"""


def baue_prompt(text: str) -> str:
    return ANWEISUNG.format(text=text)


def verarbeite_text(text: str, modell: Modell, quelle: Path | None = None) -> Ergebnis:
    """Schickt einen Belegtext durchs Modell und prüft das Ergebnis."""
    quelle = quelle or Path("<text>")

    if not text.strip():
        return Ergebnis(
            quelle=quelle,
            status="pruefen",
            befunde=["Kein Text im Beleg gefunden - vermutlich ein Scan ohne Texterkennung"],
        )

    # Das Schema geht als Vorgabe mit. Ollama zwingt das Modell damit beim
    # Erzeugen in die Form - ungültiges JSON kann so gar nicht erst entstehen.
    schema = Rechnung.model_json_schema()
    antwort = modell.frage(baue_prompt(text), json_schema=schema)

    try:
        roh = json.loads(antwort.text)
    except json.JSONDecodeError as fehler:
        return Ergebnis(
            quelle=quelle,
            status="pruefen",
            befunde=[f"Antwort ist kein gültiges JSON: {fehler}"],
            antwort=antwort,
        )

    # Vor der Pruefung das reparieren, was rechnerisch feststeht. Manche Fehler
    # verhindern sonst, dass ueberhaupt ein gueltiges Objekt entsteht - dann
    # kaeme die Nachbesserung weiter unten gar nicht mehr zum Zug.
    roh, vorab = vorbessere(roh, text)

    try:
        rechnung = Rechnung.model_validate(roh)
    except ValidationError as fehler:
        return Ergebnis(
            quelle=quelle,
            status="pruefen",
            befunde=_lesbare_befunde(fehler),
            antwort=antwort,
            ergaenzungen=vorab,
        )

    # Erst nachbessern, was sich sicher aus dem Beleg ableiten laesst ...
    rechnung, ergaenzungen = bessere_nach(rechnung, text)
    ergaenzungen = vorab + ergaenzungen

    # ... dann pruefen, ob die uebrigen Werte ueberhaupt im Beleg vorkommen.
    # Die Nachrechnung im Schema kann das nicht: Sie prueft nur Werte, die
    # miteinander zusammenhaengen. Eine erfundene Rechnungsnummer haengt mit
    # nichts zusammen und geht dort still durch.
    befunde = pruefe_belegtreue(rechnung, text)
    if befunde:
        return Ergebnis(
            quelle=quelle,
            status="pruefen",
            rechnung=rechnung,
            befunde=befunde,
            antwort=antwort,
            ergaenzungen=ergaenzungen,
        )

    return Ergebnis(
        quelle=quelle,
        status="ok",
        rechnung=rechnung,
        antwort=antwort,
        ergaenzungen=ergaenzungen,
    )


def verarbeite_pdf(pfad: Path, modell: Modell) -> Ergebnis:
    return verarbeite_text(pdf_zu_text(pfad), modell, quelle=pfad)


def _lesbare_befunde(fehler: ValidationError) -> list[str]:
    """Macht aus Pydantics Fehlerobjekten Sätze, die ein Mensch lesen kann.

    Die Warteschlange wird von jemandem abgearbeitet, der den Beleg nachsehen
    soll - nicht von einer Entwicklerin. "nettobetrag: Positionen ergeben
    349.90, Nettobetrag lautet 449.90" sagt ihr, wo sie hinschauen muss.
    """
    befunde = []
    for eintrag in fehler.errors():
        ort = ".".join(str(t) for t in eintrag["loc"]) or "Beleg"
        meldung = eintrag["msg"]
        # Pydantic stellt eigenen Validierungsmeldungen "Value error, " voran.
        meldung = meldung.removeprefix("Value error, ")
        befunde.append(f"{ort}: {meldung}")
    return befunde
