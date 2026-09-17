# Belegleser

Liest Rechnungen als PDF und gibt geprüfte Daten zurück. Das Sprachmodell läuft
lokal, die Belege verlassen den Rechner nicht.

Das Besondere ist nicht das Auslesen — das kann jeder in einem Nachmittag
zusammenstecken. Das Besondere ist, dass **nachgerechnet wird**, und dass
gemessen ist, wie oft es stimmt.

---

## Das Problem

Ein Sprachmodell antwortet immer. Auch dann, wenn es die Zahl auf dem Beleg
nicht lesen konnte — dann erfindet es eine, die plausibel aussieht. Man kann es
also nicht fragen *„warst du dir sicher?"* und der Antwort glauben.

Nachrechnen kann man aber schon. Eine Rechnung hat innere Logik:

```
Summe der Positionen − Rabatt  =  Nettobetrag
Nettobetrag × USt-Satz          =  USt-Betrag
Nettobetrag + USt               =  Bruttobetrag
```

Stimmt eine davon nicht, wurde etwas falsch gelesen — egal wie überzeugt das
Modell klingt. Diese Prüfungen kommen ohne das Modell aus, und sie sind der Kern
des Projekts.

Am Ende steht entweder ein Datensatz, der alle Prüfungen bestanden hat, oder ein
Eintrag in der **Prüf-Warteschlange** mit der Angabe, woran es lag. Ein Drittes
gibt es nicht. Insbesondere kein *„wahrscheinlich richtig"* — das wäre genau die
stille Ungenauigkeit, die später in der Buchhaltung auffällt statt hier.

---

## Was gemessen wird

```
  Durch alle Pruefungen       12 / 12   (100%)
  Davon alle Felder richtig   12 / 12
  Stille Fehler                0   <- bestanden, aber falsch

  Median pro Beleg           16694 ms

  Feldgenauigkeit
  --------------------------------------------
  rechnungsnummer           12/12   100%  ##########
  lieferant_uid             12/12   100%  ##########
  nettobetrag               12/12   100%  ##########
  steuerzeilen              12/12   100%  ##########
```

Drei Zahlen, und die dritte ist die wichtigste:

- **Durchlaufquote** — wie viele Belege bestehen alle Prüfungen. Sagt, wie viel
  Handarbeit übrig bleibt.
- **Feldgenauigkeit** — wie oft jedes Feld stimmt. Sagt, *woran* es liegt.
- **Stille Fehler** — bestanden und trotzdem falsch.

Die dritte Zahl gibt es, weil die Nachrechnung eine Lücke hat: Sie prüft, was
zusammenhängt. Eine falsch gelesene Rechnungsnummer hängt mit nichts zusammen
und geht durch. Eine Durchlaufquote ohne diese Zahl daneben ist irreführend —
ein Extraktor, der alles durchwinkt, hätte 100 Prozent.

**Woher kommt die Wahrheit zum Vergleich?** Die Testbelege werden selbst
erzeugt: erst die Werte würfeln, daraus die Rechnung ausrechnen, dann als PDF
drucken. Die Sollwerte stehen fest, bevor der Beleg existiert. Kein Abtippen,
beliebig viele Belege, über einen festen Startwert reproduzierbar.

---

## Was dabei herauskam

Jede Verbesserung im Projekt kam aus einer Messung, nicht aus einer Ahnung.

| Messung | Durch | Was dahintersteckte |
|---|---|---|
| 1 | 80 % | Spalten verrutscht, UID übersehen |
| 2 | 60 % | **Schlechter.** Der Fix legte einen verdeckten Fehler frei |
| 3 | 100 % | Alle Beträge richtig — und trotzdem 3 von 5 Belegen falsch |
| 4 | 100 % | Alle Felder richtig, keine stillen Fehler |

Dann kamen schwerere Belege dazu — gemischte Steuersätze, Rabatt, Skonto — und
die Quote fiel wieder auf 50 %. Nach drei weiteren Korrekturen: 100 %.

Die Korrekturen folgen alle einem Satz:

> **Was deterministisch geht, überlasse ich dem Modell nicht.**

- Das Modell lieferte `"98,00"` und `"1107.40"` in derselben Antwort. Eine
  Anweisung ist eine Bitte, keine Garantie — also wandelt das Schema selbst um.
- Eine österreichische UID ist `ATU` plus acht Ziffern. Übersieht das Modell
  sie, wird sie per Muster gefunden. Stehen zwei im Beleg, wird **nicht** geraten.
- Ein Rabatt ist exakt *Positionssumme minus Netto*. Fehlt er in der Ausgabe,
  wird er errechnet — aber nur, wenn der Betrag in derselben Zeile wie ein
  Abzugswort steht.

Jede solche Ergänzung wird protokolliert. Sonst ließe sich die Leistung des
Modells nicht mehr von der der Nachbesserung trennen.

---

## Zwei Sachen, die im Beleg stecken und leicht übersehen werden

**Skonto ist kein Rabatt.** „3 % Skonto bei Zahlung binnen 14 Tagen" mindert den
Rechnungsbetrag **nicht** — es ist eine Bedingung für später. Wer es abzieht,
macht jede Rechnung zu klein und fällt dabei durch keine Formatprüfung. Skonto
steht deshalb im Datenmodell, obwohl nie damit gerechnet wird: damit erkennbar
bleibt, dass es gelesen und bewusst nicht verrechnet wurde.

**Eine Rechnung hat nicht einen Steuersatz.** Eine Hotelrechnung führt
Nächtigung mit 13 % und Getränke mit 20 %. Mein erstes Datenmodell nahm einen
Satz an — das war schlicht falsch. Die Steueraufschlüsselung ist jetzt eine
Liste; bei einem Satz enthält sie eben einen Eintrag.

---

## Warum lokal

Belege enthalten Geschäftsdaten — Lieferanten, Preise, Konditionen. Die gehen
nicht an einen Cloud-Dienst. Das Modell läuft über [Ollama](https://ollama.com)
auf der eigenen Maschine, hier `qwen2.5:7b` auf einer RTX 3060 mit 6 GB.

Der Anbieter steckt hinter einer schmalen Schnittstelle — rein ein Prompt, raus
ein Text. Ein Wechsel auf Azure AI Foundry kostet eine Klasse, keinen Umbau. Und
die Tests laufen gegen eine Attrappe mit festverdrahteten Antworten: ohne
Kosten, ohne Netz, immer dasselbe Ergebnis. Ein Test, der ein echtes Modell
aufruft, ist kein Test, sondern ein Experiment mit wechselndem Ausgang.

---

## Selber ausprobieren

Man braucht Python 3.11 oder neuer und [Ollama](https://ollama.com/download).

```bash
git clone https://github.com/UrosVeljic/Belegleser.git
cd Belegleser

python -m venv .venv
.venv/Scripts/activate          # unter Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

ollama pull qwen2.5:7b          # rund 4,7 GB

python messen.py --anzahl 12
```

Das erzeugt zwölf Testbelege, liest sie und gibt den Bericht aus. Ohne
Grafikkarte dauert es etwa sieben Mal so lange — falls es kriecht, lohnt ein
Blick auf `curl localhost:11434/api/ps`: Steht dort `size_vram: 0`, rechnet das
Modell auf dem Prozessor.

Die Tests brauchen weder Ollama noch Netz:

```bash
python -m pytest
```

---

## Aufbau

```
belegleser/
  schema.py       Datenmodell und die Nachrechnungen
  testdaten.py    Belege mit bekannten Sollwerten erzeugen
  modell.py       Anbieter austauschbar, Attrappe für Tests
  extraktion.py   PDF -> Text -> Modell -> Prüfung -> ok oder Warteschlange
  belegtreue.py   was rechnerisch feststeht, statt es zu glauben
  messung.py      Vergleich gegen die Sollwerte
messen.py         Testsatz erzeugen und messen
```

---

## Was es nicht kann

Ehrlichkeitshalber, weil das für die Einschätzung wichtiger ist als die 100 %:

- **Der Testsatz ist synthetisch** und stammt aus demselben Erzeuger. Echte
  Belege sind unregelmäßiger. Die Messung sagt, dass die Kette funktioniert —
  nicht, dass sie jeden Beleg liest.
- **Scans gehen nicht.** Verarbeitet wird nur, was eine Textebene hat. Ein
  fotografierter Beleg braucht vorher eine Texterkennung.
- **Fremdwährung, mehrseitige Belege und Gutschriften** sind nicht abgedeckt.
- **Rabatt bei mehreren Steuersätzen**: Der Abzug müsste auf die Sätze verteilt
  werden, und wie, steht auf echten Belegen selten dabei. Die Prüfung je Satz
  entfällt dann; die Summenprüfungen greifen weiter.
- Es gibt **keine Schnittstelle** — das ist ein Skript, kein Dienst.
