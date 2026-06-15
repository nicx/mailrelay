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
- **Sicherheit:** AUTH nur über TLS; optionale Absender-Allowlist (IP/CIDR) gegen
  offenes Relay (+ Warnung bei nicht-lokalem Bind ohne Allowlist); Queue/Log/
  Config mit `0600`/`0700`; Queue-Obergrenze `MAX_QUEUE_FILES`.
- **Login-Autostart** über LaunchAgent (`open -a`, reaktiviert laufende Instanz).

## Was NICHT im Repo liegt (pro-Mac neu einrichten)
- Laufzeit-Config `~/Library/Application Support/MailRelay/config.json`
  (Upstream, `allowed_peers`) und das **Keychain-Passwort** (pro Mac neu setzen).
- Build-Artefakte (`dist/`, `build/`, `.venv/`), `assets/icon.icns` – regenerierbar.

## Offene/optionale Punkte
- Code-Signing + Notarisierung (Developer-ID) gegen Gatekeeper – noch offen.
- Optionaler GitHub-Actions-Release-Workflow (macOS-Runner, App bei Tag bauen).
- Security-Finding M2 (Keychain-Passwort als `security`-CLI-Argument kurz via
  `ps` sichtbar) – bewusst offen; Umstellung auf Security-Framework-API möglich.

## Git-Identität / Account
Repo unter GitHub-Account **nicx** (`https://github.com/nicx/mailrelay`).
