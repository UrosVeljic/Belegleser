"""Erzeugt einen Testsatz und misst, wie gut ein Modell ihn liest.

    python messen.py                      10 Belege, qwen2.5:7b
    python messen.py --anzahl 30          groesserer Testsatz
    python messen.py --modell llama3.1:8b anderes Modell
    python messen.py --startwert 7        anderer Testsatz

Der Startwert bestimmt, welche Belege erzeugt werden. Wer zwei Modelle
vergleichen will, muss denselben Startwert verwenden - sonst vergleicht er
nebenbei auch unterschiedlich schwere Belege.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from belegleser.messung import miss
from belegleser.modell import Ollama
from belegleser.testdaten import erzeuge_testsatz


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--anzahl", type=int, default=10, help="Anzahl der Testbelege")
    p.add_argument("--modell", default="qwen2.5:7b", help="Name des Ollama-Modells")
    p.add_argument("--startwert", type=int, default=42, help="Zufallsstartwert des Testsatzes")
    p.add_argument("--ordner", type=Path, default=Path("testsatz"))
    p.add_argument(
        "--ohne-schema",
        action="store_true",
        help="Schema nicht erzwingen - schneller, aber das Modell kann ungueltiges JSON liefern",
    )
    args = p.parse_args()

    modell = Ollama(modell=args.modell, schema_erzwingen=not args.ohne_schema)
    if not modell.erreichbar():
        print("Ollama antwortet nicht. Laeuft der Dienst?", file=sys.stderr)
        return 1

    vorhanden = modell.verfuegbare_modelle()
    if args.modell not in vorhanden:
        print(f"Modell '{args.modell}' ist nicht geladen.", file=sys.stderr)
        print(f"Vorhanden: {', '.join(vorhanden) or 'keine'}", file=sys.stderr)
        print(f"Holen mit: ollama pull {args.modell}", file=sys.stderr)
        return 1

    print(f"Erzeuge {args.anzahl} Belege (Startwert {args.startwert}) ...")
    testsatz = erzeuge_testsatz(args.anzahl, args.ordner, startwert=args.startwert)

    print(f"Lese sie mit {args.modell} ...", flush=True)

    def zeige(nummer, gesamt, messung):
        marke = "ok     " if messung.status == "ok" else "pruefen"
        if messung.status == "ok" and not messung.alle_felder_richtig:
            marke = "still! "
        print(
            f"  [{nummer:>2}/{gesamt}] {messung.quelle.name}  {marke}  "
            f"{messung.dauer_ms/1000:.1f}s",
            flush=True,
        )

    bericht = miss(testsatz, modell, fortschritt=zeige)

    # Ausgabe ueber sys.stdout mit erzwungenem UTF-8: Die Windows-Konsole
    # steht sonst auf einer Codepage, die an Umlauten scheitert.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(bericht.als_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
