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
from decimal import Decimal, InvalidOperation

from belegleser.schema import Rechnung, deutsche_zahl

# Bewusst OHNE Wortgrenzen. Deutsche Belege schreiben "Kundenrabatt",
# "Mengenrabatt" oder "Sonderrabatt" - mit einer Wortgrenze davor findet man
# davon keines, weil ein Wortzeichen vorangeht. Genau daran ist die Reparatur
# zuerst gescheitert: Sie griff nur bei Belegen, auf denen schlicht "Rabatt"
# stand - in der Messung drei von zwoelf.
#
# "Skonto" steht absichtlich nicht in der Liste. Es ist kein Abzug, sondern
# eine Zahlungsbedingung.
RABATT_WORT = re.compile(r"(rabatt|nachlass|abzug)", re.IGNORECASE)

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


def _als_betrag(wert: object) -> Decimal | None:
    if wert is None:
        return None
    try:
        return Decimal(str(deutsche_zahl(wert)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _deutsch(betrag: Decimal) -> str:
    """1234.56 -> '1.234,56' - zum Nachschlagen im Belegtext."""
    ganz, _, nach = f"{betrag:.2f}".partition(".")
    return f"{int(ganz):,}".replace(",", ".") + "," + nach


def _abzugszeile(text: str, betrag: Decimal) -> bool:
    """Gibt es eine Zeile, die ein Abzugswort UND genau diesen Betrag enthält?

    Die Einschränkung auf eine Zeile ist der Kern. Ein Betrag, der irgendwo im
    Beleg vorkommt, beweist nichts - er könnte ein Einzelpreis sein. Erst das
    Zusammentreffen mit "Rabatt" in derselben Zeile macht daraus einen Abzug.
    """
    gesucht = _deutsch(betrag)
    for zeile in text.splitlines():
        if RABATT_WORT.search(zeile) and gesucht in zeile:
            return True
    return False


# Streng: genau ATU plus acht Ziffern, nichts davor oder dahinter. Dient dazu,
# einen unbrauchbaren Wert zu erkennen - nicht dazu, ihn im Text zu finden.
UID_MUSTER_STRENG = re.compile(r"^ATU\d{8}$")


def _rechne_brutto_positionen_zurueck(roh: dict) -> tuple[dict, list[str]]:
    """Wandelt Positionsbeträge von brutto auf netto, wenn die Zahlen es zeigen.

    Bedingungen, alle drei müssen erfüllt sein:
      - die Positionen ergeben in Summe den Bruttobetrag der Rechnung
      - sie ergeben NICHT den Nettobetrag
      - jede Position bringt ihren Steuersatz mit

    Dann ist die Sache eindeutig: Es wurde die Bruttospalte gelesen, und
    netto = brutto / (1 + Satz/100) ist eine Division, keine Vermutung. Fehlt
    eine der Bedingungen, bleibt alles, wie es ist, und der Beleg geht in die
    Warteschlange - das ist die richtige Antwort auf "unklar".
    """
    positionen = roh.get("positionen")
    netto = _als_betrag(roh.get("nettobetrag"))
    brutto = _als_betrag(roh.get("bruttobetrag"))
    if not isinstance(positionen, list) or not positionen or netto is None or brutto is None:
        return roh, []
    if netto == brutto:
        return roh, []

    summe = Decimal("0")
    saetze: list[Decimal | None] = []
    for p in positionen:
        if not isinstance(p, dict):
            return roh, []
        wert = _als_betrag(p.get("gesamtpreis"))
        satz = _als_betrag(p.get("ust_satz"))
        if wert is None or satz is None:
            return roh, []
        summe += wert
        saetze.append(satz)

    summe = summe.quantize(Decimal("0.01"))
    if summe != brutto.quantize(Decimal("0.01")):
        return roh, []

    neue = []
    for p, satz in zip(positionen, saetze):
        faktor = Decimal("1") + satz / Decimal("100")
        brutto_zeile = _als_betrag(p["gesamtpreis"])
        netto_zeile = (brutto_zeile / faktor).quantize(Decimal("0.01"))
        eintrag = dict(p)
        eintrag["gesamtpreis"] = str(netto_zeile)
        # Der Einzelpreis muss mit, sonst geht die Zeilenpruefung nicht mehr auf.
        einzel = _als_betrag(p.get("einzelpreis"))
        if einzel is not None and einzel == brutto_zeile:
            eintrag["einzelpreis"] = str(netto_zeile)
        neue.append(eintrag)

    roh = dict(roh)
    roh["positionen"] = neue
    return roh, [
        f"Positionen waren Bruttobeträge (Summe {summe} = Rechnungsbrutto). "
        "Auf netto zurückgerechnet."
    ]


def vorbessere(roh: dict, text: str) -> tuple[dict, list[str]]:
    """Repariert den Rohdatensatz, bevor er geprüft wird.

    Arbeitet bewusst auf dem Wörterbuch, nicht auf dem Modell: Die Fehler, um
    die es hier geht, verhindern gerade, dass ein gültiges Objekt entsteht.

    Beide Eingriffe sind Rechnungen, keine Vermutungen - und der zweite wird
    zusätzlich gegen den Belegtext abgesichert.
    """
    if not isinstance(roh, dict):
        return roh, []

    ergaenzt: list[str] = []
    roh = dict(roh)

    # 0) Als UID etwas gelesen, das keine ist.
    #
    # Beobachtet: "AT474300045101844020" - das ist eine IBAN. Beide beginnen mit
    # AT, deshalb die Verwechslung. Die Formatpruefung faengt das zwar, wirft
    # aber den ganzen Beleg in die Warteschlange, obwohl die richtige UID im
    # selben Text steht. Also hier entfernen; die Musterersuche weiter unten
    # setzt dann die echte ein.
    uid = roh.get("lieferant_uid")
    if isinstance(uid, str) and uid.strip():
        if not UID_MUSTER_STRENG.match(uid.replace(" ", "").upper()):
            roh.pop("lieferant_uid")
            ergaenzt.append(
                f"'{uid}' als UID verworfen - passt nicht auf ATU + 8 Ziffern "
                "(häufig wird die IBAN verwechselt)"
            )

    # 1) Positionen mit Bruttobetraegen statt Nettobetraegen.
    #
    # Endkundenrechnungen fuehren beide Spalten. Nimmt das Modell die falsche,
    # ergeben die Positionen den Bruttobetrag der Rechnung statt des
    # Nettobetrags - und genau daran ist es erkennbar. Dann laesst sich pro
    # Zeile zurueckrechnen: netto = brutto / (1 + Satz/100).
    roh, umgerechnet = _rechne_brutto_positionen_zurueck(roh)
    ergaenzt.extend(umgerechnet)

    # 2) "Kein Rabatt" als Betrag null statt als fehlendes Feld.
    rabatt = roh.get("rabatt")
    if isinstance(rabatt, dict):
        betrag = _als_betrag(rabatt.get("betrag"))
        if betrag is not None and betrag <= 0:
            roh.pop("rabatt")
            ergaenzt.append("Rabatt mit Betrag 0 entfernt - das heißt: kein Rabatt")
            rabatt = None

    # 2) Abzug fehlt, obwohl die Zahlen ihn verlangen.
    if not roh.get("rabatt"):
        positionen = roh.get("positionen")
        netto = _als_betrag(roh.get("nettobetrag"))
        if isinstance(positionen, list) and positionen and netto is not None:
            summe = Decimal("0")
            vollstaendig = True
            for p in positionen:
                wert = _als_betrag(p.get("gesamtpreis")) if isinstance(p, dict) else None
                if wert is None:
                    vollstaendig = False
                    break
                summe += wert

            if vollstaendig:
                luecke = (summe - netto).quantize(Decimal("0.01"))
                # Nur wenn die Luecke positiv ist, im Beleg ein Abzugswort steht
                # und der errechnete Betrag dort auch tatsaechlich auftaucht.
                # Ohne diese drei Bedingungen waere es Ratenmit zusaetzlichem
                # Schritt - und wuerde einen echten Lesefehler zudecken.
                # Der Betrag muss in DERSELBEN Zeile wie das Abzugswort stehen.
                # Nur "kommt irgendwo im Beleg vor" genuegt nicht: In einem Test
                # stand der errechnete Abzug von 100,00 zufaellig auch als
                # Einzelpreis in der Positionszeile - die Pruefung haette den
                # Rabatt dann auch ohne Rabattzeile eingesetzt.
                if luecke > 0 and _abzugszeile(text, luecke):
                    roh["rabatt"] = {"bezeichnung": "Rabatt", "betrag": str(luecke)}
                    ergaenzt.append(
                        f"Rabatt aus der Differenz ergänzt: {summe} - {netto} = {luecke} "
                        "(Betrag steht so im Beleg)"
                    )

    return roh, ergaenzt


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
