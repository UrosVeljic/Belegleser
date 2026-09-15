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


def _steht_woertlich_im_beleg(wert: str, text: str) -> bool:
    """Kommt die Zeichenkette genau so im Beleg vor?

    Bewusst ohne Vereinheitlichung des Leerraums. Genau der trägt hier die
    Information: Der Belegtext ist spaltenweise ausgerichtet, zwischen zwei
    Spalten stehen mehrere Leerzeichen. "Übersetzung DE-EN, Seite 11" mit
    einem Leerzeichen kann deshalb nicht aus einer Zeile stammen, in der
    Bezeichnung und Menge zwei verschiedene Spalten sind.
    """
    return wert in text


def bereinige_bezeichnungen(rechnung: Rechnung, text: str) -> tuple[Rechnung, list[str]]:
    """Entfernt die Menge, wenn sie an der Bezeichnung klebt.

    Beobachtet in der Messung: Aus

        Übersetzung DE-EN, Seite        11      55,00     605,00

    wurde die Bezeichnung "Übersetzung DE-EN, Seite 11". Menge und Beträge
    stimmten - nur der Text hatte die Zahl aus der Nachbarspalte angehängt.

    Warum hier korrigiert werden darf und es kein Raten ist: Geprüft wird
    gegen den Belegtext. Steht die Bezeichnung mit angehängter Zahl dort nicht
    wörtlich, ohne die Zahl aber schon, dann stammt die Zahl aus einer anderen
    Spalte. Eine Bezeichnung, die tatsächlich auf eine Zahl endet - etwa
    "Fachbuch Band 3" - steht so im Beleg und bleibt unangetastet.
    """
    ergaenzt: list[str] = []
    neue_positionen = []
    geaendert = False

    for position in rechnung.positionen:
        bezeichnung = position.bezeichnung
        menge_als_text = str(int(position.menge)) if position.menge == int(position.menge) else None

        if (
            menge_als_text
            and bezeichnung.endswith(" " + menge_als_text)
            and not _steht_woertlich_im_beleg(bezeichnung, text)
        ):
            ohne_menge = bezeichnung[: -(len(menge_als_text) + 1)].rstrip()
            if ohne_menge and _steht_woertlich_im_beleg(ohne_menge, text):
                position = position.model_copy(update={"bezeichnung": ohne_menge})
                ergaenzt.append(
                    f"Bezeichnung bereinigt: '{bezeichnung}' -> '{ohne_menge}' "
                    "(Menge aus der Nachbarspalte war angehängt)"
                )
                geaendert = True

        neue_positionen.append(position)

    if geaendert:
        rechnung = rechnung.model_copy(update={"positionen": neue_positionen})
    return rechnung, ergaenzt


def bessere_nach(rechnung: Rechnung, text: str) -> tuple[Rechnung, list[str]]:
    """Setzt ein, was sich aus dem Beleg sicher ableiten lässt.

    Zurück kommt die überarbeitete Rechnung und eine Liste dessen, was ergänzt
    wurde. Die Liste ist wichtig: Wer sie nicht protokolliert, weiss später
    nicht mehr, was vom Modell kam und was von der Nachbesserung - und misst
    damit die Leistung des Modells falsch.
    """
    ergaenzt: list[str] = []

    rechnung, bereinigt = bereinige_bezeichnungen(rechnung, text)
    ergaenzt.extend(bereinigt)

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
