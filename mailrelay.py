#!/usr/bin/env python3
"""
MailRelay - einfacher nativer macOS-Menüleisten-SMTP-Relay (Smarthost).

Nimmt Mails lokal entgegen, legt sie als Datei in eine Disk-Queue (übersteht
Neustarts) und reicht sie an einen Upstream-SMTP-Server weiter - optional mit
SMTP-AUTH + STARTTLS, mit Retry/Backoff bei Fehlern. Das Passwort liegt im
macOS-Schlüsselbund (Keychain), nicht im Klartext.

Die UI ist vollständig PyObjC/AppKit (Statusleiste, Menü, Fenster, Dialoge, Timer,
Notifications) – ``rumps`` wird nicht mehr benötigt.

Setup:
    python3 -m pip install --user pyobjc-framework-Cocoa \
        pyobjc-framework-UserNotifications aiosmtpd keyring
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
import socket
import smtplib
import logging
import plistlib
import ipaddress
import threading
import subprocess
from pathlib import Path

import keyring
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

    Quadratischer Bitmap-Rep mit erhaltenem Seitenverhältnis, weil ``StatusItem``
    das Menüleisten-Icon per ``setSize_`` auf 20x20 pt setzt (ohne das deutet NSImage
    die Pixelmaße als Punkte); ein nicht-quadratisches Bild würde sonst gestaucht.
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
    """True nur, wenn die Weiterleitung *wirklich aktiv* ist: der pf-LaunchDaemon
    existiert UND unser Anker steht noch im aktuellen /etc/pf.conf. Ein macOS-Update
    kann /etc/pf.conf zurücksetzen (Anker weg), während die Daemon-Plist unter
    /Library/LaunchDaemons bestehen bleibt — das muss als 'aus' gelten, damit die
    Checkbox nicht fälschlich 'an' zeigt und ein erneutes Anhaken die Weiterleitung
    repariert, statt in einem No-op zu verpuffen."""
    return PF_DAEMON.exists() and _anchor_present_in_pf_conf()


def _anchor_present_in_pf_conf():
    return any(
        line.strip() == _LOAD_ANCHOR_LINE
        for line in _current_pf_conf().split("\n")
    )


def port25_needs_repair():
    """True, wenn die Weiterleitung eingerichtet *war* (Daemon-Plist existiert), der
    Anker aber aus /etc/pf.conf verschwunden ist (typisch nach einem macOS-Update).
    Die Weiterleitung ist dann tot und muss neu eingerichtet werden."""
    return PF_DAEMON.exists() and not _anchor_present_in_pf_conf()


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
    "force_sender": "",           # gesetzt = Envelope-MAIL-FROM + From:-Header darauf umschreiben
    "max_retries": 10,
    "allowed_peers": [],          # leer = alle erlaubt; sonst Allowlist aus IPs/CIDR (H1)
    "alert_email": "",            # gesetzt = Warn-Mail an diese Adresse, wenn die pf-Weiterleitung ausfällt (leer = aus)
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
    # Loopback (127.0.0.1 UND ::1) ist immer erlaubt: nicht spoofbar (= diese
    # Maschine) und ein lokaler Client, der zu „localhost" verbindet, kommt auf
    # macOS je nach Auflösung als IPv4 ODER IPv6 an. Sonst müsste man beide
    # Formen einzeln in die Allowlist eintragen — häufiger Footgun.
    if ip.is_loopback:
        return True
    for entry in allowed:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            if str(peer[0]) == str(entry):
                return True
    return False


def peer_str(peer):
    """Formatiert die Peer-Adresse als ``ip:port`` fürs Log. pf schreibt bei der
    Port-25-Weiterleitung den *Ziel*-Port bereits vor dem accept auf 2525 um – die
    App sieht also nie „25". Die **Quell-IP** bleibt aber erhalten, und daran lassen
    sich Port-25-Kandidaten erkennen: ein externes LAN-Gerät (z. B. der Scanner) kam
    über die Weiterleitung, lokale Sender erscheinen als 127.0.0.1/::1/eigene LAN-IP."""
    try:
        return f"{peer[0]}:{peer[1]}"
    except (TypeError, IndexError):
        return str(peer)


def queue_count():
    try:
        return sum(1 for _ in SPOOL.glob("*.json"))
    except OSError:
        return 0


# ---------------------------------------------------------------- Keychain ---
# Über die `keyring`-Library (Security-Framework) statt des `security`-CLI: so steht
# das Passwort nie als Prozessargument in der Kommandozeile (früher via `ps` kurz
# sichtbar – Security-Finding M2). Gleicher Ansatz wie die Schwester-Apps.
def keychain_set(account, password):
    if not account:
        return
    keyring.set_password(KEYCHAIN_SERVICE, account, password)


def keychain_get(account):
    if not account:
        return ""
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, account) or ""
    except Exception:
        # Läuft auch im Worker-Thread bei jeder Zustellung – nie hart fehlschlagen.
        return ""


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
            self.log.warning("Abgelehnt (Peer nicht in Allowlist): %s", peer_str(session.peer))
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
        self.log.info("Angenommen von %s: %s -> %s (%s)",
                      peer_str(session.peer), envelope.mail_from, envelope.rcpt_tos, fn.name)
        return "250 Message accepted for delivery"


# ----------------------------------------------------------- Sender-Rewrite ---
def rewrite_from(data, new_sender):
    """Schreibt den `From:`-Header der Mail auf `new_sender` um und hängt den
    Originalabsender als `Reply-To` an (falls noch keiner gesetzt ist), damit
    Antworten weiterhin den ursprünglichen Absender erreichen.

    Nötig für Quellen, die einen Absender setzen, den der Upstream nicht
    akzeptiert (z. B. HP-Scan-to-Mail nutzt die Empfängeradresse als Absender,
    iCloud lehnt das mit „550 From address is not one of your addresses" ab).

    Bearbeitet ausschließlich den Header-Block; der Body (z. B. ein PDF-Anhang)
    bleibt byte-genau erhalten. Faltzeilen (RFC 5322 folding) werden respektiert.
    """
    new_sender = new_sender.replace("\r", "").replace("\n", "").strip()

    # Header/Body byte-genau trennen – Body wird unverändert wieder angehängt.
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = data.find(sep)
        if idx >= 0:
            head_bytes, body, sep_used = data[:idx], data[idx + len(sep):], sep
            break
    else:
        head_bytes, body, sep_used = data, b"", b"\r\n\r\n"

    eol = "\r\n" if b"\r\n" in head_bytes else "\n"
    # Header-Felder rekonstruieren (eine Faltzeile beginnt mit Space/Tab).
    fields = []
    for line in head_bytes.decode("latin-1").split(eol):
        if line[:1] in (" ", "\t") and fields:
            fields[-1].append(line)
        else:
            fields.append([line])

    def fname(field):
        return field[0].split(":", 1)[0].strip().lower() if ":" in field[0] else ""

    orig_from = next((eol.join(f) for f in fields if fname(f) == "from"), None)
    has_reply_to = any(fname(f) == "reply-to" for f in fields)

    out = []
    for f in fields:
        if fname(f) == "from":
            out.append("From: " + new_sender)
        else:
            out.extend(f)
    if orig_from and not has_reply_to:
        # Originalabsender als Reply-To erhalten (Wert nach dem ersten Doppelpunkt).
        out.append("Reply-To:" + orig_from.split(":", 1)[1])

    return eol.join(out).encode("latin-1") + sep_used + body


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

        # Optional: Absender erzwingen (Envelope-MAIL-FROM + From:-Header), damit
        # Quellen mit „falschem" Absender (HP-Scan-to-Mail, Skripte) vom Upstream
        # akzeptiert werden. Greift für vorhandene wie neue Queue-Einträge.
        force = (cfg.get("force_sender") or "").strip()
        mailfrom = rec["mailfrom"]
        if force:
            mailfrom = force
            data = rewrite_from(data, force)

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
            s.sendmail(mailfrom, rec["rcpttos"], data)
        finally:
            try:
                s.quit()
            except Exception:
                pass


# ------------------------------------------------- Settings-Fenster (PyObjC) ---
# Rumps-freies, feldgetriebenes Einstellungsfenster – Baustein 1 der schrittweisen
# rumps->PyObjC-Vereinheitlichung (analog evcc-menu/icloud-sync). Bewusst app-agnostisch:
# Validierung/Persistenz stecken im `on_commit`-Callback, damit dieser Builder später
# unverändert in ein gemeinsames Modul wandern kann.
try:  # AppKit nur lazy/guarded – das Modul bleibt auch ohne GUI importierbar
    from Foundation import NSObject as _NSObject
    # PyObjC macht aus jeder Methode einer NSObject-Subklasse einen ObjC-Selector; reine
    # Python-Helfer mit Argumenten müssen darum ausgenommen werden (sonst BadPrototypeError).
    from objc import python_method as _python_method
    _HAVE_APPKIT = True
except Exception:  # pragma: no cover - umgebungsabhängig
    _NSObject = object

    def _python_method(fn):   # Fallback ohne PyObjC
        return fn
    _HAVE_APPKIT = False

# Offene, nicht-modale Settings-Fenster müssen am Leben gehalten werden: NSWindow-Delegate
# und Button-Targets sind in AppKit *schwache* Referenzen – ohne diese Registry würde der
# Controller vom Python-GC eingesammelt und Speichern/Abbrechen liefen ins Leere.
_SETTINGS_OPEN = {}   # title -> _SettingsController


def parse_relay_settings(raw):
    """Validiert die Roh-Eingaben des Settings-Fensters (reine Logik, AppKit-frei,
    unit-testbar). Gibt ``(values, errors, dropped_peers)`` zurück; ``values`` enthält nur
    die ``cfg``-Felder (kein Passwort, keine Seiteneffekt-Schalter)."""
    errors = []

    def _port(key, label):
        v = str(raw.get(key, "")).strip()
        if v.isdigit() and 1 <= int(v) <= 65535:
            return int(v)
        errors.append(f"{label}: Zahl 1–65535")
        return None

    listen_port = _port("listen_port", "Listen-Port")
    upstream_port = _port("upstream_port", "Upstream-Port")

    peers, dropped = [], []
    for p in [x.strip() for x in str(raw.get("allowed_peers", "")).split(",") if x.strip()]:
        try:
            ipaddress.ip_network(p, strict=False)
            peers.append(p)
        except ValueError:
            dropped.append(p)

    if errors:
        return None, errors, dropped
    values = {
        "listen_host": str(raw.get("listen_host", "")).strip(),
        "listen_port": listen_port,
        "upstream_host": str(raw.get("upstream_host", "")).strip(),
        "upstream_port": upstream_port,
        "username": str(raw.get("username", "")).strip(),
        "use_starttls": bool(raw.get("use_starttls")),
        "force_sender": str(raw.get("force_sender", "")).strip(),
        "allowed_peers": peers,
        "alert_email": str(raw.get("alert_email", "")).strip(),
    }
    return values, [], dropped


class _SettingsController(_NSObject):  # type: ignore[misc]
    """Hält Controls/Fenster am Leben, bedient Speichern/Abbrechen (Target/Action) und
    dient dem Fenster als Delegate (Schließen über den roten Button = Abbrechen)."""

    def ok_(self, _sender):
        import AppKit
        raw = {}
        for key, (kind, control) in self._controls.items():
            if kind == "check":
                raw[key] = control.state() == 1
            else:  # text | int | secret
                raw[key] = control.stringValue()
        errors = self._on_commit(raw)
        if errors:
            AppKit.NSBeep()
            self._error_label.setStringValue_("  •  ".join(errors))
            self._error_label.setHidden_(False)
            return  # Fenster offen lassen, damit der Nutzer korrigieren kann
        self._finish(True)

    def cancel_(self, _sender):
        self._finish(False)

    def windowWillClose_(self, _notification):
        self._finish(False)

    @_python_method
    def _finish(self, saved):
        """Schließt das Fenster genau einmal, gibt die Registry-Referenz frei und meldet
        das Ergebnis an ``on_done``. Der ``_done``-Schalter schützt gegen Doppelaufruf
        (roter Button feuert zusätzlich ``windowWillClose_``)."""
        if self._done:
            return
        self._done = True
        self._window.setDelegate_(None)
        self._window.orderOut_(None)
        _SETTINGS_OPEN.pop(self._key, None)
        if self._on_done is not None:
            self._on_done(saved)


def run_settings_window(title, sections, initial, on_commit, on_done=None):
    """Zeigt ein **nicht-modales**, feldgetriebenes Einstellungsfenster (Main-Thread).

    **Bewusst nicht app-modal:** Eine reine Menüleisten-App (``LSUIElement``, kein Dock-Icon)
    sperrt sich mit ``runModalForWindow_`` komplett aus, sobald das Fenster den Fokus verliert
    oder außerhalb des sichtbaren Bereichs landet (VNC, Auflösungswechsel, anderer Space):
    AppKit graut während eines App-modalen Loops alle Menüs aus, und ohne Dock-Icon gibt es
    keinen Weg, das Fenster wiederzufinden – die App ist dann unbedienbar. Nicht-modal bleibt
    sie in jedem Fall benutzbar. NICHT auf ``runModalForWindow_`` zurückbauen.

    ``sections``: ``list[(section_title, rows, note|None)]`` mit
    ``rows = list[(label, kind, key)]``, ``kind ∈ text|int|secret|check``.
    ``initial``: ``dict`` key->``str``|``bool``. ``on_commit(raw) -> list[str]``: leere Liste
    = übernehmen + schließen, sonst Fehlertexte (Fenster bleibt offen, Beep).
    ``on_done(saved: bool)``: optional; ``True`` nach Speichern, ``False`` nach Abbrechen/
    Schließen. Ersetzt den früheren synchronen Rückgabewert (nicht-modal kann nicht warten).

    Ist das Fenster bereits offen, wird es nur nach vorn geholt **und neu zentriert** – das
    holt auch ein außerhalb des Viewports verirrtes Fenster zurück.
    """
    import AppKit
    from Foundation import NSMakeRect, NSMakeSize

    existing = _SETTINGS_OPEN.get(title)
    if existing is not None:
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        existing._window.center()
        existing._window.makeKeyAndOrderFront_(None)
        return

    controller = _SettingsController.alloc().init()
    controller._controls = {}
    controller._on_commit = on_commit
    pending = []

    def _label(text, bold=False, dim=False):
        lbl = AppKit.NSTextField.labelWithString_(text)
        if bold:
            lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
        if dim:
            lbl.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        return lbl

    def _make_control(kind, key):
        value = initial.get(key)
        if kind == "check":
            btn = AppKit.NSButton.checkboxWithTitle_target_action_("", None, None)
            btn.setState_(1 if value else 0)
            return btn
        cls = AppKit.NSSecureTextField if kind == "secret" else AppKit.NSTextField
        field = cls.alloc().init()
        field.setStringValue_("" if value is None else str(value))
        field.setTranslatesAutoresizingMaskIntoConstraints_(False)
        pending.append(field.widthAnchor().constraintGreaterThanOrEqualToConstant_(240))
        return field

    stack = AppKit.NSStackView.alloc().init()
    stack.setOrientation_(1)  # vertikal
    stack.setAlignment_(AppKit.NSLayoutAttributeLeading)
    stack.setSpacing_(10)
    stack.setTranslatesAutoresizingMaskIntoConstraints_(False)

    first_field = None
    for si, (section_title, rows, note) in enumerate(sections):
        if si > 0:
            sep = AppKit.NSBox.alloc().init()
            sep.setBoxType_(AppKit.NSBoxSeparator)
            stack.addArrangedSubview_(sep)
            pending.append(sep.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()))
        stack.addArrangedSubview_(_label(section_title, bold=True))
        grid_rows = []
        for label, kind, key in rows:
            control = _make_control(kind, key)
            controller._controls[key] = (kind, control)
            if first_field is None and kind not in ("check",):
                first_field = control
            grid_rows.append([_label(label + ":"), control])
        grid = AppKit.NSGridView.gridViewWithViews_(grid_rows)
        grid.setRowSpacing_(6)
        grid.setColumnSpacing_(8)
        grid.columnAtIndex_(0).setXPlacement_(AppKit.NSGridCellPlacementTrailing)
        stack.addArrangedSubview_(grid)
        if note:
            stack.addArrangedSubview_(_label(note, dim=True))

    error_label = _label("")
    error_label.setTextColor_(AppKit.NSColor.systemRedColor())
    error_label.setHidden_(True)
    controller._error_label = error_label
    stack.addArrangedSubview_(error_label)

    cancel_btn = AppKit.NSButton.buttonWithTitle_target_action_("Abbrechen", controller, "cancel:")
    cancel_btn.setKeyEquivalent_("\x1b")  # Esc
    ok_btn = AppKit.NSButton.buttonWithTitle_target_action_("Speichern", controller, "ok:")
    ok_btn.setKeyEquivalent_("\r")  # Enter = Default
    button_row = AppKit.NSStackView.alloc().init()
    button_row.setOrientation_(0)  # horizontal
    button_row.setSpacing_(10)
    spacer = AppKit.NSView.alloc().init()
    spacer.setContentHuggingPriority_forOrientation_(1, 0)  # dehnt sich
    button_row.addArrangedSubview_(spacer)
    button_row.addArrangedSubview_(cancel_btn)
    button_row.addArrangedSubview_(ok_btn)
    button_row.setTranslatesAutoresizingMaskIntoConstraints_(False)
    stack.addArrangedSubview_(button_row)
    pending.append(button_row.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()))

    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 520, 560), style, AppKit.NSBackingStoreBuffered, False)
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    controller._window = window

    content = window.contentView()
    content.addSubview_(stack)
    AppKit.NSLayoutConstraint.activateConstraints_([
        stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 16),
        stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -16),
        stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 16),
        stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -16),
    ] + pending)

    content.layoutSubtreeIfNeeded()
    fitting = stack.fittingSize()
    window.setContentSize_(NSMakeSize(max(500, fitting.width + 32), fitting.height + 32))
    if first_field is not None:
        window.setInitialFirstResponder_(first_field)

    controller._window = window
    controller._on_done = on_done
    controller._done = False
    controller._key = title
    window.setDelegate_(controller)
    _SETTINGS_OPEN[title] = controller   # Referenz halten, s. Kommentar bei _SETTINGS_OPEN

    app = AppKit.NSApplication.sharedApplication()
    app.activateIgnoringOtherApps_(True)
    window.center()
    window.makeKeyAndOrderFront_(None)


# ------------------------------------------ Statusleiste / Timer / Dialoge ---
# Baustein 2 der rumps->PyObjC-Vereinheitlichung (analog evcc/icloud-sync): ersetzt
# rumps.App/MenuItem, rumps.Timer, rumps.alert und rumps.notification. Wie der
# Settings-Builder oben bewusst app-agnostisch gehalten, damit alles später unverändert
# in ein gemeinsames Modul wandern kann.


def _run_on_main_sync(fn):
    """Führt ``fn()`` synchron auf dem Main-Thread aus; reicht Rückgabe/Fehler durch.

    AppKit ist nicht thread-sicher, der Relay-Worker läuft aber in eigenen Threads.
    Auf dem Main-Thread direkt (kein Dispatch, keine Deadlock-Gefahr).
    """
    from Foundation import NSOperationQueue, NSThread

    if NSThread.isMainThread():
        return fn()

    box, done = {}, threading.Event()

    def _wrapper():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - an den Aufrufer weiterreichen
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapper)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def alert(title, message=""):
    """Modaler Hinweis mit OK-Button (Ersatz für ``rumps.alert``)."""
    def _show():
        import AppKit

        a = AppKit.NSAlert.alloc().init()
        a.setMessageText_(title)
        if message:
            a.setInformativeText_(message)
        a.addButtonWithTitle_("OK")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        a.runModal()

    _run_on_main_sync(_show)


_notify_auth_lock = threading.Lock()
_notify_auth_done = False


def notify(title, subtitle, message):
    """macOS-Notification über ``UNUserNotificationCenter`` (best effort, asynchron).

    Ersetzt ``rumps.notification``. Zustellung nur aus dem echten, signierten Bundle
    (App-Stub) – im Dev-Modus lehnt macOS ab, das wird nur geloggt.
    """
    from Foundation import NSOperationQueue

    def _deliver():
        global _notify_auth_done
        try:
            import UserNotifications as UN

            center = UN.UNUserNotificationCenter.currentNotificationCenter()
            with _notify_auth_lock:
                first = not _notify_auth_done
                _notify_auth_done = True
            if first:
                center.requestAuthorizationWithOptions_completionHandler_(
                    UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
                    lambda granted, error: None)

            content = UN.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            if subtitle:
                content.setSubtitle_(subtitle)
            content.setBody_(message)
            req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                str(uuid.uuid4()), content, None)
            center.addNotificationRequest_withCompletionHandler_(req, None)
        except Exception as exc:  # noqa: BLE001 - Notifications sind best effort
            logging.getLogger(APP_NAME).warning("Notification fehlgeschlagen: %s", exc)

    NSOperationQueue.mainQueue().addOperationWithBlock_(_deliver)


class _Separator:
    """Sentinel für eine Trennlinie im Menü."""
    __slots__ = ()


SEPARATOR = _Separator()


class MenuEntry:
    """Ein Menüeintrag als reine Daten (AppKit-frei, damit testbar).

    ``key`` erlaubt spätere Titeländerungen **in place** (``StatusItem.set_item_title``) —
    MailRelay aktualisiert Status/Warteschlange/Gesendet im 3-Sekunden-Takt, ein
    Menü-Neuaufbau wäre dafür unnötig und würde ein geöffnetes Menü stören.
    """

    def __init__(self, title, callback=None, key=None, enabled=None):
        self.title = title
        self.callback = callback
        self.key = key
        self.enabled = enabled

    def is_enabled(self):
        """Aktiv, wenn explizit gesetzt — sonst automatisch bei vorhandenem Callback."""
        if self.enabled is not None:
            return self.enabled
        return self.callback is not None


class _MenuTarget(_NSObject):
    """Einziges Target aller Menüeinträge; verteilt Klicks anhand des ``tag``.

    Einer statt eines pro Eintrag, weil ``NSMenuItem.target`` eine *schwache* Referenz
    ist — pro-Eintrag-Objekte würden deallokiert und die Klicks liefen ins Leere.
    """

    def menuAction_(self, sender):
        cb = self._callbacks.get(int(sender.tag()))
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001 - ein Klick darf die App nie killen
            logging.getLogger(APP_NAME).exception("Menü-Callback fehlgeschlagen")


class StatusItem:
    """``NSStatusItem`` + ``NSMenu`` (Ersatz für ``rumps.App``)."""

    def __init__(self):
        import AppKit

        self._appkit = AppKit
        self._item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self._target = _MenuTarget.alloc().init()
        self._target._callbacks = {}
        self._by_key = {}
        self._images = {}
        self._cur_icon = None

    def set_icon(self, path):
        """Setzt das Template-Icon; No-op bei unverändertem Pfad (kein Flackern)."""
        if not path or path == self._cur_icon:
            return

        def _apply():
            from Foundation import NSMakeSize

            img = self._images.get(path)
            if img is None:
                img = self._appkit.NSImage.alloc().initWithContentsOfFile_(path)
                if img is None:
                    return
                img.setTemplate_(True)
                # Ohne setSize_ deutet NSImage die Pixelmaße als Punkte -> viel zu groß.
                img.setSize_(NSMakeSize(20.0, 20.0))
                self._images[path] = img
            self._item.button().setImage_(img)
            self._cur_icon = path

        _run_on_main_sync(_apply)

    def set_menu(self, entries):
        """Ersetzt das Menü vollständig durch die übergebene Spezifikation."""
        def _apply():
            AppKit = self._appkit
            self._target._callbacks = {}
            self._by_key = {}
            menu = AppKit.NSMenu.alloc().init()
            # Ohne dies aktiviert AppKit unsere bewusst deaktivierten Info-Zeilen wieder.
            menu.setAutoenablesItems_(False)
            tag = 0
            for e in entries:
                if e is SEPARATOR:
                    menu.addItem_(AppKit.NSMenuItem.separatorItem())
                    continue
                item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    e.title, None, "")
                if e.callback is not None:
                    self._target._callbacks[tag] = e.callback
                    item.setTag_(tag)
                    item.setTarget_(self._target)
                    item.setAction_("menuAction:")
                    tag += 1
                item.setEnabled_(e.is_enabled())
                if e.key:
                    self._by_key[e.key] = item
                menu.addItem_(item)
            self._item.setMenu_(menu)

        _run_on_main_sync(_apply)

    def set_item_title(self, key, title):
        """Ändert den Titel eines Eintrags ohne Menü-Neuaufbau. True, wenn ``key`` existiert."""
        item = self._by_key.get(key)
        if item is None:
            return False
        _run_on_main_sync(lambda: item.setTitle_(title))
        return True


class _TimerTarget(_NSObject):
    """ObjC-Ziel für ``NSTimer`` (der Timer hält es stark, solange er lebt)."""

    def fire_(self, _timer):
        cb = getattr(self, "_callback", None)
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001 - ein Tick darf die App nie killen
            logging.getLogger(APP_NAME).exception("Timer-Callback fehlgeschlagen")


class RepeatingTimer:
    """Wiederholender Main-Thread-Timer (Ersatz für ``rumps.Timer``).

    Läuft bewusst nur im ``NSDefaultRunLoopMode``: bei geöffnetem Menü ruht der Tick,
    sonst würden Titeländerungen dem Nutzer ins offene Menü grätschen. ``rumps.Timer``
    feuert sofort beim Start — das bildet ``fire_immediately`` nach.
    """

    def __init__(self, interval, callback, fire_immediately=False):
        self._interval = max(0.1, float(interval))
        self._target = _TimerTarget.alloc().init()
        self._target._callback = callback
        self._fire_immediately = fire_immediately
        self._timer = None

    def start(self):
        def _apply():
            if self._timer is not None:
                return
            from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop, NSTimer

            if self._fire_immediately:
                t = NSTimer.alloc().initWithFireDate_interval_target_selector_userInfo_repeats_(
                    NSDate.date(), self._interval, self._target, "fire:", None, True)
                NSRunLoop.currentRunLoop().addTimer_forMode_(t, NSDefaultRunLoopMode)
            else:
                t = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    self._interval, self._target, "fire:", None, True)
            t.setTolerance_(self._interval * 0.1)
            self._timer = t

        _run_on_main_sync(_apply)

    def stop(self):
        def _apply():
            if self._timer is not None:
                self._timer.invalidate()
                self._timer = None

        _run_on_main_sync(_apply)


# --------------------------------------------------------------- Menü-App ---
class MailRelayApp:
    def __init__(self):
        self.status = StatusItem()
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
        # pf-Selbstüberwachung: warnt per Mail, wenn die Port-25-Weiterleitung
        # unerwartet ausfällt (Anker nach macOS-Update aus /etc/pf.conf verschwunden).
        self._pf_repair_alerted = False
        self._pf_last_check = 0.0

        # Die vier oberen Einträge tragen einen `key`: ihre Titel wechseln zur Laufzeit
        # (Status, Start/Stop, Warteschlange, Gesendet) und werden per set_item_title
        # in place aktualisiert, ohne das Menü neu zu bauen.
        self.status.set_menu([
            MenuEntry("Status: gestoppt", key="status"),
            MenuEntry("Start", self.toggle, key="toggle"),
            SEPARATOR,
            MenuEntry("Warteschlange: 0", self.flush_now, key="queue"),
            MenuEntry("Gesendet: 0", key="sent"),
            SEPARATOR,
            MenuEntry("Einstellungen…", self.open_settings),
            MenuEntry("Log öffnen…", self.open_log),
            MenuEntry("Konfigurationsdatei öffnen…", self.open_config),
            SEPARATOR,
            MenuEntry("Beenden", self.quit_app),
        ])

        self.update_icon()    # Initialzustand (gestoppt -> Outline)

        # UI-Aktualisierung auf dem Main-Thread. fire_immediately bildet das bisherige
        # rumps-Verhalten ab (erster Tick sofort statt erst nach 3 s).
        self.timer = RepeatingTimer(3, self.tick, fire_immediately=True)
        self.timer.start()

        # Automatisch starten, wenn ein Upstream hinterlegt ist
        if self.cfg.get("upstream_host"):
            self.start_relay()

    # -------------------------------------------------------- Hilfsfunktionen
    def tick(self):
        try:
            n = len(list(SPOOL.glob("*.json")))
        except Exception:
            n = 0
        self.status.set_item_title("queue", f"Warteschlange: {n}")
        self.status.set_item_title("sent", f"Gesendet: {self.sent_count}")
        self._check_pf_alert()

    def _check_pf_alert(self):
        """Warnt per Mail, wenn die Port-25-Weiterleitung unerwartet ausfällt (Anker
        nach macOS-Update aus /etc/pf.conf weg) – und schickt eine Entwarnung, sobald
        sie repariert ist. Höchstens alle 60 s geprüft; dedupliziert über einen State-
        Schalter. Die Mail läuft über die eigene Warteschlange zum Upstream und ist
        damit unabhängig von der (eingehenden) Port-25-Weiterleitung."""
        recipient = (self.cfg.get("alert_email") or "").strip()
        if not recipient:
            return
        now = time.time()
        if now - self._pf_last_check < 60:
            return
        self._pf_last_check = now
        try:
            broken = port25_needs_repair()
        except Exception:
            return
        host = socket.gethostname()
        if broken and not self._pf_repair_alerted:
            if self._enqueue_alert(
                recipient,
                f"MailRelay: Port-25-Weiterleitung inaktiv ({host})",
                "Die pf-Weiterleitung auf Port 25 ist nicht mehr aktiv "
                f"(Anker fehlt in /etc/pf.conf – meist nach einem macOS-Update) auf {host}.\n\n"
                "Eingehende Mail auf Port 25 wird derzeit nicht angenommen. Der ausgehende "
                "Relay ist davon nicht betroffen.\n\n"
                "Beheben: MailRelay, Einstellungen, Haken bei ‚Port 25 weiterleiten‘ "
                "neu setzen und speichern (Admin-Passwort).",
            ):
                self._pf_repair_alerted = True
        elif not broken and self._pf_repair_alerted:
            if self._enqueue_alert(
                recipient,
                f"MailRelay: Port-25-Weiterleitung wieder aktiv ({host})",
                f"Die pf-Weiterleitung auf Port 25 ist auf {host} wieder eingerichtet.",
            ):
                self._pf_repair_alerted = False

    def _enqueue_alert(self, recipient, subject, body):
        """Legt eine Warn-Mail als RFC-822-Nachricht in die Warteschlange; der Worker
        stellt sie mit Retry/Backoff zum Upstream zu. Absender = force_sender falls
        gesetzt, sonst der Empfänger selbst. True bei Erfolg."""
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid
        try:
            sender = (self.cfg.get("force_sender") or "").strip() or recipient
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = recipient
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain="mailrelay.local")
            msg.set_content(body)
            rec = {
                "mailfrom": sender,
                "rcpttos": [recipient],
                "data": base64.b64encode(msg.as_bytes()).decode(),
                "attempts": 0,
                "next_try": 0,
            }
            fn = SPOOL / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
            write_private(fn, json.dumps(rec))
            self.log.info("pf-Warnmail eingereiht: %s -> %s", subject, recipient)
            return True
        except Exception as e:
            self.log.warning("pf-Warnmail nicht eingereiht: %s", e)
            return False

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
        self.status.set_icon(self._icon_for(symbol))

    # ----------------------------------------------------------- Start/Stop
    def start_relay(self):
        if self.controller:
            return
        try:
            handler = RelayHandler(self)
            # aiosmtpd testet nach dem Bind eine Verbindung gegen self.hostname.
            # "0.0.0.0"/"::" sind als *Verbindungsziel* (macOS) nicht erreichbar
            # -> der Selbsttest scheitert mit „not responding within allotted
            # time", obwohl der Bind klappte. Leerer Hostname bindet ebenfalls
            # alle Interfaces, lässt aiosmtpd aber gegen localhost testen.
            listen_host = self.cfg["listen_host"]
            controller_host = "" if listen_host in ("0.0.0.0", "::") else listen_host
            self.controller = Controller(
                handler,
                hostname=controller_host,
                port=int(self.cfg["listen_port"]),
                ready_timeout=READY_TIMEOUT,
            )
            self.controller.start()
            self.worker = RelayWorker(self)
            self.worker.start()
        except Exception as e:
            self.controller = None
            alert(APP_NAME, f"Start fehlgeschlagen:\n{e}")
            self.log.error("Start fehlgeschlagen: %s", e)
            return
        self.status.set_item_title(
            "status", f"Status: läuft ({self.cfg['listen_host']}:{self.cfg['listen_port']})")
        self.status.set_item_title("toggle", "Stop")
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
            notify(
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
        self.status.set_item_title("status", "Status: gestoppt")
        self.status.set_item_title("toggle", "Start")
        self.update_icon()
        self.log.info("Relay gestoppt")

    def toggle(self):
        if self.controller:
            self.stop_relay()
        else:
            self.start_relay()

    # ----------------------------------------------------------- Einstellungen
    def _restart_hint(self):
        if self.controller:
            self.stop_relay()
            self.start_relay()

    def open_settings(self):
        """Öffnet das native PyObjC-Einstellungsfenster (alle Felder auf einen Blick)."""
        sections = [
            ("Listener", [
                ("Listen-Host", "text", "listen_host"),
                ("Listen-Port", "int", "listen_port"),
            ], "0.0.0.0 = im LAN erreichbar, 127.0.0.1 = nur lokal."),
            ("Upstream", [
                ("Host", "text", "upstream_host"),
                ("Port", "int", "upstream_port"),
                ("Benutzername", "text", "username"),
                ("Passwort", "secret", "password"),
                ("STARTTLS verwenden", "check", "use_starttls"),
            ], "Port 587 = STARTTLS, 465 = SSL, 25 = plain. Passwort leer lassen = "
               "unverändert. AUTH wird nur über TLS gesendet."),
            ("Sicherheit / Absender", [
                ("Absender erzwingen", "text", "force_sender"),
                ("Erlaubte Absender", "text", "allowed_peers"),
            ], "Absender erzwingen: Upstream-Adresse → Envelope + From: werden "
               "umgeschrieben (leer = aus).\nErlaubte Absender: IP/CIDR, kommagetrennt "
               "(leer = alle erlauben)."),
            ("System", [
                ("Beim Login starten", "check", "login_item"),
                ("Port 25 weiterleiten (pf)", "check", "port25"),
                ("Warn-Mail an", "text", "alert_email"),
            ], "Login-Autostart braucht die installierte .app. Port-25-Weiterleitung "
               "fragt nach dem Admin-Passwort.\nWarn-Mail an: Adresse, die eine Mail "
               "erhält, wenn die Port-25-Weiterleitung unerwartet ausfällt (z. B. nach "
               "einem macOS-Update). Leer = aus."),
        ]
        initial = {
            "listen_host": self.cfg.get("listen_host", ""),
            "listen_port": self.cfg.get("listen_port", ""),
            "upstream_host": self.cfg.get("upstream_host", ""),
            "upstream_port": self.cfg.get("upstream_port", ""),
            "username": self.cfg.get("username", ""),
            "password": "",
            "use_starttls": bool(self.cfg.get("use_starttls", True)),
            "force_sender": self.cfg.get("force_sender", ""),
            "allowed_peers": ", ".join(self.cfg.get("allowed_peers") or []),
            "login_item": login_item_enabled(),
            "port25": port25_redirect_enabled(),
            "alert_email": self.cfg.get("alert_email", ""),
        }
        self._settings_notices = []

        def _done(saved):
            # Läuft nach Speichern/Abbrechen; das Fenster ist nicht-modal, daher kann
            # open_settings nicht mehr auf ein Ergebnis warten (s. run_settings_window).
            if saved:
                for note in self._settings_notices:
                    alert(APP_NAME, note)

        run_settings_window("MailRelay – Einstellungen", sections, initial,
                            self._commit_settings, _done)

    def _commit_settings(self, raw):
        """Validiert + übernimmt die Fenster-Eingaben. Rückgabe: Fehlerliste (leer =
        erfolgreich/schließen). Nicht-blockierende Hinweise sammelt ``_settings_notices``."""
        values, errors, dropped = parse_relay_settings(raw)
        if errors:
            return errors

        old = (self.cfg.get("listen_host"), self.cfg.get("listen_port"),
               self.cfg.get("allowed_peers"))
        self.cfg.update(values)
        save_config(self.cfg)
        if dropped:
            self.log.warning("Ignorierte Absender (keine IP/CIDR): %s", ", ".join(dropped))
            self._settings_notices.append(
                "Ignoriert (keine gültige IP/CIDR):\n" + ", ".join(dropped))

        # Passwort nur bei Eingabe ändern (leer = unverändert).
        pw = raw.get("password") or ""
        if pw:
            if values["username"]:
                try:
                    keychain_set(values["username"], pw)
                except Exception as e:
                    self._settings_notices.append(f"Passwort nicht gespeichert:\n{e}")
            else:
                self._settings_notices.append(
                    "Passwort ignoriert – es ist kein Benutzername gesetzt.")

        # Seiteneffekt: Login-Autostart nur bei Zustandswechsel.
        want_login = bool(raw.get("login_item"))
        if want_login != login_item_enabled():
            try:
                set_login_item(want_login)
                self.log.info("Login-Autostart: %s", "ein" if want_login else "aus")
            except Exception as e:
                self._settings_notices.append(f"Login-Autostart nicht geändert:\n{e}")

        # Seiteneffekt: pf-Weiterleitung nur bei Zustandswechsel.
        want25 = bool(raw.get("port25"))
        if want25 != port25_redirect_enabled():
            if want25 and int(values["listen_port"]) == 25:
                self._settings_notices.append(
                    "Listen-Port ist 25 – eine Weiterleitung ergibt keinen Sinn.")
            else:
                try:
                    set_port25_redirect(want25, values["listen_port"])
                    self.log.info("Port-25-Weiterleitung: %s (-> %s)",
                                  "ein" if want25 else "aus", values["listen_port"])
                except Exception as e:
                    self._settings_notices.append(
                        f"Port-25-Weiterleitung nicht geändert:\n{e}")

        # Relay nur neu starten, wenn Listener-relevante Felder sich änderten.
        if old != (self.cfg["listen_host"], self.cfg["listen_port"],
                   self.cfg["allowed_peers"]):
            self._restart_hint()
        return []

    # ----------------------------------------------------------- Aktionen
    def flush_now(self):
        for f in SPOOL.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
                rec["next_try"] = 0
                write_private(f, json.dumps(rec))
            except Exception:
                pass
        notify(APP_NAME, "", "Warteschlange wird jetzt erneut zugestellt.")

    def open_config(self):
        save_config(self.cfg)
        subprocess.run(["open", str(CONFIG_PATH)])

    def open_log(self):
        LOG_PATH.touch(exist_ok=True)
        subprocess.run(["open", str(LOG_PATH)])

    def quit_app(self):
        import AppKit

        self.timer.stop()
        self.stop_relay()
        AppKit.NSApplication.sharedApplication().terminate_(None)


def main():
    """Startet die Menüleisten-App und übergibt an den AppKit-Runloop."""
    import AppKit

    ns_app = AppKit.NSApplication.sharedApplication()
    # „Accessory": Menüleisten-Resident ohne Dock-Icon (entspricht LSUIElement, gilt
    # aber auch im Dev-Modus ohne Bundle).
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app = MailRelayApp()   # Referenz halten: Status-Item und Timer hängen daran
    logging.getLogger(APP_NAME).info("Menüleisten-Item aktiv, Runloop startet.")
    ns_app.run()
    del app


if __name__ == "__main__":
    main()
