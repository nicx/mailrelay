# HANDOFF – MailRelay

Übergabe-Notiz für die Fortsetzung der Arbeit (z. B. neue Claude-Sitzung auf
einem anderen Mac). Quelle der Wahrheit ist der Code + `git log`; diese Datei
fasst Stand und Entscheidungen zusammen.

## Was ist das
Native macOS-Menüleisten-App (Python + `rumps` + `aiosmtpd`), die als
SMTP-Relay/Smarthost dient: nimmt lokal Mails an, legt sie in eine Disk-Queue
und stellt sie an einen Upstream-SMTP zu (mit AUTH/STARTTLS, Retry/Backoff).
Build als `.app` via `py2app`. Repo: `https://github.com/nicx/mailrelay`.

## Build / Start
```bash
brew install python@3.13                              # falls nötig
make app PYTHON=/opt/homebrew/bin/python3.13          # -> dist/MailRelay.app
make run PYTHON=/opt/homebrew/bin/python3.13          # aus Quelltext testen
```
- Build-Python: **Homebrew 3.13** (py2app/PyObjC zuverlässiger als System-3.9).
- `setup.py` hat **kein** `install_requires` (py2app 0.28.x bricht sonst ab);
  Laufzeit-Deps stehen in `requirements.txt`.
- Pillow ist nur Build-Hilfe für `assets/menubar_icon_gen.py` (Fallback-Icon),
  keine Laufzeit-Abhängigkeit.

## Wichtige Designentscheidungen (nicht versehentlich rückgängig machen)
- **Menüleisten-Icon:** SF-System-Symbol als Template; **läuft = `envelope.fill`,
  gestoppt = `envelope`** (Outline), analog zu matter-server/homeassistant.
  Fallback: gebündeltes `assets/menubar.png` (macOS < 11). KEIN Badge, kein
  Fehlerzustand (vom Nutzer mehrfach so gewünscht).
- **Ressourcenpfad:** `resource_path`/`app_bundle_path` nutzen `RESOURCEPATH`
  (py2app), nicht nur `sys.executable` — sonst bricht der **Alias-Build**.
- **Port-25-Weiterleitung (pf):** Menüpunkt richtet per Admin-Dialog (osascript)
  einen **benannten Anker `mailrelay`** in der **gemeinsamen `/etc/pf.conf`** ein
  (additiv, idempotent) + LaunchDaemon, der `pfctl -E -f /etc/pf.conf` beim Boot
  lädt. ⚠️ **NICHT** auf ein privates Voll-Regelwerk (`pfctl -f privat.conf`)
  zurückfallen — sonst werden Anker anderer Apps (z. B. ProxyManager 80/443)
  beim Boot weggespült. Siehe Commit `cd3020a`.
- **Listen-Host `0.0.0.0`/`::` -> Controller bekommt `""`:** aiosmtpd testet nach
  dem Bind eine Verbindung gegen `self.hostname`; `0.0.0.0` ist als *Verbindungs-
  ziel* (macOS) nicht erreichbar -> Start scheitert mit „SMTP server started, but
  not responding within allotted time", obwohl der Bind klappte. `start_relay`
  übersetzt `0.0.0.0`/`::` daher in `""` (bindet ebenso alle Interfaces, Selbst-
  test läuft gegen localhost). Zusätzlich `READY_TIMEOUT=15` als Last-Sicherheitsnetz.
- **Absender erzwingen (`force_sender`):** Optionaler Sender-Rewrite. Ist gesetzt,
  schreibt `deliver()` Envelope-`MAIL FROM` **und** den `From:`-Header auf den Wert
  um (Originalabsender bleibt als `Reply-To`). `rewrite_from()` arbeitet **header-
  only** und lässt den Body byte-genau (PDF-Anhänge!) – NICHT auf
  `email…as_bytes()` der ganzen Message umstellen, das verändert den Body. Use-Case:
  HP-Scan-to-Mail setzt die Empfängeradresse als Absender -> iCloud-`550`.
- **Sicherheit:** AUTH nur über TLS; optionale Absender-Allowlist (IP/CIDR) gegen
  offenes Relay (+ Warnung bei nicht-lokalem Bind ohne Allowlist); Queue/Log/
  Config mit `0600`/`0700`; Queue-Obergrenze `MAX_QUEUE_FILES`.
- **Login-Autostart** über LaunchAgent (`open -a`, reaktiviert laufende Instanz).
- **Einstellungen = natives PyObjC-Fenster** (`run_settings_window` + reine, testbare
  `parse_relay_settings`), kein rumps-Prompt-Stapel mehr. Bewusst **app-agnostisch**
  (Feldspec + `on_commit`-Callback), damit der Builder später in ein gemeinsames Modul
  der Python-Menübar-Familie (mailrelay/icloud-sync/evcc) wandern kann. Speichern/
  Abbrechen; Passwort via `NSSecureTextField` (leer = unverändert); Seiteneffekte
  (Login-Item/pf) nur bei Zustandswechsel; Relay-Neustart nur bei Listener-Änderung.

## Richtung: rumps → PyObjC (gestaffelt, analog evcc-menu)
Strategie der Python-Familie: bei Python bleiben, UI schrittweise auf PyObjC
vereinheitlichen, rumps mittelfristig ablösen (eine Sprache, ein Repo, Tests bleiben,
kein IPC). Der aktuelle Dual-Style (rumps-Menü + PyObjC-Fenster) ist Übergang. Erledigt:
Settings-Fenster (Baustein 1). Offen: gemeinsames `menubar-ui`-Modul extrahieren +
icloud-sync/evcc darauf umstellen; dann `NSAlert`-Helfer, `NSStatusItem`+`NSMenu`,
`NSTimer`, Notifications, eigene Runloop → rumps raus.

## Was NICHT im Repo liegt (pro-Mac neu einrichten)
- Laufzeit-Config `~/Library/Application Support/MailRelay/config.json`
  (Upstream, `allowed_peers`) und das **Keychain-Passwort** (pro Mac neu setzen).
- Build-Artefakte (`dist/`, `build/`, `.venv/`), `assets/icon.icns` – regenerierbar.

## Offene/optionale Punkte
- Code-Signing + Notarisierung (Developer-ID) gegen Gatekeeper – noch offen.
- Optionaler GitHub-Actions-Release-Workflow (macOS-Runner, App bei Tag bauen).
- ~~Security-Finding M2 (Keychain-Passwort via `security`-CLI in `ps` sichtbar)~~ –
  **erledigt:** Keychain läuft jetzt über die `keyring`-Library (Security-Framework),
  kein Passwort mehr in der Prozess-Argumentliste.

## Git-Identität / Account
Repo unter GitHub-Account **nicx** (`https://github.com/nicx/mailrelay`).
