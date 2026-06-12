# MailRelay

Ein einfacher, nativer macOS-**Menüleisten-SMTP-Relay** (Smarthost). Nimmt Mails
lokal entgegen, legt sie in eine Disk-Queue (übersteht Neustarts) und reicht sie
an einen Upstream-SMTP-Server weiter – mit SMTP-AUTH, STARTTLS/SSL und
Retry/Backoff bei Fehlern. Das Passwort liegt im **macOS-Schlüsselbund**, nicht
im Klartext.

Gedacht als schlanker Ersatz für einen `exim`-/`postfix`-Relay-Container, wenn
man stattdessen etwas Natives mit GUI direkt auf dem Mac möchte – ohne Docker.

## Features

- Menüleisten-App mit SF-System-Symbol als Template (gleiche Optik wie System-
  Icons, passt sich Hell/Dunkel an). Status am Icon ablesbar: **läuft = gefüllter
  Umschlag** (`envelope.fill`), **gestoppt = Outline** (`envelope`) – analog zu
  matter-server/homeassistant. Fallback: gebündeltes Outline-PNG (macOS < 11).
  Kein Dock-Icon (`LSUIElement`)
- SMTP-Listener auf konfigurierbarem Host/Port (Default `127.0.0.1:2525`)
- Persistente Disk-Queue mit Retry und exponentiellem Backoff
- Upstream mit STARTTLS (587), SSL (465) oder Plain (25)
- Optionale SMTP-Authentifizierung; Passwort im Schlüsselbund
- Sicherheit: TLS-Zwang für Upstream-AUTH, optionale Absender-Allowlist (IP/CIDR)
  gegen offenes Relay, Queue/Log/Config nur für den eigenen Benutzer lesbar
- Statusanzeige, Queue-Tiefe, „sofort erneut zustellen", Log-Zugriff im Menü

## Build (macOS)

Voraussetzung: Python 3 (Homebrew-Python empfohlen).

```bash
make app          # baut dist/MailRelay.app (standalone)
# Alternativ, falls der Standalone-Build zickt:
make alias        # schneller Build, nur lokal lauffähig
```

Danach `dist/MailRelay.app` nach `/Applications` ziehen.

Erststart einer unsignierten App: per Rechtsklick → **Öffnen** bestätigen, oder
vorab das Quarantäne-Flag entfernen:

```bash
xattr -dr com.apple.quarantine /Applications/MailRelay.app
```

## Aus dem Quelltext starten (ohne Build)

```bash
make run
```

## Konfiguration

Beim ersten Start im Menü unter **Einstellungen**: Upstream-Host, Port,
Benutzername und Passwort setzen. Sobald ein Upstream hinterlegt ist, startet
das Relay beim nächsten Programmstart automatisch.

Config: `~/Library/Application Support/MailRelay/config.json`
Queue/Log: gleiches Verzeichnis (`spool/`, `failed/`, `mailrelay.log`).

### Hinweise

- Sollen andere Geräte im LAN relayen, Listen-Host auf `0.0.0.0` setzen
  (statt `127.0.0.1`, das nur lokale Verbindungen annimmt).

## Port 25 nutzen (pf-Umleitung)

Ports unter 1024 sind **privilegiert** – nur Prozesse als `root` dürfen sich
darauf binden. MailRelay läuft als normale Benutzer-App und kann Port 25 daher
nicht direkt belegen (Start scheitert mit „Start fehlgeschlagen“). Der saubere
Weg: Die App bleibt auf dem unprivilegierten Port **2525**, und macOS leitet
eingehenden Verkehr von Port 25 dorthin um – über die eingebaute Firewall `pf`.

**Am einfachsten direkt in der App:** **Einstellungen → „Port 25 weiterleiten
(25 → 2525)"** aktivieren. Das installiert die pf-Regel **und** einen
LaunchDaemon (über Neustarts persistent) nach einmaliger Eingabe des
Admin-Passworts (macOS-Dialog) und entfernt beides beim Deaktivieren wieder.
Das Regelwerk behält die Apple-Standardanker bei. Die folgenden Schritte sind
nur nötig, wenn du es **manuell** ohne die App einrichten willst.

**1. App-Einstellungen** (Menü → Einstellungen):

- Listen-Port: `2525` (**nicht** `25`)
- Listen-Host: `0.0.0.0`, damit andere Geräte im LAN senden können

**2. Umleitungsregel anlegen** – Datei `/etc/pf.anchors/mailrelay`:

```
rdr pass inet proto tcp from any to any port 25 -> 127.0.0.1 port 2525
```

**3. Regel laden** (einmalig `sudo`, da Systemeingriff):

```bash
sudo pfctl -ef /etc/pf.anchors/mailrelay   # pf aktivieren + Regel laden
sudo pfctl -sn                             # Kontrolle: rdr-Regel sichtbar?
```

**4. Persistent über Neustarts** (optional): Ein **LaunchDaemon** unter
`/Library/LaunchDaemons/` lädt die Regel beim Boot als root erneut
(`pfctl -ef /etc/pf.anchors/mailrelay`) – sonst ist sie nach einem Reboot weg.

### Stolpersteine

- **macOS-Firewall:** Beim ersten Start ggf. „Eingehende Verbindungen erlauben“
  bestätigen (Systemeinstellungen → Netzwerk → Firewall).
- **Provider blockieren Port 25:** Im LAN unkritisch; aus dem Internet sperren
  die meisten ISPs eingehenden Port 25 – das liegt dann nicht an MailRelay.
- **`0.0.0.0` öffnet den Listener für alle erreichbaren Geräte.** MailRelay
  verlangt *eingehend* keine Authentifizierung. Schränke daher die Absender ein:
  **Einstellungen → „Erlaubte Absender…“** mit IP/CIDR füllen (z. B.
  `192.168.1.0/24`) – dann werden alle anderen Geräte abgewiesen. Ist die Liste
  leer und der Listener nicht-lokal, warnt die App beim Start vor dem offenen
  Relay. Alternativ per Firewall einschränken.

### Sicherheit (Kurzüberblick)

- **AUTH nur über TLS:** Zugangsdaten werden nie über eine unverschlüsselte
  Verbindung gesendet – nutze Port 465 (SSL) oder 587 mit STARTTLS.
- **Absender-Allowlist** (IP/CIDR) gegen offenes Relay, s. o.
- **Dateirechte:** `config.json`, `mailrelay.log` und die Queue (`spool/`,
  `failed/`) gehören `0600`/`0700` – nur dein Benutzer kann sie lesen.
- **Passwort** liegt im macOS-Schlüsselbund, nicht im Klartext.
- Für „Daten at-rest“ (gequeuete Mails) zusätzlich **FileVault** aktivieren.

## Autostart

Am einfachsten direkt in der App: **Einstellungen → „Beim Login starten“**
aktivieren. Das richtet automatisch einen LaunchAgent ein
(`~/Library/LaunchAgents/com.github.mailrelay.plist`) und entfernt ihn beim
Deaktivieren wieder.

Alternativ über **Systemeinstellungen → Allgemein → Anmeldeobjekte**, oder das
Template unter `launchagent/` anpassen und nach `~/Library/LaunchAgents/`
kopieren.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
