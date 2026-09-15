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

    @property
    def braucht_pruefung(self) -> bool:
        return self.status == "pruefen"


def pdf_zu_text(pfad: Path) -> str:
    """Liest den Text aus einem PDF.

    Funktioniert nur bei PDFs mit einer Textebene - also bei allem, was aus
    einem Programm heraus erzeugt wurde. Eingescannte Belege sind Bilder und
    brauchen vorher eine Texterkennung; das ist ein eigener Schritt und hier
    bewusst noch nicht enthalten.
    """
    with pdfplumber.open(pfad) as pdf:
        seiten = [seite.extract_text() or "" for seite in pdf.pages]
    return "\n".join(seiten).strip()


ANWEISUNG = """Du liest österreichische Rechnungen und gibst die Felder als JSON zurück.

Regeln:
- Gib ausschließlich Werte zurück, die im Beleg stehen. Rechne nichts aus und
  ergänze nichts.
- Steht ein Feld nicht auf dem Beleg, lass es weg oder setze es auf null.
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

    try:
        rechnung = Rechnung.model_validate(roh)
    except ValidationError as fehler:
        return Ergebnis(
            quelle=quelle,
            status="pruefen",
            befunde=_lesbare_befunde(fehler),
            antwort=antwort,
        )

    return Ergebnis(quelle=quelle, status="ok", rechnung=rechnung, antwort=antwort)


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
