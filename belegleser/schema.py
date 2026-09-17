"""Datenmodell für eine Rechnung samt Plausibilitätsprüfungen.

Der wichtigste Gedanke dieses Moduls: Ein Sprachmodell antwortet immer. Es
antwortet auch dann, wenn es die Zahl auf dem Beleg nicht lesen konnte - dann
erfindet es eine, die plausibel aussieht. Man kann dem Modell also nicht die
Frage stellen "hast du richtig gelesen?" und der Antwort glauben.

Was man stattdessen tun kann: nachrechnen. Eine Rechnung hat innere Logik, die
unabhängig vom Modell überprüfbar ist:

    Summe der Positionen  = Nettobetrag
    Nettobetrag × USt-Satz = USt-Betrag
    Nettobetrag + USt      = Bruttobetrag

Stimmt das nicht, wurde etwas falsch gelesen - ganz gleich, wie überzeugt das
Modell klingt. Diese Prüfungen sind der Kern der Qualitätssicherung hier.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.functional_validators import BeforeValidator
from typing import Annotated

# Beträge werden auf den Cent genau verglichen. Ein Cent Abweichung lassen wir
# durchgehen: Auf echten Belegen wird pro Position gerundet, die Summe weicht
# dann gelegentlich um einen Cent von der Nachrechnung ab.
CENT = Decimal("0.01")

# Österreichische UID-Nummer: ATU gefolgt von acht Stellen.
UID_MUSTER = re.compile(r"^ATU\d{8}$")

# In Österreich gültige Umsatzsteuersätze. 13 % gilt etwa für Beherbergung und
# kulturelle Leistungen, 10 % für Lebensmittel und Bücher, 0 % für steuerfreie
# Leistungen wie innergemeinschaftliche Lieferungen.
GUELTIGE_UST_SAETZE = {Decimal("0"), Decimal("10"), Decimal("13"), Decimal("20")}


def deutsche_zahl(wert: object) -> object:
    """Nimmt Betraege in deutscher wie englischer Schreibweise entgegen.

    Das Sprachmodell wird angewiesen, Zahlen mit Punkt zu liefern. Es haelt
    sich nicht daran - in derselben Antwort standen "98,00" und "1107.40"
    nebeneinander. Eine Anweisung ist eine Bitte, keine Garantie.

    Statt die Bitte zu wiederholen, wird hier umgewandelt. Das ist
    deterministisch, kostet nichts und ist im Gegensatz zum Modellverhalten
    testbar.

    Die Regeln, bewusst einfach gehalten:

        "1.234,56"  Komma vorhanden -> Komma trennt die Nachkommastellen,
                                       Punkte sind Tausendertrennzeichen
        "1.234.567" kein Komma, mehrere Punkte -> alles Tausendertrennzeichen
        "1234.56"   kein Komma, ein Punkt -> Punkt trennt die Nachkommastellen

    Der letzte Fall ist theoretisch mehrdeutig: "1.234" koennte deutsch
    Tausend-zweihundertvierunddreissig meinen. Wir lesen es als 1,234 - so
    wuerde es auch JSON meinen. Sollte das im Einzelfall falsch sein, faellt es
    auf: Die Nachrechnung von Positionen, Netto und Brutto geht dann nicht mehr
    auf, und der Beleg landet in der Warteschlange statt still in der
    Buchhaltung.
    """
    if not isinstance(wert, str):
        return wert

    text = wert.strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        # Ein Modell, das ein Feld nicht fuellen kann, schreibt eine leere
        # Zeichenkette. Das heisst "nichts gefunden" - nicht "ungueltig".
        # Als None laesst sich damit weiterarbeiten, als "" nicht.
        return None
    text = text.removeprefix("EUR").removeprefix("\u20ac").strip()

    if "," in text:
        return text.replace(".", "").replace(",", ".")
    if text.count(".") > 1:
        return text.replace(".", "")
    return text


# Ein Decimal, das auch deutsche Schreibweise entgegennimmt.
Betrag = Annotated[Decimal, BeforeValidator(deutsche_zahl)]

# Fuer Felder, die fehlen duerfen. Wichtig ist, dass die Umwandlung VOR der
# Vereinigung steht und nicht in einem ihrer Zweige: Schriebe man
# `Betrag | None`, liefe die Umwandlung innerhalb von `Betrag`, gaebe bei
# leerem Text None zurueck - und die Decimal-Pruefung dieses Zweigs wuerde
# daran scheitern, bevor der None-Zweig ueberhaupt geprueft wird.
BetragOderNichts = Annotated[Decimal | None, BeforeValidator(deutsche_zahl)]


class Position(BaseModel):
    """Eine einzelne Zeile auf der Rechnung."""

    bezeichnung: str = Field(min_length=1)
    # Optional. Dienstleistungsrechnungen fuehren oft gar keine Mengenspalte -
    # "Hautkrebsvorsorge  190,00" ist eine vollstaendige Zeile.
    menge: BetragOderNichts = Field(default=None, gt=0)
    einzelpreis: Betrag = Field(ge=0)
    gesamtpreis: Betrag = Field(ge=0)
    # Der Satz, der für diese Zeile gilt. Auf Belegen mit nur einem Satz steht
    # er meist nicht an der Zeile - dann bleibt das Feld leer und der Satz
    # ergibt sich aus der Steueraufschlüsselung.
    ust_satz: Betrag | None = None

    @model_validator(mode="after")
    def zeile_rechnet_auf(self) -> Position:
        """Prüft die Zeile - und leitet die Menge her, wenn sie fehlt.

        Fehlt die Menge, ergibt sie sich aus Gesamtpreis geteilt durch
        Einzelpreis. Das ist keine Annahme, sondern eine Division. Geht sie
        nicht auf oder fehlen die Preise, bleibt das Feld leer und die Prüfung
        entfällt - die Summenprüfungen der Rechnung greifen weiterhin.
        """
        if self.menge is None:
            if self.einzelpreis > 0:
                hergeleitet = (self.gesamtpreis / self.einzelpreis).quantize(
                    Decimal("0.001")
                )
                if hergeleitet > 0:
                    # Ohne Aenderungsschutz koennte man hier nicht zuweisen:
                    # Pydantic-Modelle sind nach der Pruefung schreibbar, aber
                    # object.__setattr__ umgeht auch kuenftige Sperren.
                    object.__setattr__(self, "menge", hergeleitet)
            return self

        erwartet = (self.menge * self.einzelpreis).quantize(CENT)
        if abs(erwartet - self.gesamtpreis.quantize(CENT)) > CENT:
            raise ValueError(
                f"Position '{self.bezeichnung}': {self.menge} × {self.einzelpreis} "
                f"ergibt {erwartet}, auf dem Beleg steht {self.gesamtpreis}"
            )
        return self


class Rabatt(BaseModel):
    """Ein Abzug, der den Nettobetrag mindert.

    Auf dem Beleg steht er zwischen Positionssumme und Nettobetrag:

        Zwischensumme                 1.000,00
        Rabatt 10 %                    -100,00
        Nettobetrag                     900,00

    `betrag` ist immer positiv und meint die Höhe des Abzugs - nicht den
    verminderten Betrag. Ein Minuszeichen auf dem Beleg gehört zur Darstellung,
    nicht in die Daten.
    """

    bezeichnung: str = Field(min_length=1, default="Rabatt")
    prozent: Betrag | None = Field(default=None, ge=0, le=100)
    betrag: Betrag = Field(gt=0)


class Skonto(BaseModel):
    """Eine Zahlungsbedingung - kein Abzug.

    "3 % Skonto bei Zahlung binnen 14 Tagen" mindert den Rechnungsbetrag
    nicht. Ob der Abzug je zustande kommt, entscheidet sich erst bei der
    Zahlung.

    Hier steht es, damit sichtbar bleibt, dass es gelesen und bewusst nicht
    verrechnet wurde. Ein Extraktor, der Skonto stillschweigend abzieht, macht
    aus jeder Rechnung einen zu kleinen Betrag - und faellt dabei durch keine
    Formatpruefung.
    """

    prozent: Betrag = Field(gt=0, le=100)
    tage: int = Field(gt=0)


class Steuerzeile(BaseModel):
    """Eine Zeile der Steueraufschlüsselung.

    Österreichische Rechnungen mit mehreren Steuersätzen weisen die Steuer je
    Satz getrennt aus:

        Netto 20 %     800,00     USt     160,00
        Netto 13 %     196,00     USt      25,48

    Genau das bildet diese Klasse ab - und macht damit den Beleg nachrechenbar,
    statt ihn auf einen Satz zu vereinfachen, den er nicht hat.
    """

    satz: Betrag = Field(ge=0, le=100)
    nettobetrag: Betrag = Field(ge=0)
    ust_betrag: Betrag = Field(ge=0)

    @field_validator("satz")
    @classmethod
    def satz_ist_gueltig(cls, wert: Decimal) -> Decimal:
        if wert not in GUELTIGE_UST_SAETZE:
            gueltige = ", ".join(str(s) for s in sorted(GUELTIGE_UST_SAETZE))
            raise ValueError(f"{wert} % ist kein gültiger USt-Satz (erlaubt: {gueltige})")
        return wert

    @model_validator(mode="after")
    def steuer_rechnet_auf(self) -> Steuerzeile:
        erwartet = (self.nettobetrag * self.satz / Decimal("100")).quantize(CENT)
        if abs(erwartet - self.ust_betrag.quantize(CENT)) > CENT:
            raise ValueError(
                f"Steuerzeile {self.satz} %: {self.nettobetrag} ergibt {erwartet} USt, "
                f"auf dem Beleg steht {self.ust_betrag}"
            )
        return self


class Rechnung(BaseModel):
    """Die extrahierten Felder einer Rechnung.

    Dieses Modell wird an zwei Stellen gebraucht: Es beschreibt, was am Ende
    herauskommen soll, und es liefert über `model_json_schema()` zugleich die
    Vorgabe, die das Sprachmodell einzuhalten hat. Beides aus derselben Quelle -
    so können Vorgabe und Prüfung nicht auseinanderlaufen.
    """

    rechnungsnummer: str = Field(min_length=1)
    rechnungsdatum: date
    lieferant_name: str = Field(min_length=1)
    lieferant_uid: str | None = None

    positionen: list[Position] = Field(min_length=1)
    # Die Steueraufschlüsselung. Bei einem einzigen Satz enthält sie genau
    # einen Eintrag - der Sonderfall bleibt damit ein Fall der Regel.
    steuerzeilen: list[Steuerzeile] = Field(min_length=1)

    rabatt: Rabatt | None = None
    # Wird bewusst nicht in die Summen eingerechnet - siehe Klasse Skonto.
    skonto: Skonto | None = None

    nettobetrag: Betrag = Field(ge=0)
    ust_betrag: Betrag = Field(ge=0)
    bruttobetrag: Betrag = Field(ge=0)
    waehrung: str = "EUR"

    @property
    def ust_saetze(self) -> list[Decimal]:
        """Die vorkommenden Steuersätze, aufsteigend."""
        return sorted(z.satz for z in self.steuerzeilen)

    @field_validator("lieferant_uid")
    @classmethod
    def uid_hat_gueltiges_format(cls, wert: str | None) -> str | None:
        if wert is None:
            return None
        bereinigt = wert.replace(" ", "").upper()
        if not UID_MUSTER.match(bereinigt):
            raise ValueError(
                f"'{wert}' ist keine österreichische UID-Nummer (erwartet: ATU + 8 Ziffern)"
            )
        return bereinigt

    @model_validator(mode="after")
    def betraege_rechnen_auf(self) -> Rechnung:
        """Die drei Nachrechnungen, um die es in diesem Projekt geht.

        Alle Abweichungen werden gesammelt und gemeinsam gemeldet. Wer nur den
        ersten Fehler zurückgibt, zwingt den Aufrufer zu mehreren Durchläufen,
        bis er weiss, was alles nicht stimmt.
        """
        fehler: list[str] = []

        summe_positionen = sum(
            (p.gesamtpreis for p in self.positionen), start=Decimal("0")
        ).quantize(CENT)
        netto = self.nettobetrag.quantize(CENT)
        abzug = self.rabatt.betrag.quantize(CENT) if self.rabatt else Decimal("0")

        if abs((summe_positionen - abzug) - netto) > CENT:
            if self.rabatt:
                fehler.append(
                    f"Positionen ergeben {summe_positionen} minus Rabatt {abzug} "
                    f"= {summe_positionen - abzug}, Nettobetrag lautet {netto}"
                )
            else:
                fehler.append(
                    f"Positionen ergeben {summe_positionen}, Nettobetrag lautet {netto}"
                )

        # Ist der Rabatt in Prozent ausgewiesen, muss der Betrag dazu passen.
        if self.rabatt and self.rabatt.prozent is not None:
            erwartet = (summe_positionen * self.rabatt.prozent / Decimal("100")).quantize(CENT)
            if abs(erwartet - abzug) > CENT:
                fehler.append(
                    f"Rabatt {self.rabatt.prozent} % auf {summe_positionen} ergibt "
                    f"{erwartet}, ausgewiesen sind {abzug}"
                )

        # Die Aufschlüsselung muss in Summe den Gesamtbetrag ergeben - sonst
        # fehlt eine Steuerzeile oder eine wurde doppelt gelesen.
        summe_zeilen_netto = sum(
            (z.nettobetrag for z in self.steuerzeilen), start=Decimal("0")
        ).quantize(CENT)
        if abs(summe_zeilen_netto - netto) > CENT:
            fehler.append(
                f"Steueraufschlüsselung ergibt netto {summe_zeilen_netto}, "
                f"Nettobetrag lautet {netto}"
            )

        ust = self.ust_betrag.quantize(CENT)
        summe_zeilen_ust = sum(
            (z.ust_betrag for z in self.steuerzeilen), start=Decimal("0")
        ).quantize(CENT)
        if abs(summe_zeilen_ust - ust) > CENT:
            fehler.append(
                f"Steueraufschlüsselung ergibt {summe_zeilen_ust} USt, "
                f"ausgewiesen sind {ust}"
            )

        # Jeder Satz darf nur einmal vorkommen. Zwei Zeilen mit 20 % bedeuten,
        # dass eine Zeile doppelt gelesen wurde.
        saetze = [z.satz for z in self.steuerzeilen]
        if len(saetze) != len(set(saetze)):
            fehler.append("Ein Steuersatz kommt in der Aufschlüsselung mehrfach vor")

        # Wo die Positionen ihren Satz mitbringen, lässt sich die
        # Aufschlüsselung Zeile für Zeile gegenprüfen. Bei einem Rabatt geht
        # das nicht ohne Weiteres: Er verteilt sich auf die Sätze, und wie
        # genau, steht auf dem Beleg nicht immer. Diese Prüfung entfällt dann -
        # die Summenprüfungen darüber greifen weiterhin.
        if self.rabatt is None and all(p.ust_satz is not None for p in self.positionen):
            for zeile in self.steuerzeilen:
                summe = sum(
                    (p.gesamtpreis for p in self.positionen if p.ust_satz == zeile.satz),
                    start=Decimal("0"),
                ).quantize(CENT)
                if abs(summe - zeile.nettobetrag.quantize(CENT)) > CENT:
                    fehler.append(
                        f"Positionen mit {zeile.satz} % ergeben {summe}, "
                        f"die Aufschlüsselung nennt {zeile.nettobetrag}"
                    )

        brutto = self.bruttobetrag.quantize(CENT)
        if abs((netto + ust) - brutto) > CENT:
            fehler.append(
                f"Netto {netto} plus USt {ust} ergibt {netto + ust}, "
                f"Bruttobetrag lautet {brutto}"
            )

        if fehler:
            raise ValueError("; ".join(fehler))
        return self
