"""Misst, wie gut die Extraktion wirklich ist.

Ohne diesen Teil bleibt jede Aussage über Qualität eine Behauptung. "Sieht gut
aus" ist keine Antwort auf die Frage, ob man das Ding einsetzen kann.

Gemessen werden drei Dinge, und das dritte ist das wichtigste:

1. **Durchlaufquote** - wie viele Belege bestehen alle Prüfungen. Sagt, wie
   viel Handarbeit übrig bleibt.

2. **Feldgenauigkeit** - wie oft jedes einzelne Feld stimmt. Sagt, *woran* es
   liegt. Ein Modell, das Beträge sicher liest, aber an der Rechnungsnummer
   scheitert, braucht eine andere Verbesserung als eines, das umgekehrt
   danebenliegt.

3. **Stille Fehler** - Belege, die alle Prüfungen bestanden haben und trotzdem
   falsche Werte enthalten.

Punkt 3 ist der eigentliche Grund für dieses Modul. Die Nachrechnung fängt
alles ab, was mit Beträgen zu tun hat - sie kann aber nicht merken, wenn das
Modell den Lieferantennamen oder die Rechnungsnummer falsch gelesen hat. Solche
Fehler gehen unbemerkt durch. Wie viele es sind, weiss man nur, wenn man gegen
bekannte Sollwerte prüft.

Eine Durchlaufquote ohne diese Zahl daneben ist irreführend: Ein Extraktor, der
alles durchwinkt, hätte 100 Prozent.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from belegleser.extraktion import Ergebnis, verarbeite_pdf
from belegleser.modell import Modell
from belegleser.schema import Rechnung

# Die Felder, die einzeln verglichen werden. Die Positionsliste wird gesondert
# behandelt, weil dort Reihenfolge und Anzahl mitspielen.
EINZELFELDER = [
    "rechnungsnummer",
    "rechnungsdatum",
    "lieferant_name",
    "lieferant_uid",
    "nettobetrag",
    "ust_satz",
    "ust_betrag",
    "bruttobetrag",
]


def _vergleichbar(wert: Any) -> Any:
    """Bringt Werte in eine Form, in der sich Gleichheit sinnvoll prüfen lässt.

    Decimal("300.00") und Decimal("300") sind derselbe Betrag, aber nicht
    dasselbe Objekt. Bei Text stören Gross- und Kleinschreibung sowie
    Leerzeichen an den Rändern - "Muster GmbH " und "Muster GmbH" sind für
    diese Messung dieselbe Firma.
    """
    if wert is None:
        return None
    if isinstance(wert, Decimal):
        return wert.quantize(Decimal("0.01"))
    if isinstance(wert, str):
        return " ".join(wert.split()).casefold()
    return wert


@dataclass
class Feldvergleich:
    feld: str
    soll: Any
    ist: Any

    @property
    def stimmt(self) -> bool:
        return _vergleichbar(self.soll) == _vergleichbar(self.ist)


@dataclass
class Belegmessung:
    quelle: Path
    status: str
    vergleiche: list[Feldvergleich] = field(default_factory=list)
    befunde: list[str] = field(default_factory=list)
    dauer_ms: int = 0
    tokens_ausgabe: int | None = None

    @property
    def alle_felder_richtig(self) -> bool:
        return bool(self.vergleiche) and all(v.stimmt for v in self.vergleiche)

    @property
    def stiller_fehler(self) -> bool:
        """Hat alle Prüfungen bestanden und ist trotzdem falsch."""
        return self.status == "ok" and not self.alle_felder_richtig


@dataclass
class Bericht:
    messungen: list[Belegmessung]

    @property
    def anzahl(self) -> int:
        return len(self.messungen)

    @property
    def durchgelaufen(self) -> int:
        return sum(1 for m in self.messungen if m.status == "ok")

    @property
    def durchlaufquote(self) -> float:
        return self.durchgelaufen / self.anzahl if self.anzahl else 0.0

    @property
    def stille_fehler(self) -> list[Belegmessung]:
        return [m for m in self.messungen if m.stiller_fehler]

    @property
    def vollstaendig_richtig(self) -> int:
        return sum(1 for m in self.messungen if m.alle_felder_richtig)

    def feldgenauigkeit(self) -> dict[str, tuple[int, int]]:
        """Pro Feld: wie oft richtig, wie oft überhaupt verglichen.

        Verglichen wird nur, wo das Modell etwas geliefert hat. Ein Beleg, der
        gar nicht erst durch die Schemaprüfung kam, taucht hier nicht auf -
        sonst vermischt sich "falsch gelesen" mit "gar nicht gelesen".
        """
        zaehler: dict[str, list[int]] = {f: [0, 0] for f in EINZELFELDER}
        for messung in self.messungen:
            for v in messung.vergleiche:
                if v.feld not in zaehler:
                    zaehler[v.feld] = [0, 0]
                zaehler[v.feld][1] += 1
                if v.stimmt:
                    zaehler[v.feld][0] += 1
        return {f: (richtig, gesamt) for f, (richtig, gesamt) in zaehler.items()}

    @property
    def median_dauer_ms(self) -> int:
        zeiten = [m.dauer_ms for m in self.messungen if m.dauer_ms]
        return int(statistics.median(zeiten)) if zeiten else 0

    def als_text(self) -> str:
        zeilen = [
            "",
            "=" * 64,
            f"  {self.anzahl} Belege",
            "=" * 64,
            "",
            f"  Durch alle Pruefungen      {self.durchgelaufen:>3} / {self.anzahl}"
            f"   ({self.durchlaufquote:.0%})",
            f"  Davon alle Felder richtig  {self.vollstaendig_richtig:>3} / {self.anzahl}",
            f"  Stille Fehler              {len(self.stille_fehler):>3}"
            "   <- bestanden, aber falsch",
            "",
            f"  Median pro Beleg           {self.median_dauer_ms:>5} ms",
            "",
            "  Feldgenauigkeit",
            "  " + "-" * 44,
        ]
        for feld, (richtig, gesamt) in self.feldgenauigkeit().items():
            if gesamt == 0:
                zeilen.append(f"  {feld:<24}      -   (nie geliefert)")
                continue
            quote = richtig / gesamt
            balken = "#" * round(quote * 10)
            zeilen.append(f"  {feld:<24} {richtig:>3}/{gesamt:<3} {quote:>5.0%}  {balken}")

        if self.stille_fehler:
            zeilen += ["", "  Stille Fehler im Einzelnen", "  " + "-" * 44]
            for m in self.stille_fehler:
                falsch = [v for v in m.vergleiche if not v.stimmt]
                zeilen.append(f"  {m.quelle.name}")
                for v in falsch:
                    zeilen.append(f"      {v.feld}: erwartet {v.soll!r}, gelesen {v.ist!r}")

        in_warteschlange = [m for m in self.messungen if m.status == "pruefen"]
        if in_warteschlange:
            zeilen += ["", "  In der Pruef-Warteschlange", "  " + "-" * 44]
            for m in in_warteschlange:
                zeilen.append(f"  {m.quelle.name}")
                for b in m.befunde[:3]:
                    zeilen.append(f"      {b}")

        zeilen.append("")
        return "\n".join(zeilen)


def vergleiche(ergebnis: Ergebnis, soll: Rechnung) -> Belegmessung:
    messung = Belegmessung(
        quelle=ergebnis.quelle,
        status=ergebnis.status,
        befunde=ergebnis.befunde,
        dauer_ms=ergebnis.antwort.dauer_ms if ergebnis.antwort else 0,
        tokens_ausgabe=ergebnis.antwort.tokens_ausgabe if ergebnis.antwort else None,
    )
    if ergebnis.rechnung is None:
        return messung

    for feld in EINZELFELDER:
        messung.vergleiche.append(
            Feldvergleich(
                feld=feld,
                soll=getattr(soll, feld),
                ist=getattr(ergebnis.rechnung, feld),
            )
        )
    # Die Positionen als Ganzes: Anzahl und Inhalt muessen stimmen.
    messung.vergleiche.append(
        Feldvergleich(
            feld="positionen",
            soll=[(p.bezeichnung, p.menge, p.gesamtpreis) for p in soll.positionen],
            ist=[
                (p.bezeichnung, p.menge, p.gesamtpreis) for p in ergebnis.rechnung.positionen
            ],
        )
    )
    return messung


def miss(testsatz: list[tuple[Path, Rechnung]], modell: Modell) -> Bericht:
    """Laesst das Modell auf den Testsatz los und vergleicht gegen die Sollwerte."""
    messungen = []
    for pfad, soll in testsatz:
        ergebnis = verarbeite_pdf(pfad, modell)
        messungen.append(vergleiche(ergebnis, soll))
    return Bericht(messungen=messungen)
