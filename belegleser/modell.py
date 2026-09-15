"""Zugriff auf Sprachmodelle - austauschbar hinter einer schmalen Schnittstelle.

Warum eine eigene Schnittstelle, statt direkt die Bibliothek des Anbieters zu
verwenden:

1. **Tests ohne Kosten und ohne Netz.** Die Attrappe liefert festverdrahtete
   Antworten. Damit laufen Tests in Millisekunden, kosten nichts und liefern
   immer dasselbe Ergebnis. Ein Test, der ein echtes Modell aufruft, ist kein
   Test - er ist ein Experiment mit wechselndem Ausgang.

2. **Anbieterwechsel ohne Umbau.** Lokal über Ollama, später Azure AI Foundry
   für dieselbe Aufgabe. Was sich ändert, ist eine Klasse - nicht die Pipeline.

3. **Vergleichbarkeit.** Weil alle Anbieter dieselbe Schnittstelle erfüllen,
   lässt sich derselbe Testsatz gegen mehrere Modelle laufen lassen und die
   Genauigkeit direkt vergleichen.

Die Schnittstelle ist bewusst winzig: rein ein Prompt, raus ein Text. Alles
Weitere - Schema vorgeben, Antwort prüfen, nachrechnen - passiert eine Ebene
darüber und ist damit für jeden Anbieter gleich.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Antwort:
    """Was ein Modelllauf geliefert hat - samt allem, was zum Messen nötig ist.

    Dauer und Tokenzahl gehören hierher, nicht in eine Protokolldatei am Rand.
    Wer Kosten und Antwortzeiten später auswerten will, braucht sie an der
    Stelle, an der sie entstehen.
    """

    text: str
    modell: str
    dauer_ms: int
    tokens_eingabe: int | None = None
    tokens_ausgabe: int | None = None


class Modell(Protocol):
    """Alles, was dieses Projekt von einem Sprachmodell braucht."""

    name: str

    def frage(self, prompt: str, *, json_schema: dict | None = None) -> Antwort:
        ...


class Attrappe:
    """Liefert vorbereitete Antworten, ohne irgendetwas aufzurufen.

    Für Tests. Die Antworten werden der Reihe nach ausgegeben; ist die Liste
    erschöpft, wiederholt sich die letzte. So lässt sich auch prüfen, was
    passiert, wenn das Modell Unsinn liefert - kaputtes JSON, fehlende Felder,
    falsch gerechnete Beträge.
    """

    name = "attrappe"

    def __init__(self, antworten: list[str]):
        if not antworten:
            raise ValueError("Die Attrappe braucht mindestens eine Antwort")
        self._antworten = list(antworten)
        self.aufrufe: list[str] = []

    def frage(self, prompt: str, *, json_schema: dict | None = None) -> Antwort:
        self.aufrufe.append(prompt)
        index = min(len(self.aufrufe) - 1, len(self._antworten) - 1)
        return Antwort(text=self._antworten[index], modell=self.name, dauer_ms=0)


class OllamaFehler(RuntimeError):
    pass


class Ollama:
    """Spricht mit einem lokal laufenden Ollama-Server.

    Bewusst ohne zusätzliche Bibliothek: Ollama bietet eine schlichte
    HTTP-Schnittstelle, und urllib aus der Standardbibliothek genügt dafür.
    Eine Abhängigkeit weniger, die veralten oder brechen kann.

    Wichtig ist der Parameter `format`: Bekommt Ollama ein JSON-Schema, zwingt
    es das Modell beim Erzeugen jedes Tokens dazu, im Schema zu bleiben. Das
    Modell *kann* dann gar kein ungültiges JSON produzieren. Das ersetzt die
    inhaltliche Prüfung nicht - erfundene Zahlen sind weiterhin möglich - aber
    es beseitigt eine ganze Klasse von Fehlern vorab.
    """

    def __init__(
        self,
        modell: str = "qwen2.5:7b",
        host: str = "http://127.0.0.1:11434",
        zeitlimit: int = 180,
        temperatur: float = 0.0,
    ):
        self.name = modell
        self._host = host.rstrip("/")
        self._zeitlimit = zeitlimit
        # Temperatur 0: Bei einer Extraktionsaufgabe gibt es eine richtige
        # Antwort. Kreativitaet ist hier kein Vorteil, sondern eine Fehlerquelle -
        # und sie macht Messungen unreproduzierbar.
        self._temperatur = temperatur

    def erreichbar(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._host}/api/tags", timeout=3):
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def verfuegbare_modelle(self) -> list[str]:
        with urllib.request.urlopen(f"{self._host}/api/tags", timeout=5) as antwort:
            daten = json.loads(antwort.read())
        return [m["name"] for m in daten.get("models", [])]

    def frage(self, prompt: str, *, json_schema: dict | None = None) -> Antwort:
        rumpf: dict = {
            "model": self.name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self._temperatur},
        }
        if json_schema is not None:
            rumpf["format"] = json_schema

        anfrage = urllib.request.Request(
            f"{self._host}/api/generate",
            data=json.dumps(rumpf).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(anfrage, timeout=self._zeitlimit) as antwort:
                daten = json.loads(antwort.read())
        except urllib.error.URLError as fehler:
            raise OllamaFehler(
                f"Ollama unter {self._host} nicht erreichbar: {fehler}. "
                "Läuft der Dienst? (ollama serve)"
            ) from fehler
        dauer_ms = int((time.perf_counter() - start) * 1000)

        return Antwort(
            text=daten.get("response", ""),
            modell=self.name,
            dauer_ms=dauer_ms,
            tokens_eingabe=daten.get("prompt_eval_count"),
            tokens_ausgabe=daten.get("eval_count"),
        )
