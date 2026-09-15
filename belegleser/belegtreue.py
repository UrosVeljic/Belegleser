"""Prüft, ob die gelesenen Werte wirklich im Beleg stehen.

Die Nachrechnung im Schema fängt alles ab, was miteinander zusammenhängt -
Positionen, Netto, Umsatzsteuer, Brutto. Sie kann aber nicht merken, wenn ein
Wert falsch gelesen wurde, der mit nichts anderem verknüpft ist: eine
Rechnungsnummer, eine UID, ein Firmenname. Genau diese Fehler gingen in der
dritten Messung still durch.

Für solche Felder gibt es eine zweite, ebenso modellunabhängige Prüfung: **Der
Wert muss im Beleg vorkommen.** Ein Sprachmodell, das eine Rechnungsnummer
erfindet, erfindet eine, die nicht im Text steht. Das ist nachprüfbar, ohne
irgendetwas über den Beleg zu wissen.

Dazu ein zweiter Gedanke: Manche Felder haben ein so strenges Format, dass man
sie selbst finden kann. Eine österreichische UID ist "ATU" plus acht Ziffern -
dafür braucht es kein Sprachmodell. Wenn das Modell sie übersieht, der Beleg
aber genau eine enthält, wird sie eingesetzt. Das ist kein Raten, sondern eine
Ableitung aus dem Beleg selbst.

Die Reihenfolge ist wichtig: erst nachbessern, was sicher ableitbar ist, dann
prüfen, was übrig bleibt.
"""

from __future__ import annotations

import re

from belegleser.schema import Rechnung

UID_IM_TEXT = re.compile(r"\bATU\s?\d{8}\b", re.IGNORECASE)

# Felder, deren Wert unverändert im Beleg stehen muss. Beträge gehören nicht
# dazu: Sie stehen dort in deutscher Schreibweise und wurden absichtlich
# umgeformt - ein Vergleich würde immer fehlschlagen.
WOERTLICHE_FELDER = ["rechnungsnummer", "lieferant_uid"]


def finde_uids(text: str) -> list[str]:
    """Alle UID-Nummern im Belegtext, normalisiert und ohne Dubletten."""
    gefunden = [t.replace(" ", "").upper() for t in UID_IM_TEXT.findall(text)]
    # Reihenfolge erhalten, Dubletten entfernen
    return list(dict.fromkeys(gefunden))


def bessere_nach(rechnung: Rechnung, text: str) -> tuple[Rechnung, list[str]]:
    """Setzt ein, was sich aus dem Beleg sicher ableiten lässt.

    Zurück kommt die überarbeitete Rechnung und eine Liste dessen, was ergänzt
    wurde. Die Liste ist wichtig: Wer sie nicht protokolliert, weiss später
    nicht mehr, was vom Modell kam und was von der Nachbesserung - und misst
    damit die Leistung des Modells falsch.
    """
    ergaenzt: list[str] = []

    if rechnung.lieferant_uid is None:
        uids = finde_uids(text)
        # Nur bei genau einer Fundstelle. Stehen mehrere im Beleg - etwa die
        # des Lieferanten und die des Empfaengers - ist nicht entscheidbar,
        # welche gemeint ist. Dann bleibt es beim Befund.
        if len(uids) == 1:
            rechnung = rechnung.model_copy(update={"lieferant_uid": uids[0]})
            ergaenzt.append(f"lieferant_uid aus dem Beleg ergänzt: {uids[0]}")

    return rechnung, ergaenzt


def pruefe_belegtreue(rechnung: Rechnung, text: str) -> list[str]:
    """Meldet Werte, die im Beleg nicht vorkommen.

    Das ist die Prüfung gegen erfundene Werte. Sie ersetzt keine inhaltliche
    Kontrolle - ein Modell kann auch eine falsche Zahl abschreiben, die
    tatsächlich irgendwo auf dem Blatt steht. Aber sie fängt den häufigsten
    Fall: einen Wert, den es nirgends gibt.
    """
    befunde: list[str] = []
    # Leerraum vereinheitlichen, damit Umbrueche im PDF nicht stoeren.
    vergleichstext = " ".join(text.split()).casefold()

    for feld in WOERTLICHE_FELDER:
        wert = getattr(rechnung, feld)
        if wert is None:
            continue
        if str(wert).casefold() not in vergleichstext:
            befunde.append(
                f"{feld}: '{wert}' kommt im Beleg nicht vor - vermutlich erfunden"
            )

    if rechnung.lieferant_uid is None and finde_uids(text):
        befunde.append(
            "lieferant_uid: Im Beleg steht eine UID-Nummer, sie wurde aber nicht gelesen"
        )

    return befunde
