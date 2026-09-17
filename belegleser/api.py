"""HTTP-Schnittstelle - damit aus dem Skript ein Dienst wird.

Ein Skript verarbeitet, was jemand von Hand hineingibt. Ein Dienst wartet auf
Belege und hält fest, was daraus wurde. Der Unterschied ist nicht die
Technologie, sondern dass es einen Ort gibt, an dem der Stand nachvollziehbar
ist.

Die Endpunkte bilden den tatsächlichen Ablauf ab, nicht die Datenbanktabellen:

    POST /belege                   Beleg abgeben
    GET  /belege/{id}              nachsehen, was daraus wurde
    GET  /warteschlange            was auf einen Menschen wartet
    POST /belege/{id}/freigeben    nach Sichtprüfung abschliessen
    GET  /kennzahlen               wie viel läuft, wie viel bleibt liegen
    GET  /gesundheit               ist der Dienst arbeitsfähig

`/gesundheit` und `/kennzahlen` sind keine Zierde. Ein Dienst, der ein
Sprachmodell braucht, kann auf zwei Arten ausfallen: Er antwortet nicht mehr -
das merkt jeder - oder das Modell ist weg und jeder Beleg landet in der
Warteschlange. Der zweite Fall sieht von aussen aus wie Betrieb.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from belegleser.ablage import Ablage, Vorgang
from belegleser.extraktion import verarbeite_pdf
from belegleser.modell import Modell, Ollama

MAX_BYTES = 10 * 1024 * 1024


class Freigabe(BaseModel):
    """Was beim Freigeben mitgeschickt wird.

    Steht bewusst auf Modulebene, nicht in baue_app: Pydantic löst Typangaben
    über den Namensraum des Moduls auf. Mit "from __future__ import annotations"
    sind sie Zeichenketten, und "dict | None" lässt sich dann aus einer Funktion
    heraus nicht mehr auflösen - FastAPI antwortete deshalb mit 422 statt den
    Körper zu prüfen.
    """

    person: str = Field(min_length=1, description="Wer hat nachgesehen")
    daten: dict | None = Field(
        default=None,
        description="Korrigierte Werte, falls beim Nachsehen etwas berichtigt wurde",
    )
    anmerkung: str | None = None


def baue_app(modell: Modell | None = None, ablage: Ablage | None = None) -> FastAPI:
    """Baut den Dienst.

    Modell und Ablage werden hereingereicht statt im Modul angelegt. Das ist der
    Grund, warum die Tests hier ohne Ollama und ohne Datei auf der Platte
    auskommen: Sie geben die Attrappe und eine Datenbank im Arbeitsspeicher mit.
    """
    modell = modell or Ollama()
    ablage = ablage or Ablage()

    app = FastAPI(
        title="Belegleser",
        version="0.1.0",
        description=(
            "Liest Rechnungen aus PDF und rechnet nach, statt dem Modell zu glauben. "
            "Was nicht aufgeht, landet in der Prüf-Warteschlange."
        ),
    )

    # -------------------------------------------------------------- Betrieb
    @app.get("/gesundheit", tags=["Betrieb"])
    def gesundheit() -> dict:
        """Ist der Dienst arbeitsfähig?

        Geprüft wird nicht nur, ob das Programm läuft, sondern ob das Modell
        erreichbar und geladen ist. Ohne diese Prüfung meldet der Dienst
        "gesund", während jeder Beleg in der Warteschlange landet.
        """
        erreichbar = getattr(modell, "erreichbar", lambda: True)()
        modelle = []
        if erreichbar and hasattr(modell, "verfuegbare_modelle"):
            try:
                modelle = modell.verfuegbare_modelle()
            except Exception:
                erreichbar = False

        geladen = modell.name in modelle if modelle else erreichbar
        return {
            "bereit": bool(erreichbar and geladen),
            "modell": modell.name,
            "modell_erreichbar": erreichbar,
            "modell_geladen": geladen,
        }

    @app.get("/kennzahlen", tags=["Betrieb"])
    def kennzahlen() -> dict:
        return ablage.kennzahlen()

    # --------------------------------------------------------------- Belege
    @app.post("/belege", tags=["Belege"], status_code=201)
    async def beleg_abgeben(datei: UploadFile = File(...)) -> dict:
        """Nimmt ein PDF entgegen, liest es und legt den Vorgang ab."""
        if not (datei.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Nur PDF-Dateien. Scans brauchen vorher eine Texterkennung.")

        inhalt = await datei.read()
        if len(inhalt) > MAX_BYTES:
            raise HTTPException(413, f"Datei grösser als {MAX_BYTES // 1024 // 1024} MB")
        if not inhalt:
            raise HTTPException(400, "Die Datei ist leer")

        # Das PDF wird nur zum Lesen kurz auf die Platte gelegt und danach
        # geloescht. Es enthaelt Geschaeftsdaten und hat auf dem Server nichts
        # verloren - gespeichert werden die ausgelesenen Felder, nicht der Beleg.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(inhalt)
            pfad = Path(tmp.name)
        try:
            ergebnis = verarbeite_pdf(pfad, modell)
        finally:
            pfad.unlink(missing_ok=True)

        vorgang = ablage.lege_ab(ergebnis, dateiname=datei.filename or "unbenannt.pdf")
        return _als_antwort(vorgang)

    @app.get("/belege/{vorgang_id}", tags=["Belege"])
    def beleg_ansehen(vorgang_id: str) -> dict:
        vorgang = ablage.hole(vorgang_id)
        if vorgang is None:
            raise HTTPException(404, "Kein Vorgang mit dieser Kennung")
        return _als_antwort(vorgang)

    @app.get("/warteschlange", tags=["Belege"])
    def warteschlange(grenze: int = 50) -> dict:
        offen = ablage.liste(status="pruefen", grenze=grenze)
        return {"anzahl": len(offen), "vorgaenge": [_als_antwort(v) for v in offen]}

    @app.post("/belege/{vorgang_id}/freigeben", tags=["Belege"])
    def freigeben(vorgang_id: str, freigabe: Freigabe) -> dict:
        """Schliesst einen beanstandeten Vorgang ab.

        Wer freigibt, wird festgehalten. Bei einem Beleg, der spaeter auffaellt,
        muss nachvollziehbar sein, wer ihn durchgewunken hat - und ob dabei
        Werte geaendert wurden.
        """
        vorgang = ablage.gib_frei(
            vorgang_id, person=freigabe.person, daten=freigabe.daten, anmerkung=freigabe.anmerkung
        )
        if vorgang is None:
            raise HTTPException(404, "Kein Vorgang mit dieser Kennung")
        return _als_antwort(vorgang)

    return app


def _als_antwort(vorgang: Vorgang) -> dict:
    return {
        "id": vorgang.id,
        "dateiname": vorgang.dateiname,
        "eingegangen": vorgang.eingegangen,
        "status": vorgang.status,
        "rechnung": vorgang.daten,
        "befunde": vorgang.befunde,
        "ergaenzungen": vorgang.ergaenzungen,
        "modell": vorgang.modell,
        "dauer_ms": vorgang.dauer_ms,
        "freigegeben_von": vorgang.freigegeben_von,
        "freigegeben_am": vorgang.freigegeben_am,
        "anmerkung": vorgang.anmerkung,
    }


# Für "uvicorn belegleser.api:app"
app = baue_app()
