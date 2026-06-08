# MailRelay

Ein einfacher, nativer macOS-**Menüleisten-SMTP-Relay** (Smarthost). Nimmt Mails
lokal entgegen, legt sie in eine Disk-Queue (übersteht Neustarts) und reicht sie
an einen Upstream-SMTP-Server weiter – mit SMTP-AUTH, STARTTLS/SSL und
Retry/Backoff bei Fehlern. Das Passwort liegt im **macOS-Schlüsselbund**, nicht
im Klartext.

Gedacht als schlanker Ersatz für einen `exim`-/`postfix`-Relay-Container, wenn
man stattdessen etwas Natives mit GUI direkt auf dem Mac möchte – ohne Docker.

## Features

- Menüleisten-App (✉︎), kein Dock-Icon (`LSUIElement`)
- SMTP-Listener auf konfigurierbarem Host/Port (Default `127.0.0.1:2525`)
- Persistente Disk-Queue mit Retry und exponentiellem Backoff
- Upstream mit STARTTLS (587), SSL (465) oder Plain (25)
- Optionale SMTP-Authentifizierung; Passwort im Schlüsselbund
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

- **Port 25** bräuchte root. Entweder Clients auf `2525` umstellen oder per
  `pfctl` 25 → 2525 umleiten.
- Sollen andere Geräte im LAN relayen, Listen-Host auf `0.0.0.0` setzen.

## Autostart

Am einfachsten über **Systemeinstellungen → Allgemein → Anmeldeobjekte**.
Alternativ das Template unter `launchagent/` anpassen und nach
`~/Library/LaunchAgents/` kopieren.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
