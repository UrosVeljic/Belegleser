"""Speichert Vorgänge - damit die Prüf-Warteschlange eine Warteschlange ist.

Bisher war das Ergebnis eine Rückgabe: Wer nicht hinschaute, für den existierte
es nicht. Eine Warteschlange, die niemand wiederfindet, ist keine.

Deshalb SQLite. Die Gründe gegen etwas Grösseres:

- Es steckt in der Standardbibliothek. Keine Abhängigkeit, kein Server, keine
  Zugangsdaten - und der Dienst startet ohne Vorbereitung.
- Die Datei lässt sich mitkopieren und ansehen. Für ein Projekt, das jemand
  nachvollziehen soll, ist das mehr wert als ein Container mehr.
- Für den Fall, um den es hier geht - Belege eintreffen lassen, prüfen,
  freigeben - reicht es. Erst bei mehreren gleichzeitig schreibenden Diensten
  wird PostgreSQL nötig, und dann tauscht man diese Datei aus.

Ein Vorgang durchläuft drei Zustände:

    ok            alle Prüfungen bestanden, nichts zu tun
    pruefen       etwas stimmt nicht, wartet auf einen Menschen
    freigegeben   ein Mensch hat hingesehen und entschieden

Der dritte ist der Grund für die Ablage. Ohne ihn liesse sich nicht sagen, ob
ein beanstandeter Beleg noch offen ist oder längst erledigt.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from belegleser.extraktion import Ergebnis

SCHEMA = """
CREATE TABLE IF NOT EXISTS vorgaenge (
    id               TEXT PRIMARY KEY,
    dateiname        TEXT NOT NULL,
    eingegangen      TEXT NOT NULL,
    status           TEXT NOT NULL,
    daten            TEXT,
    befunde          TEXT NOT NULL DEFAULT '[]',
    ergaenzungen     TEXT NOT NULL DEFAULT '[]',
    modell           TEXT,
    dauer_ms         INTEGER,
    freigegeben_von  TEXT,
    freigegeben_am   TEXT,
    anmerkung        TEXT
);

-- Die Warteschlange wird bei jedem Aufruf der Oberfläche gelesen; ohne Index
-- wird das mit wachsender Tabelle langsam.
CREATE INDEX IF NOT EXISTS idx_status ON vorgaenge (status, eingegangen);
"""


@dataclass
class Vorgang:
    id: str
    dateiname: str
    eingegangen: str
    status: str
    daten: dict | None
    befunde: list[str]
    ergaenzungen: list[str]
    modell: str | None = None
    dauer_ms: int | None = None
    freigegeben_von: str | None = None
    freigegeben_am: str | None = None
    anmerkung: str | None = None


def _jetzt() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ablage:
    def __init__(self, pfad: Path | str = "belegleser.db"):
        self.pfad = str(pfad)
        # check_same_thread=False, weil der Webserver mehrere Threads benutzt.
        # Zulaessig, solange nur diese Klasse auf die Verbindung zugreift und
        # SQLite die Schreibzugriffe selbst serialisiert.
        self._db = sqlite3.connect(self.pfad, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def schliesse(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ ablegen
    def lege_ab(self, ergebnis: Ergebnis, dateiname: str) -> Vorgang:
        vorgang = Vorgang(
            id=uuid.uuid4().hex[:12],
            dateiname=dateiname,
            eingegangen=_jetzt(),
            status=ergebnis.status,
            daten=json.loads(ergebnis.rechnung.model_dump_json()) if ergebnis.rechnung else None,
            befunde=list(ergebnis.befunde),
            ergaenzungen=list(ergebnis.ergaenzungen),
            modell=ergebnis.antwort.modell if ergebnis.antwort else None,
            dauer_ms=ergebnis.antwort.dauer_ms if ergebnis.antwort else None,
        )
        self._db.execute(
            """INSERT INTO vorgaenge
               (id, dateiname, eingegangen, status, daten, befunde, ergaenzungen,
                modell, dauer_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                vorgang.id,
                vorgang.dateiname,
                vorgang.eingegangen,
                vorgang.status,
                json.dumps(vorgang.daten) if vorgang.daten is not None else None,
                json.dumps(vorgang.befunde, ensure_ascii=False),
                json.dumps(vorgang.ergaenzungen, ensure_ascii=False),
                vorgang.modell,
                vorgang.dauer_ms,
            ),
        )
        self._db.commit()
        return vorgang

    # -------------------------------------------------------------------- lesen
    def hole(self, vorgang_id: str) -> Vorgang | None:
        zeile = self._db.execute(
            "SELECT * FROM vorgaenge WHERE id = ?", (vorgang_id,)
        ).fetchone()
        return _zu_vorgang(zeile) if zeile else None

    def liste(self, status: str | None = None, grenze: int = 100) -> list[Vorgang]:
        if status:
            zeilen = self._db.execute(
                "SELECT * FROM vorgaenge WHERE status = ? ORDER BY eingegangen DESC LIMIT ?",
                (status, grenze),
            ).fetchall()
        else:
            zeilen = self._db.execute(
                "SELECT * FROM vorgaenge ORDER BY eingegangen DESC LIMIT ?", (grenze,)
            ).fetchall()
        return [_zu_vorgang(z) for z in zeilen]

    # ---------------------------------------------------------------- freigeben
    def gib_frei(
        self,
        vorgang_id: str,
        person: str,
        daten: dict | None = None,
        anmerkung: str | None = None,
    ) -> Vorgang | None:
        """Schliesst einen beanstandeten Vorgang ab.

        `daten` erlaubt, die korrigierten Werte mitzugeben - jemand hat im Beleg
        nachgesehen und die richtige Zahl eingetragen. Wer freigibt, wird
        festgehalten: Bei einem Vorgang, der später auffällt, muss nachvollziehbar
        sein, wer ihn durchgewunken hat.
        """
        vorhanden = self.hole(vorgang_id)
        if vorhanden is None:
            return None

        self._db.execute(
            """UPDATE vorgaenge
               SET status = 'freigegeben',
                   daten = COALESCE(?, daten),
                   freigegeben_von = ?,
                   freigegeben_am = ?,
                   anmerkung = ?
               WHERE id = ?""",
            (
                json.dumps(daten) if daten is not None else None,
                person,
                _jetzt(),
                anmerkung,
                vorgang_id,
            ),
        )
        self._db.commit()
        return self.hole(vorgang_id)

    # --------------------------------------------------------------- kennzahlen
    def kennzahlen(self) -> dict[str, Any]:
        """Zahlen für die Überwachung des laufenden Betriebs.

        Nicht dieselben wie in messung.py: Dort wird gegen bekannte Sollwerte
        gemessen, das geht nur mit Testdaten. Hier laufen echte Belege durch,
        deren richtige Werte niemand kennt. Messbar bleibt, wie viel Handarbeit
        anfällt und wie lange es dauert.
        """
        nach_status = {
            z["status"]: z["anzahl"]
            for z in self._db.execute(
                "SELECT status, COUNT(*) AS anzahl FROM vorgaenge GROUP BY status"
            ).fetchall()
        }
        gesamt = sum(nach_status.values())
        offen = nach_status.get("pruefen", 0)

        zeiten = [
            z["dauer_ms"]
            for z in self._db.execute(
                "SELECT dauer_ms FROM vorgaenge WHERE dauer_ms IS NOT NULL"
            ).fetchall()
        ]
        zeiten.sort()

        return {
            "vorgaenge_gesamt": gesamt,
            "nach_status": nach_status,
            "offen_zur_pruefung": offen,
            # Der Anteil, der ohne Handarbeit durchgeht - die Zahl, an der sich
            # der Nutzen des Dienstes bemisst.
            "ohne_handarbeit": round((gesamt - offen) / gesamt, 3) if gesamt else None,
            "dauer_ms_median": zeiten[len(zeiten) // 2] if zeiten else None,
            # Das langsamste Zwanzigstel sagt mehr ueber den Alltag als der
            # Mittelwert: Ausreisser merkt der Benutzer, den Durchschnitt nicht.
            "dauer_ms_p95": zeiten[int(len(zeiten) * 0.95)] if len(zeiten) >= 20 else None,
        }


def _zu_vorgang(zeile: sqlite3.Row) -> Vorgang:
    return Vorgang(
        id=zeile["id"],
        dateiname=zeile["dateiname"],
        eingegangen=zeile["eingegangen"],
        status=zeile["status"],
        daten=json.loads(zeile["daten"]) if zeile["daten"] else None,
        befunde=json.loads(zeile["befunde"]),
        ergaenzungen=json.loads(zeile["ergaenzungen"]),
        modell=zeile["modell"],
        dauer_ms=zeile["dauer_ms"],
        freigegeben_von=zeile["freigegeben_von"],
        freigegeben_am=zeile["freigegeben_am"],
        anmerkung=zeile["anmerkung"],
    )
