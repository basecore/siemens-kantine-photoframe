# siemens-kantine-photoframe

Wöchentlich automatischer Screenshot des [Siemens Kantine Regensburg Speiseplans](https://siemens.cateringportal.io/menu/Regensburg/Mittagessen) als RSS Feed für den **Philips 8FF3WMI** Digital PhotoFrame.

Nachfolger von [kicktipp-photoframe](https://github.com/basecore/kicktipp-photoframe), das während der FIFA WM 2026 die Kicktipp-Tabelle auf demselben Bilderrahmen anzeigte.

## Aktueller Screenshot

<!-- SCREENSHOT_LINK_START -->
![Aktueller Speiseplan](docs/images/latest.jpg)
<!-- SCREENSHOT_LINK_END -->

> Das Bild wird jeden Montag automatisch aktualisiert. Kalenderwoche, Datum und Uhrzeit sind direkt im JPEG eingebettet (deutsche Zeit, 24h-Format). Der Statusbalken am unteren Rand ist in **Siemens-Blau** gehalten.

---

## Hardware

### Philips 8FF3WMI Digital PhotoFrame

![Philips 8FF3WMI Digital PhotoFrame](https://github.com/basecore/siemens-kantine-photoframe/blob/0501a4b28e14bcf6f840445b591d8350d6f2768f/sources/IMG/frame.jpg)

Der **Philips 8FF3WMI** ist ein digitaler Bilderrahmen, der RSS Feeds mit JPEG-Bildern über HTTP abrufen kann. Er unterstützt kein HTTPS – daher läuft das Hosting über bplaced.net. Das Ausgabeformat ist **800×600 px Querformat** (Landscape), passend zur horizontalen Wochentabelle des Cateringportals.

### Router: TP-Link TL-MR3020 mit 4G Stick & Netzclub SIM

![TP-Link TL-MR3020 mit 4G Stick und Netzclub SIM](https://github.com/basecore/siemens-kantine-photoframe/blob/0501a4b28e14bcf6f840445b591d8350d6f2768f/sources/IMG/router.jpg)

Für die Internetverbindung des PhotoFrames kommt ein **TP-Link TL-MR3020** Reiserouter mit einem 4G USB-Stick und einer **Netzclub SIM-Karte (200 MB gratis)** zum Einsatz. Diese Kombination ermöglicht eine kostengünstige, autarke Netzwerkverbindung für den Bilderrahmen.

---

## Wie es funktioniert

1. **GitHub Actions** startet jeden **Montag um 05:30 UTC (≈ 07:30 CEST)** – der neue Wochenspeiseplan ist auf dem Cateringportal i.d.R. montagmorgens verfügbar
2. Playwright öffnet die Cateringportal-Seite im Headless-Chrome (kein Login erforderlich – die Seite ist öffentlich)
3. GDPR-Consent-Banner wird automatisch weggeklickt, falls vorhanden
4. Header, Navigation und Footer werden per JavaScript ausgeblendet
5. Das Bild wird auf **800×600 px Querformat** skaliert (proportional, auf weißem Canvas zentriert – optimiert für Philips 8FF3WMI)
6. **Kalenderwoche, Datum und Uhrzeit werden als Siemens-blauer Balken am unteren Bildrand eingebettet** (deutsche Zeit / Europe/Berlin, 24h-Format, z. B. `KW 26 – 23.06.2026 07:42 Uhr – Siemens Kantine Regensburg`)
7. Ein RSS 2.0 Feed mit `media:content`-Tags wird generiert (Philips-kompatibles Format)
8. Feed & Bilder werden auf **GitHub Pages** veröffentlicht (HTTPS, für Browser)
9. Das **README wird automatisch** mit dem Link zum aktuellen Wochenbild (`kantine_YYYY-Www.jpg`) aktualisiert
10. **Das Wochenbild und `feed.php` werden per FTP auf bplaced.net hochgeladen** – primär über den **self-hosted HAOS-Runner** (Heim-IP, kein bplaced-Blockade-Problem), bei Ausfall automatisch Fallback auf GitHub-hosted Runner
11. Nach jedem Run wird eine **Status-E-Mail** an `basecore@gmx.de` verschickt (Ergebnis aller Tasks + Screenshot-Anhang)
12. Die letzten 8 Wochenbilder (≈ 2 Monate) werden vorgehalten, ältere automatisch gelöscht (GitHub + bplaced)

---

## Workflow-Übersicht (4 Jobs)

Der GitHub Actions Workflow besteht aus 4 Jobs:

| Job | Runner | Aufgabe | Wann |
|---|---|---|---|
| `screenshot` | GitHub (ubuntu-latest) | Screenshot + RSS generieren + README updaten + git push | immer |
| `upload-haos` | **self-hosted (HAOS)** | FTP Upload via ncftpput (Heim-IP) + alte Dateien löschen | nach `screenshot` |
| `upload-fallback` | GitHub (ubuntu-latest) | FTP Upload via lftp (Fallback) + alte Dateien löschen | nur wenn `upload-haos` fehlschlägt |
| `notify` | GitHub (ubuntu-latest) | Status-E-Mail mit Screenshot versenden | immer (auch bei Fehler) |

### Warum ein self-hosted Runner auf Home Assistant?

bplaced.net blockiert FTP-Verbindungen von GitHub Actions IP-Adressen (Rechenzentrumsblöcke) sporadisch oder dauerhaft. Der **self-hosted Runner läuft als Docker-Container auf Home Assistant OS (HAOS)** im Heimnetz – mit der privaten Heim-IP-Adresse sind FTP-Verbindungen zu bplaced zuverlässig möglich.

---

## E-Mail im Überblick

Es wird pro Run **1 Status-E-Mail** verschickt (Job 4 `notify`) – immer, auch bei Fehlern:

| Feld | Inhalt |
|---|---|
| **Betreff** | `✅ OK:` oder `❌ FAILED:` + Zeitstempel |
| **Inhalt** | Status-Tabelle aller Jobs (Screenshot, GitHub-Push, FTP HAOS, FTP Fallback) |
| **Bild** | Aktueller Speiseplan eingebettet (bplaced-URL) |
| **Anhang** | `latest.jpg` als Datei-Anhang |
| **Link** | Direktlink zum GitHub Actions Log |

---

## Setup (einmalig)

### 1. GitHub Secrets hinterlegen

Gehe zu **Settings → Secrets and variables → Actions** und erstelle folgende Secrets:

| Secret | Beschreibung |
|---|---|
| `BPLACED_FTP_PASSWORD` | bplaced FTP-Passwort (identisch mit kicktipp-photoframe) |
| `MAIL_USERNAME` | Gmail-Adresse für Status-E-Mails |
| `MAIL_PASSWORD` | Gmail **App-Passwort** ([App-Passwort erstellen](https://myaccount.google.com/apppasswords)) |
| `CATERINGPORTAL_SID` | *(optional)* Session-ID `ste_sid` – nur nötig wenn die Seite sie erzwingt |

> ⚠️ `MAIL_PASSWORD` muss ein **App-Passwort** sein, kein normales Gmail-Passwort.

### 2. GitHub Pages aktivieren

- Gehe zu **Settings → Pages**
- Source: `Deploy from a branch` → Branch: `main`, Ordner: `/docs`
- Speichern – nach wenigen Minuten ist der Feed live unter:
  `https://basecore.github.io/siemens-kantine-photoframe/feed.xml`

### 3. bplaced einrichten (bereits vorhanden)

Der bplaced-Account und der FTP-Zugang sind bereits aus dem kicktipp-photoframe-Projekt vorhanden:
- FTP-Host: `basecore.bplaced.net`, User: `basecore`, Port: `21`
- Im bplaced Dateimanager sicherstellen, dass der Ordner `www/images/` existiert

### 4. Self-hosted GitHub Runner auf Home Assistant OS

Der Runner ist bereits aus dem kicktipp-photoframe-Projekt registriert und läuft weiter. Kein neuer Runner nötig – beide Repos teilen denselben HAOS-Runner.

Falls eine Neuregistrierung nötig ist:

#### 4a. Runner-Token holen

Gehe im Repo zu **Settings → Actions → Runners → New self-hosted runner**:
- OS: **Linux**, Architecture: **x64**
- Den angezeigten **Token** kopieren

#### 4b. Runner-Container starten

Im **Home Assistant Terminal Add-on** (oder SSH Add-on):

```bash
docker run -d \
  --name github-runner-kantine \
  --restart unless-stopped \
  -e REPO_URL="https://github.com/basecore/siemens-kantine-photoframe" \
  -e RUNNER_TOKEN="DEIN_TOKEN_HIER" \
  -e RUNNER_NAME="haos-runner" \
  -e LABELS="self-hosted,haos" \
  -e RUNNER_WORKDIR="/tmp/runner" \
  -v /tmp/runner:/tmp/runner \
  myoung34/github-runner:latest
```

#### 4c. Runner-Status prüfen

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
docker logs github-runner-kantine
# → sollte "Listening for Jobs" anzeigen
```

### 5. RSS Feed URL im Philips PhotoFrame eintragen

Der PhotoFrame unterstützt kein HTTPS – daher wird der Feed über bplaced per HTTP ausgeliefert:

```
http://basecore.bplaced.net/feed.php
```

- Am PhotoFrame: **Menü → Online → RSS Feed**
- Kategorie & Name frei wählen, URL eintragen

> Für Browser (HTTPS): `https://basecore.github.io/siemens-kantine-photoframe/feed.xml`

### 6. Workflow manuell starten (erster Screenshot)

- Gehe zu **Actions → Weekly Kantine Screenshot → Run workflow**

---

## Dateistruktur

```
siemens-kantine-photoframe/
├── .github/
│   └── workflows/
│       └── screenshot.yml          # GitHub Actions Workflow (4 Jobs)
├── docs/                           # GitHub Pages Root
│   ├── images/
│   │   ├── latest.jpg              # Kopie des neuesten Wochenscreenshots
│   │   └── kantine_YYYY-Www.jpg   # Wochenbilder (max. 8 Wochen)
│   ├── feed.xml                    # RSS Feed (HTTPS, für Browser)
│   └── feed.php                    # RSS Feed (HTTP via bplaced, für PhotoFrame)
└── scripts/
    ├── take_screenshot.py          # Playwright Screenshot + KW/Datum-Overlay
    └── generate_rss.py             # RSS Feed Generator
```

---

## Technische Details

| Eigenschaft | Wert |
|---|---|
| Bildgröße | 800 × 600 px (Querformat / Landscape) |
| Bildformat | JPEG, Qualität 92 |
| Skalierung | Proportional auf max. 800×578 px, zentriert auf weißem Canvas |
| Datum-Overlay | Siemens-blauer Balken (`#003366`), 22 px, unten zentriert, deutsche Zeit (CET/CEST) |
| Zeitformat Overlay | `KW WW – DD.MM.YYYY HH:MM Uhr – Siemens Kantine Regensburg` |
| Screenshot-Trigger | Jeden Montag 05:30 UTC = **07:30 CEST** |
| Login erforderlich | Nein – öffentliche Seite |
| Session-ID | Optional via Secret `CATERINGPORTAL_SID` konfigurierbar |
| Viewport | 1100 × 900 px (breit genug für die Wochentabelle) |
| FTP-Upload primär | HAOS self-hosted Runner via `ncftpput` (Heim-IP) |
| FTP-Upload Fallback | GitHub-hosted Runner via `lftp` (6 Versuche, 60 s Pause) |
| Uploads auf bplaced | `feed.php` → `/www/`, `kantine_YYYY-Www.jpg` → `/www/images/` |
| README-Update | Bildlink wird wöchentlich automatisch auf `kantine_YYYY-Www.jpg` aktualisiert |
| Status-E-Mail | nach jedem Run an `basecore@gmx.de` inkl. Screenshot-Anhang |
| Aufbewahrung | letzte 8 Wochen (GitHub + bplaced, automatisch gelöscht) |
| Zielgerät | Philips 8FF3WMI Digital PhotoFrame |
| Feed-Format | RSS 2.0 mit `media:content`-Tags |
| HTTP-Hosting | [bplaced.net](http://basecore.bplaced.net) |
| HTTPS-Hosting | [GitHub Pages](https://basecore.github.io/siemens-kantine-photoframe) |
| Runner-Image | [myoung34/github-runner:latest](https://hub.docker.com/r/myoung34/github-runner) |
| Router | TP-Link TL-MR3020 mit 4G Stick |
| SIM-Karte | Netzclub (200 MB gratis) |

---

## Warum bplaced für den PhotoFrame?

Der **Philips 8FF3WMI** unterstützt nur **HTTP** (kein HTTPS). GitHub Pages erzwingt HTTPS.
bplaced.net hostet die `feed.php` und das Wochenbild über plain HTTP – der PhotoFrame kann
den Feed daher direkt abrufen. Der self-hosted HAOS-Runner lädt jeden Montag per FTP die neue
Version hoch.

---

## Wichtig: Warum .php und nicht .xml?

bplaced führt alle Dateien mit der Endung `.php` als PHP-Code aus. Das klingt zunächst wie
ein Problem, ist aber die einzig funktionierende Lösung:

**`.xml` geht nicht**, weil bplaced `.xml`-Dateien zwar ausliefert, der Philips PhotoFrame
aber explizit eine URL mit `.php`-Endung erwartet.

**`.php` ohne korrekten Header geht nicht**, weil PHP die erste Zeile `<?xml ...?>` als
PHP-Öffnungs-Tag interpretiert und mit einem Parse-Error abbricht:
```
Parse error: syntax error, unexpected identifier "version" in feed.php on line 1
```

**Die korrekte Lösung** ist ein PHP-Block am Anfang der Datei, der:
1. Den korrekten `Content-Type`-Header setzt
2. Die `<?xml`-Deklaration per `echo` ausgibt (damit PHP sie nicht als Tag interpretiert)
3. Danach folgt normales RSS-XML

```php
<?php
header("Content-Type: application/rss+xml; charset=utf-8");
echo '<?xml version="1.0" encoding="ISO-8859-1"?>';
?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    ...
  </channel>
</rss>
```

### Pflichtfelder im RSS Feed für den Philips 8FF3WMI

Der PhotoFrame ist wählerisch – folgende Felder müssen vorhanden sein:

| Feld | Pflicht | Beschreibung |
|---|---|---|
| `<title>` | ✅ | Kanalname |
| `<link>` | ✅ | URL des Kanals (HTTP!) |
| `<description>` | ✅ | Kann leer sein, muss aber vorhanden sein |
| `<item><title>` | ✅ | Titel des Eintrags |
| `<item><link>` | ✅ | URL des Eintrags (HTTP!) |
| `<item><description>` | ✅ | Bildreferenz als `&lt;img src=&quot;URL&quot;/&gt;` |
| `<item><pubDate>` | ✅ | RFC-822 Datum z.B. `Mon, 23 Jun 2026 06:00:00 +0000` |
| `<media:content url=...>` | ✅ | Direkte Bild-URL, `type="image/jpeg"`, `width` & `height` |

> **Wichtig:** Alle URLs im Feed müssen `http://` verwenden – der PhotoFrame folgt keinen
> HTTPS-Redirects und zeigt bei HTTPS-URLs kein Bild an.

---

## Hinweise

- **Verzögerung:** GitHub Actions scheduled Jobs bei kostenlosen/öffentlichen Repositories werden häufig um 1–4 Stunden verzögert gestartet – normal
- **HAOS-Runner offline?** Falls der self-hosted Runner nicht erreichbar ist, übernimmt automatisch der GitHub-hosted Fallback-Job den FTP-Upload
- **Cateringportal nicht erreichbar?** Manuell via **Actions → Weekly Kantine Screenshot → Run workflow** neu starten
- **Session-ID rotiert?** Das Secret `CATERINGPORTAL_SID` im Repo aktualisieren – die URL-Base funktioniert auch ohne SID
- Das Projekt hat **kein End-Datum** – der Workflow läuft dauerhaft jeden Montag

---

## Verwandte Projekte

| Projekt | Beschreibung |
|---|---|
| [kicktipp-photoframe](https://github.com/basecore/kicktipp-photoframe) | Vorgänger: Täglicher Screenshot der Kicktipp WC2026 Tabelle (bis 19.07.2026) |
| [siemens-kantine-photoframe](https://github.com/basecore/siemens-kantine-photoframe) | Dieses Projekt: Wöchentlicher Speiseplan-Screenshot |
