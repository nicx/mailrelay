#!/usr/bin/env python3
"""
MailRelay - einfacher nativer macOS-Menüleisten-SMTP-Relay (Smarthost).

Nimmt Mails lokal entgegen, legt sie als Datei in eine Disk-Queue (übersteht
Neustarts) und reicht sie an einen Upstream-SMTP-Server weiter - optional mit
SMTP-AUTH + STARTTLS, mit Retry/Backoff bei Fehlern. Das Passwort liegt im
macOS-Schlüsselbund (Keychain), nicht im Klartext.

Setup:
    python3 -m pip install --user rumps aiosmtpd
Start:
    python3 mailrelay.py        # erscheint als Briefumschlag-Icon in der Menüleiste
"""

import os
import ssl
import sys
import json
import time
import uuid
import base64
import smtplib
import logging
import plistlib
import ipaddress
import threading
import subprocess
from pathlib import Path

import rumps
from aiosmtpd.controller import Controller

__version__ = "1.0.0"

APP_NAME = "MailRelay"
SUPPORT = Path.home() / "Library" / "Application Support" / APP_NAME
SPOOL = SUPPORT / "spool"
FAILED = SUPPORT / "failed"
CONFIG_PATH = SUPPORT / "config.json"
LOG_PATH = SUPPORT / "mailrelay.log"
KEYCHAIN_SERVICE = "MailRelay-upstream"


def resource_path(name):
    """Absoluter Pfad zu einer gebündelten Ressource.

    Funktioniert aus dem Quelltext (./assets/<name>) wie auch aus der gebauten
    py2app-.app (dort liegen Ressourcen unter Contents/Resources/<name>).
    """
    if getattr(sys, "frozen", False):
        # py2app setzt RESOURCEPATH auf Contents/Resources. Wichtig v. a. beim
        # Alias-Build, wo sys.executable auf den (Homebrew-)Python-Interpreter
        # zeigt statt ins Bundle – die Pfadberechnung wäre dann falsch.
        res = os.environ.get("RESOURCEPATH")
        base = Path(res) if res else Path(sys.executable).resolve().parent.parent / "Resources"
    else:
        base = Path(__file__).resolve().parent / "assets"
    return str(base / name)


# Menüleisten-Symbol: bevorzugt das SF-System-Symbol „envelope" (gleiche Optik
# und Größe wie System-Icons); als Fallback das gebündelte Outline-PNG.
ICON = resource_path("menubar.png")

# SF-Symbol bei 22 pt @2x – wie bei den Schwester-Apps (icloud-sync etc.)
_SF_POINTS = 22
_SF_SCALE = 2


def render_sf_menubar_icon(symbol="envelope"):
    """Rendert das SF-Symbol als Template-PNG nach App Support und gibt den Pfad
    zurück. None, wenn nicht möglich (z. B. macOS < 11) -> Fallback auf ICON.

    Quadratischer Bitmap-Rep mit erhaltenem Seitenverhältnis, weil rumps das
    Menüleisten-Icon auf 20x20 zwingt; ein nicht-quadratisches Bild würde sonst
    gestaucht.
    """
    try:
        import AppKit
        from Foundation import NSMakeRect, NSSize
    except Exception:
        return None
    if not hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
        return None
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
    if img is None:
        return None
    cfg_cls = getattr(AppKit, "NSImageSymbolConfiguration", None)
    if cfg_cls is not None:
        img = img.imageWithSymbolConfiguration_(
            cfg_cls.configurationWithPointSize_weight_(float(_SF_POINTS), 0.0)
        ) or img
    img.setTemplate_(True)
    sz = img.size()
    sw, sh = (sz.width or _SF_POINTS), (sz.height or _SF_POINTS)
    side = max(sw, sh)
    px = int(round(side * _SF_SCALE))
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bitmapFormat_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0, 0
    )
    rep.setSize_(NSSize(side, side))
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    AppKit.NSColor.blackColor().set()
    img.drawInRect_(NSMakeRect((side - sw) / 2, (side - sh) / 2, sw, sh))  # zentriert
    AppKit.NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if png is None:
        return None
    secure_dir(SUPPORT)
    dest = SUPPORT / ("menubar_%s.png" % symbol.replace(".", "_"))
    png.writeToFile_atomically_(str(dest), True)
    return str(dest)


# --------------------------------------------------------- Login-Autostart ---
LOGIN_LABEL = "com.github.mailrelay"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGIN_PLIST = LAUNCH_AGENTS / f"{LOGIN_LABEL}.plist"


def app_bundle_path():
    """Pfad zur .app, wenn aus dem Bundle gestartet; sonst None (Quelltext)."""
    if getattr(sys, "frozen", False):
        # Bevorzugt aus RESOURCEPATH ableiten (…/MailRelay.app/Contents/Resources
        # -> .app), da sys.executable im Alias-Build auf den Python-Interpreter
        # zeigt statt ins Bundle.
        res = os.environ.get("RESOURCEPATH")
        if res:
            return Path(res).resolve().parent.parent
        return Path(sys.executable).resolve().parents[2]
    return None


def login_item_enabled():
    """True, wenn der LaunchAgent für den Login-Autostart installiert ist."""
    return LOGIN_PLIST.exists()


def set_login_item(enabled):
    """Login-Autostart über einen LaunchAgent ein-/ausschalten.

    Wirft RuntimeError, wenn aus dem Quelltext gestartet (keine .app vorhanden).
    """
    if enabled:
        bundle = app_bundle_path()
        if bundle is None:
            raise RuntimeError(
                "Login-Autostart funktioniert nur mit der installierten "
                "MailRelay.app, nicht beim Start aus dem Quelltext."
            )
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        # open -a ist absichtlich gewählt: läuft die App schon, wird die
        # bestehende Instanz aktiviert statt eine zweite zu starten.
        data = {
            "Label": LOGIN_LABEL,
            "ProgramArguments": ["/usr/bin/open", "-a", str(bundle)],
            "RunAtLoad": True,
        }
        with open(LOGIN_PLIST, "wb") as f:
            plistlib.dump(data, f)
        subprocess.run(["launchctl", "unload", str(LOGIN_PLIST)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(LOGIN_PLIST)], capture_output=True)
    else:
        if LOGIN_PLIST.exists():
            subprocess.run(["launchctl", "unload", "-w", str(LOGIN_PLIST)], capture_output=True)
            LOGIN_PLIST.unlink(missing_ok=True)


# -------------------------------------------------- Port-25-Weiterleitung (pf) ---
# Port 25 ist privilegiert (Root). Statt die App als Root laufen zu lassen, leitet
# macOS' Paketfilter pf eingehend 25 -> 127.0.0.1:<listen_port> um. Ein LaunchDaemon
# macht die Regel über Neustarts persistent. Installation/Entfernung erfordert
# einmalig Admin-Rechte (macOS-Auth-Dialog via osascript).
PF_LABEL = "com.github.mailrelay.pf"
PF_DAEMON = Path("/Library/LaunchDaemons") / f"{PF_LABEL}.plist"
PF_ANCHOR = Path("/etc/pf.anchors/mailrelay")
PF_CONF = Path("/etc/pf.conf")
PF_ANCHOR_NAME = "mailrelay"
_RDR_ANCHOR_LINE = f'rdr-anchor "{PF_ANCHOR_NAME}"'
_LOAD_ANCHOR_LINE = f'load anchor "{PF_ANCHOR_NAME}" from "{PF_ANCHOR}"'

# Fallback, falls /etc/pf.conf fehlt (entspricht Apples Default).
_DEFAULT_PF_CONF = (
    'scrub-anchor "com.apple/*"\n'
    'nat-anchor "com.apple/*"\n'
    'rdr-anchor "com.apple/*"\n'
    'dummynet-anchor "com.apple/*"\n'
    'anchor "com.apple/*"\n'
    'load anchor "com.apple" from "/etc/pf.anchors/com.apple"\n'
)


def port25_redirect_enabled():
    """True, wenn der pf-LaunchDaemon für die Port-25-Weiterleitung installiert ist."""
    return PF_DAEMON.exists()


def _run_with_admin(shell_cmd):
    """Führt shell_cmd mit Admin-Rechten aus (macOS-Auth-Dialog). RuntimeError bei
    Abbruch/Fehler (z. B. Passwort-Dialog abgebrochen)."""
    apple = 'do shell script "%s" with administrator privileges' % (
        shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    )
    r = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip() or "abgebrochen")


def _current_pf_conf():
    try:
        return PF_CONF.read_text()
    except OSError:
        return _DEFAULT_PF_CONF


def _last_index_with_prefix(lines, prefix):
    found = None
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            found = i
    return found


def _conf_by_adding_anchor(original):
    """Registriert unseren benannten Anker idempotent im Haupt-Regelwerk und
    erhält dabei andere Anker (z. B. den von ProxyManager) — so kommen sich
    mehrere pf-nutzende Apps nicht in die Quere."""
    lines = original.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    if _RDR_ANCHOR_LINE not in lines:
        # rdr-anchor gehört in den Translation-Abschnitt, vor die Filter-Anker.
        idx = _last_index_with_prefix(lines, "rdr-anchor")
        if idx is None:
            idx = _last_index_with_prefix(lines, "nat-anchor")
        if idx is None:
            idx = _last_index_with_prefix(lines, "scrub-anchor")
        if idx is None:
            lines.insert(0, _RDR_ANCHOR_LINE)
        else:
            lines.insert(idx + 1, _RDR_ANCHOR_LINE)
    if _LOAD_ANCHOR_LINE not in lines:
        lines.append(_LOAD_ANCHOR_LINE)
    return "\n".join(lines) + "\n"


def _conf_by_removing_anchor(original):
    return "\n".join(
        line for line in original.split("\n")
        if line != _RDR_ANCHOR_LINE and line != _LOAD_ANCHOR_LINE
    )


def _anchor_rule(target_port):
    """Reiner Anker-Inhalt (nur die rdr-Regel) — kein Voll-Regelwerk."""
    return f"rdr pass inet proto tcp from any to any port 25 -> 127.0.0.1 port {int(target_port)}\n"


def set_port25_redirect(enabled, target_port):
    """pf-Weiterleitung 25 -> 127.0.0.1:target_port als LaunchDaemon ein-/ausschalten.
    Erfordert einmalig Admin-Rechte. Koexistenz-Modell: trägt einen benannten
    Anker in /etc/pf.conf ein und lädt diese gemeinsame Datei, statt das gesamte
    Regelwerk zu ersetzen (so bleiben Anker anderer Apps aktiv)."""
    daemon = f"'{PF_DAEMON}'"
    anchor = str(PF_ANCHOR)
    secure_dir(SUPPORT)
    tmp_conf = SUPPORT / "pf-conf.tmp"
    if enabled:
        tmp_anchor = SUPPORT / "pf-anchor.tmp"
        tmp_plist = SUPPORT / "pf-daemon.tmp"
        tmp_anchor.write_text(_anchor_rule(target_port))
        tmp_conf.write_text(_conf_by_adding_anchor(_current_pf_conf()))
        with open(tmp_plist, "wb") as f:
            plistlib.dump(
                {
                    "Label": PF_LABEL,
                    "ProgramArguments": ["/sbin/pfctl", "-E", "-f", "/etc/pf.conf"],
                    "RunAtLoad": True,
                },
                f,
            )
        cmd = (
            f"mkdir -p /etc/pf.anchors && "
            f"cp '{tmp_anchor}' {anchor} && chown root:wheel {anchor} && chmod 644 {anchor} && "
            f"(cp -n /etc/pf.conf /etc/pf.conf.orig.mailrelay 2>/dev/null || true) && "
            f"/sbin/pfctl -nf '{tmp_conf}' && "                    # Regelwerk validieren
            f"cp '{tmp_conf}' /etc/pf.conf && chown root:wheel /etc/pf.conf && chmod 644 /etc/pf.conf && "
            f"cp '{tmp_plist}' {daemon} && chown root:wheel {daemon} && chmod 644 {daemon} && "
            f"(launchctl bootout system {daemon} 2>/dev/null || true) && "
            f"launchctl bootstrap system {daemon} && "
            f"/sbin/pfctl -E -f /etc/pf.conf"
        )
        _run_with_admin(cmd)
    else:
        tmp_conf.write_text(_conf_by_removing_anchor(_current_pf_conf()))
        cmd = (
            f"(launchctl bootout system {daemon} 2>/dev/null || true); "
            f"rm -f {daemon} {anchor}; "
            f"cp '{tmp_conf}' /etc/pf.conf && chown root:wheel /etc/pf.conf && chmod 644 /etc/pf.conf; "
            f"/sbin/pfctl -E -f /etc/pf.conf 2>/dev/null; true"
        )
        _run_with_admin(cmd)


DEFAULTS = {
    "listen_host": "127.0.0.1",   # auf 0.0.0.0 setzen, wenn andere LAN-Geräte relayen sollen
    "listen_port": 2525,          # Port 25 bräuchte root -> lieber Client umstellen oder pf-Redirect
    "upstream_host": "",
    "upstream_port": 587,
    "username": "",
    "use_starttls": True,
    "max_retries": 10,
    "allowed_peers": [],          # leer = alle erlaubt; sonst Allowlist aus IPs/CIDR (H1)
}

# Sicherheits-Konstanten
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MAX_QUEUE_FILES = 10000           # DoS-Schutz: Obergrenze der Warteschlange (M3)
DIR_MODE = 0o700                  # Verzeichnisrechte: nur Eigentümer (M1)
FILE_MODE = 0o600                 # Dateirechte: nur Eigentümer (M1)
# aiosmtpd-Default ist 1 s; beim Autostart direkt nach Login/Reboot ist das
# System unter Last und der Server-Thread meldet „ready" oft nicht rechtzeitig
# (-> „SMTP server started, but not responding within allotted time"). Großzügig.
READY_TIMEOUT = 15.0


# ---------------------------------------------------- Datei-/Verzeichnisrechte ---
def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def secure_dir(path):
    """Verzeichnis anlegen und auf 0700 (nur Eigentümer) setzen."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIR_MODE)


def write_private(path, text):
    """Datei schreiben und auf 0600 (nur Eigentümer) setzen."""
    path.write_text(text)
    _chmod(path, FILE_MODE)


def peer_allowed(peer, allowed):
    """True, wenn die Peer-IP in der Allowlist (IPs/CIDR) liegt. Leere Liste -> alle."""
    if not allowed:
        return True
    try:
        ip = ipaddress.ip_address(peer[0])
    except (ValueError, TypeError, IndexError):
        return False
    for entry in allowed:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            if str(peer[0]) == str(entry):
                return True
    return False


def queue_count():
    try:
        return sum(1 for _ in SPOOL.glob("*.json"))
    except OSError:
        return 0


# ---------------------------------------------------------------- Keychain ---
def keychain_set(account, password):
    if not account:
        return
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", account, "-w", password],
        capture_output=True,
    )


def keychain_get(account):
    if not account:
        return ""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


# ------------------------------------------------------------------ Config ---
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    secure_dir(SUPPORT)
    write_private(CONFIG_PATH, json.dumps(cfg, indent=2))


def setup_logging():
    secure_dir(SUPPORT)
    log = logging.getLogger(APP_NAME)
    if not log.handlers:
        log.setLevel(logging.INFO)
        h = logging.FileHandler(LOG_PATH)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    _chmod(LOG_PATH, FILE_MODE)
    return log


# --------------------------------------------------------- SMTP-Annahme ---
class RelayHandler:
    """Nimmt Mails an und legt sie auf die Disk-Queue."""

    def __init__(self, app):
        self.app = app
        self.spool_dir = SPOOL
        self.log = app.log

    async def handle_DATA(self, server, session, envelope):
        # H1: optionale Peer-Allowlist gegen offenes Relay
        allowed = self.app.cfg.get("allowed_peers") or []
        if not peer_allowed(session.peer, allowed):
            self.log.warning("Abgelehnt (Peer nicht in Allowlist): %s", session.peer)
            return "550 Sender host not allowed"

        # M3: Warteschlange begrenzen (DoS-Schutz)
        if queue_count() >= MAX_QUEUE_FILES:
            self.log.error("Warteschlange voll (>= %d) – Mail abgelehnt", MAX_QUEUE_FILES)
            return "452 Insufficient system storage, try again later"

        rec = {
            "mailfrom": envelope.mail_from,
            "rcpttos": list(envelope.rcpt_tos),
            "data": base64.b64encode(envelope.content).decode(),
            "attempts": 0,
            "next_try": 0,
        }
        fn = self.spool_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        write_private(fn, json.dumps(rec))
        self.log.info("Angenommen: %s -> %s (%s)",
                      envelope.mail_from, envelope.rcpt_tos, fn.name)
        return "250 Message accepted for delivery"


# ---------------------------------------------------- Zustellung / Worker ---
class RelayWorker(threading.Thread):
    """Liest die Queue und stellt an den Upstream zu, mit Retry/Backoff."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            self.process_once()
            self._stop.wait(5)

    def process_once(self):
        cfg = self.app.cfg
        for f in sorted(SPOOL.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except Exception:
                continue
            if rec.get("next_try", 0) > time.time():
                continue
            try:
                self.deliver(cfg, rec)
                f.unlink(missing_ok=True)
                self.app.sent_count += 1
                self.app.log.info("Zugestellt: %s", f.name)
            except Exception as e:
                rec["attempts"] = rec.get("attempts", 0) + 1
                backoff = min(300, 5 * (2 ** rec["attempts"]))
                rec["next_try"] = time.time() + backoff
                self.app.log.warning(
                    "Fehlgeschlagen: %s (Versuch %d): %s; neuer Versuch in %ds",
                    f.name, rec["attempts"], e, backoff,
                )
                if rec["attempts"] >= int(cfg.get("max_retries", 10)):
                    secure_dir(FAILED)
                    f.rename(FAILED / f.name)
                    self.app.log.error(
                        "Aufgegeben: %s nach %d Versuchen -> failed/",
                        f.name, rec["attempts"],
                    )
                else:
                    write_private(f, json.dumps(rec))

    def deliver(self, cfg, rec):
        host = cfg.get("upstream_host", "")
        port = int(cfg.get("upstream_port", 587))
        if not host:
            raise RuntimeError("kein Upstream-Host konfiguriert")
        data = base64.b64decode(rec["data"])

        secure = False
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30,
                                 context=ssl.create_default_context())
            secure = True
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            if cfg.get("use_starttls", True):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                secure = True
        try:
            user = cfg.get("username", "")
            if user:
                # H2: Zugangsdaten niemals über eine unverschlüsselte Verbindung senden
                if not secure:
                    raise RuntimeError(
                        "AUTH nur über TLS erlaubt – STARTTLS aktivieren oder Port 465 nutzen"
                    )
                s.login(user, keychain_get(user))
            s.sendmail(rec["mailfrom"], rec["rcpttos"], data)
        finally:
            try:
                s.quit()
            except Exception:
                pass


# --------------------------------------------------------------- Menü-App ---
class MailRelayApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, icon=ICON, template=True, quit_button=None)
        secure_dir(SUPPORT)
        secure_dir(SPOOL)
        self.log = setup_logging()
        self.cfg = load_config()
        save_config(self.cfg)
        self.controller = None
        self.worker = None
        self.sent_count = 0
        self._cur_symbol = None        # aktuell gesetztes Menüleisten-Symbol
        self._icon_cache = {}          # Symbol -> gerenderter Template-PNG-Pfad

        self.status_item = rumps.MenuItem("Status: gestoppt")
        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.queue_item = rumps.MenuItem("Warteschlange: 0", callback=self.flush_now)
        self.sent_item = rumps.MenuItem("Gesendet: 0")

        self.tls_item = rumps.MenuItem("STARTTLS verwenden", callback=self.toggle_tls)
        self.tls_item.state = 1 if self.cfg.get("use_starttls", True) else 0

        self.login_item = rumps.MenuItem("Beim Login starten", callback=self.toggle_login_item)
        self.login_item.state = 1 if login_item_enabled() else 0

        self.pf_item = rumps.MenuItem(
            "Port 25 weiterleiten (25 → %s)" % self.cfg.get("listen_port", 2525),
            callback=self.toggle_port25,
        )
        self.pf_item.state = 1 if port25_redirect_enabled() else 0

        settings = rumps.MenuItem("Einstellungen")
        settings.update([
            rumps.MenuItem("Listen-Host…", callback=self.set_listen_host),
            rumps.MenuItem("Listen-Port…", callback=self.set_listen_port),
            rumps.MenuItem("Erlaubte Absender…", callback=self.set_allowed_peers),
            None,
            rumps.MenuItem("Upstream-Host…", callback=self.set_upstream_host),
            rumps.MenuItem("Upstream-Port…", callback=self.set_upstream_port),
            rumps.MenuItem("Benutzername…", callback=self.set_username),
            rumps.MenuItem("Passwort…", callback=self.set_password),
            self.tls_item,
            None,
            self.login_item,
            self.pf_item,
            rumps.MenuItem("Konfigurationsdatei öffnen…", callback=self.open_config),
        ])

        self.menu = [
            self.status_item,
            self.toggle_item,
            None,
            self.queue_item,
            self.sent_item,
            rumps.MenuItem("Log öffnen…", callback=self.open_log),
            None,
            settings,
            None,
            rumps.MenuItem("Beenden", callback=self.quit_app),
        ]

        self.update_icon()    # Initialzustand (gestoppt -> Outline)

        # UI-Aktualisierung auf dem Main-Thread
        rumps.Timer(self.tick, 3).start()

        # Automatisch starten, wenn ein Upstream hinterlegt ist
        if self.cfg.get("upstream_host"):
            self.start_relay()

    # -------------------------------------------------------- Hilfsfunktionen
    def prompt(self, message, default=""):
        w = rumps.Window(message=message, title=APP_NAME,
                         default_text=str(default), ok="Speichern",
                         cancel="Abbrechen", dimensions=(320, 24))
        resp = w.run()
        return resp.text.strip() if resp.clicked else None

    def tick(self, _):
        try:
            n = len(list(SPOOL.glob("*.json")))
        except Exception:
            n = 0
        self.queue_item.title = f"Warteschlange: {n}"
        self.sent_item.title = f"Gesendet: {self.sent_count}"

    # ----------------------------------------------------------- Menüleisten-Icon
    def _icon_for(self, symbol):
        """Gerendertes Template-PNG für ein SF-Symbol (gecacht); Fallback ICON."""
        if symbol not in self._icon_cache:
            self._icon_cache[symbol] = render_sf_menubar_icon(symbol) or ICON
        return self._icon_cache[symbol]

    def update_icon(self):
        """Symbol nach Status: läuft = gefüllt (envelope.fill), gestoppt = Outline
        (envelope) – analog zu matter-server / homeassistant."""
        symbol = "envelope.fill" if self.controller else "envelope"
        if symbol == self._cur_symbol:
            return
        self._cur_symbol = symbol
        self.icon = self._icon_for(symbol)

    # ----------------------------------------------------------- Start/Stop
    def start_relay(self):
        if self.controller:
            return
        try:
            handler = RelayHandler(self)
            self.controller = Controller(
                handler,
                hostname=self.cfg["listen_host"],
                port=int(self.cfg["listen_port"]),
                ready_timeout=READY_TIMEOUT,
            )
            self.controller.start()
            self.worker = RelayWorker(self)
            self.worker.start()
        except Exception as e:
            self.controller = None
            rumps.alert(APP_NAME, f"Start fehlgeschlagen:\n{e}")
            self.log.error("Start fehlgeschlagen: %s", e)
            return
        self.status_item.title = (
            f"Status: läuft ({self.cfg['listen_host']}:{self.cfg['listen_port']})"
        )
        self.toggle_item.title = "Stop"
        self.update_icon()
        self.log.info("Relay gestartet auf %s:%s",
                      self.cfg["listen_host"], self.cfg["listen_port"])

        # H1: Warnen, wenn nicht-lokal gebunden und keine Allowlist gesetzt ist
        host = self.cfg["listen_host"]
        if host not in LOOPBACK_HOSTS and not (self.cfg.get("allowed_peers") or []):
            self.log.warning(
                "OFFENES RELAY: lauscht auf %s ohne Peer-Allowlist – jedes "
                "erreichbare Gerät kann über den Upstream versenden.", host
            )
            rumps.notification(
                APP_NAME, "Sicherheitshinweis",
                "Relay ist im Netz offen erreichbar. Unter Einstellungen → "
                "„Erlaubte Absender…“ einschränken oder Listen-Host 127.0.0.1.",
            )

    def stop_relay(self):
        if self.controller:
            self.controller.stop()
            self.controller = None
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.status_item.title = "Status: gestoppt"
        self.toggle_item.title = "Start"
        self.update_icon()
        self.log.info("Relay gestoppt")

    def toggle(self, _):
        if self.controller:
            self.stop_relay()
        else:
            self.start_relay()

    # ----------------------------------------------------------- Einstellungen
    def _restart_hint(self):
        if self.controller:
            self.stop_relay()
            self.start_relay()

    def set_listen_host(self, _):
        v = self.prompt("Listen-Host (z. B. 127.0.0.1 oder 0.0.0.0):",
                        self.cfg["listen_host"])
        if v is not None:
            self.cfg["listen_host"] = v
            save_config(self.cfg)
            self._restart_hint()

    def set_listen_port(self, _):
        v = self.prompt("Listen-Port (z. B. 2525):", self.cfg["listen_port"])
        if v is not None and v.isdigit():
            self.cfg["listen_port"] = int(v)
            save_config(self.cfg)
            self._restart_hint()

    def set_allowed_peers(self, _):
        cur = ", ".join(self.cfg.get("allowed_peers") or [])
        v = self.prompt(
            "Erlaubte Absender als IP/CIDR, kommagetrennt\n"
            "(leer = alle erlauben, z. B. 192.168.1.0/24, 10.0.0.5):",
            cur,
        )
        if v is not None:
            peers = [p.strip() for p in v.split(",") if p.strip()]
            # Validierung: ungültige Einträge verwerfen und melden
            valid, invalid = [], []
            for p in peers:
                try:
                    ipaddress.ip_network(p, strict=False)
                    valid.append(p)
                except ValueError:
                    invalid.append(p)
            self.cfg["allowed_peers"] = valid
            save_config(self.cfg)
            if invalid:
                rumps.alert(APP_NAME, "Ignoriert (keine gültige IP/CIDR):\n" + ", ".join(invalid))
            self._restart_hint()

    def set_upstream_host(self, _):
        v = self.prompt("Upstream-SMTP-Host:", self.cfg["upstream_host"])
        if v is not None:
            self.cfg["upstream_host"] = v
            save_config(self.cfg)

    def set_upstream_port(self, _):
        v = self.prompt("Upstream-Port (587 = STARTTLS, 465 = SSL, 25 = plain):",
                        self.cfg["upstream_port"])
        if v is not None and v.isdigit():
            self.cfg["upstream_port"] = int(v)
            save_config(self.cfg)

    def set_username(self, _):
        v = self.prompt("Benutzername (leer lassen = keine Auth):",
                        self.cfg["username"])
        if v is not None:
            self.cfg["username"] = v
            save_config(self.cfg)

    def set_password(self, _):
        user = self.cfg.get("username", "")
        if not user:
            rumps.alert(APP_NAME, "Bitte zuerst einen Benutzernamen setzen.")
            return
        v = self.prompt(f"Passwort für {user} (wird im Schlüsselbund gespeichert):", "")
        if v is not None:
            keychain_set(user, v)
            rumps.notification(APP_NAME, "", "Passwort im Schlüsselbund gespeichert.")

    def toggle_tls(self, sender):
        sender.state = 0 if sender.state else 1
        self.cfg["use_starttls"] = bool(sender.state)
        save_config(self.cfg)

    def toggle_login_item(self, sender):
        want = not bool(sender.state)
        try:
            set_login_item(want)
        except Exception as e:
            rumps.alert(APP_NAME, f"Login-Autostart konnte nicht geändert werden:\n{e}")
            return
        sender.state = 1 if want else 0
        self.log.info("Login-Autostart: %s", "ein" if want else "aus")

    def toggle_port25(self, sender):
        want = not bool(sender.state)
        port = self.cfg.get("listen_port", 2525)
        if want and int(port) == 25:
            rumps.alert(APP_NAME, "Listen-Port ist bereits 25 – eine Umleitung ergibt keinen Sinn.")
            return
        try:
            set_port25_redirect(want, port)
        except Exception as e:
            rumps.alert(APP_NAME, f"Port-25-Weiterleitung konnte nicht geändert werden:\n{e}")
            return
        sender.state = 1 if want else 0
        self.log.info("Port-25-Weiterleitung: %s (-> %s)", "ein" if want else "aus", port)
        rumps.notification(
            APP_NAME, "",
            f"Port-25-Weiterleitung aktiv: 25 → {port}." if want
            else "Port-25-Weiterleitung deaktiviert.",
        )

    # ----------------------------------------------------------- Aktionen
    def flush_now(self, _):
        for f in SPOOL.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
                rec["next_try"] = 0
                write_private(f, json.dumps(rec))
            except Exception:
                pass
        rumps.notification(APP_NAME, "", "Warteschlange wird jetzt erneut zugestellt.")

    def open_config(self, _):
        save_config(self.cfg)
        subprocess.run(["open", str(CONFIG_PATH)])

    def open_log(self, _):
        LOG_PATH.touch(exist_ok=True)
        subprocess.run(["open", str(LOG_PATH)])

    def quit_app(self, _):
        self.stop_relay()
        rumps.quit_application()


if __name__ == "__main__":
    MailRelayApp().run()
