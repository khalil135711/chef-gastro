# CHEF Gastro: Kasse, AI-Agent und Buchhaltung

Kassensystem für die Gastronomie mit Lager/MHD, Schichtplan, Mitarbeiter-Portal, **AI-Agent** (Claude optional)
und neu: **Buchhaltung** mit Netto-Gehalt, Rechnungen, Umsatzsteuer und Auswertung.
Nur Python-Standardbibliothek (3.9+), keine Installation nötig.

## Schnellstart (lokal)

```bash
git clone <dein-repo> && cd chef-gastro
CHEF_DEMO=1 python chef.py serve        # Windows PowerShell: $env:CHEF_DEMO=1; python chef.py serve
```

Dann öffnen: <http://localhost:8000/> (Kasse, Küche, Lager & MHD, Schichtplan, AI-Agent, Berichte) · `/buchhaltung` (Buchhaltung)

## Demo-Zugangsdaten (nur mit `CHEF_DEMO=1`, nur Testdaten)

| Nummer | PIN        | Rolle                                  |
|--------|------------|----------------------------------------|
| admin  | `demo1234` | Administrator, alles inkl. Buchhaltung |
| 1001   | `1234`     | Amina: Kasse, Bedienung                |
| 1002   | `1234`     | Youssef: Küche                         |
| 1003   | `1234`     | Lena: Bar, Bedienung                   |
| 1004   | `1234`     | Mehdi: Küche, Kasse                    |

Die Liste steht im Demo-Modus auch unter `GET /demo/info`. **Auf einer öffentlichen Demo niemals echte Namen, Löhne
oder Rechnungen eintragen.** Für echte Daten: `CHEF_DEMO` weglassen und `CHEF_ADMIN_PIN` auf einen eigenen Wert setzen.

## Buchhaltung

| Funktion | Was sie macht |
|---|---|
| **Netto-Gehalt** | Brutto → Netto je Mitarbeiter und Monat: Lohnsteuer (Tarif 2026, Klassen I–VI), Soli, Kirchensteuer, KV/PV/RV/AV, Minijob (603 €), Midijob-Übergangsbereich, Arbeitgeberkosten. Stunden kommen aus dem Schichtplan, sonst fester Monatslohn. Mitarbeiter sehen ihre eigenen Abrechnungen im Portal (`/portal/payslips`). |
| **Rechnungen** | Ausgangsrechnungen (Kunden, z. B. Catering) mit fortlaufender Nummer `RE-JJJJ-0001`, 0/7/19 % USt, druckbare Rechnung (Browser → PDF), Status offen/bezahlt/storniert, Überfällig-Markierung. Eingangsrechnungen (Lieferanten) als Belege mit Vorsteuer. |
| **Auswertung** | Tagesumsätze aus der Kasse, Umsatzsteuer − Vorsteuer = Zahllast, Einnahmen − Ausgaben − Personalkosten, offene Posten. |
| **Export** | `GET /accounting/export.csv?month=2026-11`: Buchungsliste im DATEV-Stil (SKR03) als Vorlage für den Steuerberater. |
| **AI-Agent** | Fragen wie „Netto von Mehdi?“, „Welche Rechnungen sind offen?“, „Umsatzsteuer letzter Monat“, „Personalkosten“. Mit `ANTHROPIC_API_KEY` bekommt Claude zusätzlich die Buchhaltungsdaten als Kontext. |

Alle Buchhaltungs-Routen (`/accounting/*`) sind **nur für Administratoren**.

### Wichtige Grenzen, bitte lesen

- **Die Lohnberechnung ist eine Schätzung.** Die Lohnsteuer wird vereinfacht nach dem Einkommensteuertarif 2026 berechnet,
  nicht nach dem amtlichen Programmablaufplan. Für echte Gehälter, Meldungen an die Krankenkassen und das Finanzamt
  braucht es ein Lohnbüro, den Steuerberater oder eine zertifizierte Lohnsoftware.
- Nicht abgebildet: kurzfristige Beschäftigung, Werkstudenten, Zuschläge (Nacht/Sonntag), Berufsgenossenschaft,
  Privatversicherte, Sonderzahlungen. Steuerklasse V/VI ist nur angenähert.
- Die **Umsatzsteuer-Zahllast** ist ein Orientierungswert (nach Rechnungsdatum), keine Voranmeldung.
- Die **TSE-Signatur der Kasse ist ein Mock** und nicht rechtsgültig. Für den echten Betrieb eine Cloud-TSE anbinden.
- Zahlungen sind simuliert. Die Parameter für 2026 stehen oben in `accounting.py` und müssen jedes Jahr aktualisiert werden.

Einstellungen für Rechnungen (`POST /settings`): `name`, `address`, `tax_id`, `ust_id`, `iban`, `bic`, `footer`.

## Auf GitHub hosten

```bash
git init && git add . && git commit -m "CHEF Gastro mit Buchhaltung"
gh repo create chef-gastro --public --source=. --push      # oder Repo im Browser anlegen und pushen
```

GitHub Pages kann keinen Python-Server ausführen. Drei Wege, wie das Repo trotzdem als Live-Demo läuft:

1. **GitHub Codespaces** (am einfachsten): Repo → *Code* → *Codespaces* → *Create*. Die Datei `.devcontainer/devcontainer.json`
   startet den Server im Demo-Modus und öffnet Port 8000.
2. **Docker-Hoster** (Render, Railway, Fly.io): Repo verbinden, das `Dockerfile` wird erkannt (Demo-Modus aktiv).
   Auf Gratis-Plänen geht die Datenbank bei Neustart verloren, der Demo-Modus legt die Testdaten dann automatisch neu an.
3. **Lokal im Netzwerk:** `CHEF_HOST=0.0.0.0 CHEF_ADMIN_PIN=<eigene-pin> python chef.py serve`

Die GitHub-Action `.github/workflows/ci.yml` führt bei jedem Push die Tests aus (`python -m unittest discover -s tests`).

## Dateien

`chef.py` Server, Kasse, Portal, Agent · `accounting.py` Buchhaltung · `buchhaltung.html` Oberfläche ·
`licensing_stub.py` Einzelbetrieb-Ersatz (wird übersprungen, wenn eine eigene `licensing.py` im Ordner liegt) ·
`index.html` Oberfläche für Kasse, Küche, Lager/MHD, Schichtplan, Agent · `tests/` Unit-Tests.
Nicht enthalten: `index2.html` (`/portal`), `kunde.html`, `willkommen.html`, `bestellung.html` (Kundenkarte, Online-Bestellung); fehlen sie, zeigt der Server eine Hinweisseite statt eines Fehlers.
Mitarbeiter sehen nach dem Login nur ihre Bereiche (z. B. Küche: Küche + Lager).
