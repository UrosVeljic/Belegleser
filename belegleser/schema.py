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


class Position(BaseModel):
    """Eine einzelne Zeile auf der Rechnung."""

    bezeichnung: str = Field(min_length=1)
    menge: Decimal = Field(gt=0)
    einzelpreis: Decimal = Field(ge=0)
    gesamtpreis: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def zeile_rechnet_auf(self) -> Position:
        erwartet = (self.menge * self.einzelpreis).quantize(CENT)
        if abs(erwartet - self.gesamtpreis.quantize(CENT)) > CENT:
            raise ValueError(
                f"Position '{self.bezeichnung}': {self.menge} × {self.einzelpreis} "
                f"ergibt {erwartet}, auf dem Beleg steht {self.gesamtpreis}"
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

    nettobetrag: Decimal = Field(ge=0)
    ust_satz: Decimal = Field(ge=0, le=100)
    ust_betrag: Decimal = Field(ge=0)
    bruttobetrag: Decimal = Field(ge=0)
    waehrung: str = "EUR"

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

    @field_validator("ust_satz")
    @classmethod
    def ust_satz_ist_gueltig(cls, wert: Decimal) -> Decimal:
        if wert not in GUELTIGE_UST_SAETZE:
            gueltige = ", ".join(str(s) for s in sorted(GUELTIGE_UST_SAETZE))
            raise ValueError(f"{wert} % ist kein gültiger USt-Satz (erlaubt: {gueltige})")
        return wert

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
        if abs(summe_positionen - netto) > CENT:
            fehler.append(
                f"Positionen ergeben {summe_positionen}, Nettobetrag lautet {netto}"
            )

        erwartete_ust = (netto * self.ust_satz / Decimal("100")).quantize(CENT)
        ust = self.ust_betrag.quantize(CENT)
        if abs(erwartete_ust - ust) > CENT:
            fehler.append(
                f"{netto} bei {self.ust_satz} % USt ergibt {erwartete_ust}, "
                f"auf dem Beleg steht {ust}"
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
