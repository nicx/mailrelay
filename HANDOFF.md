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
- **Settings-Fenster ist bewusst NICHT app-modal** (seit 2026-07-17). `runModalForWindow_`
  hatte die App an einem Tag zweimal komplett gesperrt: Während eines App-modalen Loops
  graut AppKit **alle** Menüleisten-Einträge aus, und als `LSUIElement` (kein Dock-Icon)
  gibt es keinen Weg, ein Fenster wiederzufinden, das den Fokus verloren hat oder außerhalb
  des sichtbaren Bereichs liegt (VNC, Auflösungswechsel, anderer Space) – die App war
  unbedienbar, während der Relay munter weiterlief. Jetzt: normales Fenster + Delegate,
  `on_done(saved)`-Callback statt synchronem Rückgabewert (nicht-modal kann nicht warten),
  erneutes Öffnen holt das Fenster nach vorn **und zentriert neu** (rettet ein verirrtes
  Fenster zurück). Zwei Fallstricke, die dabei gelöst sind: `_SETTINGS_OPEN` muss den
  Controller halten (NSWindow-Delegate und Button-Targets sind **schwache** Referenzen –
  sonst sammelt der Python-GC ihn ein und die Buttons sind tot), und reine Python-Helfer
  mit Argumenten in der NSObject-Subklasse brauchen `@_python_method`, sonst macht PyObjC
  einen Selector daraus (`BadPrototypeError`). ⚠️ **NICHT** auf `runModalForWindow_`
  zurückbauen. Symptom, falls doch: Menüs ausgegraut, `sample <pid>` zeigt den Main-Thread
  in `-[NSApplication runModalForWindow:]`.

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

## Bekanntes Problem: reiner IPv4-Loopback (`127.0.0.1`) nimmt nur 1 Verbindung an
Direkte Verbindungen von `127.0.0.1` -> `127.0.0.1:2525` funktionieren nur **einmal**;
danach bleibt der TCP-Handshake hängen (Client `SYN_SENT` / Server `SYN_RCVD`), bis der
Listener nach kurzer Idle-Zeit wieder „einen Schuss" freigibt. **Nur reiner IPv4-Loopback
ist betroffen** – nicht die LAN-IP und nicht IPv6.

Eingegrenzt (2026-07-11):
- **pf ausgeschlossen** (Anker `mailrelay` geleert -> Bug bleibt).
- **Code/aiosmtpd ausgeschlossen** (identischer Controller/Handler aus dem Quelltext im
  venv-Python nimmt Loopback unbegrenzt an; trivialer asyncio-Server auf `127.0.0.1` ebenso).
- Reproduziert **nur im py2app-Bundle-Prozess** (der die kopierte Homebrew-Runtime nutzt,
  die als venv sauber läuft) – ab der ersten Verbindung, auch bei frischer Instanz.
  Verdacht: Prozess-/Startkontext des per LaunchServices gestarteten Bundles. Grundursache
  noch offen; nächste Schritte: Bundle-Binary im Vordergrund mit sichtbarem stderr (das
  Bundle wirft stderr sonst nach `/dev/null` -> asyncio-Traceback geht verloren) oder
  `sudo tcpdump -i lo0 port 2525` während eines hängenden Verbindungsaufbaus.

**Workaround / Empfehlung:** Lokale Sender auf **`192.168.2.1`** (LAN-IP des Mac, in der
Allowlist über `192.168.2.0/24` abgedeckt) **oder `::1`** statt `127.0.0.1` zeigen lassen.
Beide nehmen unbegrenzt an (verifiziert). Betroffene lokale Sender im Haus, die per Default
`127.0.0.1:2525` nutzen: **icloud-sync, evcc, home-assistant- und esphome-Menübar-App**.
Der reguläre Mailfluss (Gerät -> `LAN-IP:25` -> pf-rdr -> `127.0.0.1:2525`) ist **nicht**
betroffen, weil die Quelle dort die LAN-Adresse bleibt (kein reiner Loopback).

Nebenbei am 2026-07-11 gefixt (Commit `868f944`): `peer_allowed` erlaubt Loopback jetzt
grundsätzlich (`ip.is_loopback`), sonst wurde ein zu „localhost" verbindender Client, der
auf macOS als `::1` ankommt, trotz `127.0.0.1` in der Allowlist mit `550` abgelehnt.

## Git-Identität / Account
Repo unter GitHub-Account **nicx** (`https://github.com/nicx/mailrelay`).
