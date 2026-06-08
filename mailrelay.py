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
    python3 mailrelay.py        # erscheint als ✉︎ in der Menüleiste
"""

import os
import ssl
import json
import time
import uuid
import base64
import smtplib
import logging
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

DEFAULTS = {
    "listen_host": "127.0.0.1",   # auf 0.0.0.0 setzen, wenn andere LAN-Geräte relayen sollen
    "listen_port": 2525,          # Port 25 bräuchte root -> lieber Client umstellen oder pf-Redirect
    "upstream_host": "",
    "upstream_port": 587,
    "username": "",
    "use_starttls": True,
    "max_retries": 10,
}


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
    SUPPORT.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def setup_logging():
    SUPPORT.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(APP_NAME)
    if not log.handlers:
        log.setLevel(logging.INFO)
        h = logging.FileHandler(LOG_PATH)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    return log


# --------------------------------------------------------- SMTP-Annahme ---
class RelayHandler:
    """Nimmt Mails an und legt sie auf die Disk-Queue."""

    def __init__(self, spool_dir, log):
        self.spool_dir = spool_dir
        self.log = log

    async def handle_DATA(self, server, session, envelope):
        rec = {
            "mailfrom": envelope.mail_from,
            "rcpttos": list(envelope.rcpt_tos),
            "data": base64.b64encode(envelope.content).decode(),
            "attempts": 0,
            "next_try": 0,
        }
        fn = self.spool_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        fn.write_text(json.dumps(rec))
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
                    FAILED.mkdir(parents=True, exist_ok=True)
                    f.rename(FAILED / f.name)
                    self.app.log.error(
                        "Aufgegeben: %s nach %d Versuchen -> failed/",
                        f.name, rec["attempts"],
                    )
                else:
                    f.write_text(json.dumps(rec))

    def deliver(self, cfg, rec):
        host = cfg.get("upstream_host", "")
        port = int(cfg.get("upstream_port", 587))
        if not host:
            raise RuntimeError("kein Upstream-Host konfiguriert")
        data = base64.b64decode(rec["data"])

        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30,
                                 context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            if cfg.get("use_starttls", True):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
        try:
            user = cfg.get("username", "")
            if user:
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
        super().__init__(APP_NAME, title="✉︎", quit_button=None)
        SUPPORT.mkdir(parents=True, exist_ok=True)
        SPOOL.mkdir(parents=True, exist_ok=True)
        self.log = setup_logging()
        self.cfg = load_config()
        save_config(self.cfg)
        self.controller = None
        self.worker = None
        self.sent_count = 0

        self.status_item = rumps.MenuItem("Status: gestoppt")
        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.queue_item = rumps.MenuItem("Warteschlange: 0", callback=self.flush_now)
        self.sent_item = rumps.MenuItem("Gesendet: 0")

        self.tls_item = rumps.MenuItem("STARTTLS verwenden", callback=self.toggle_tls)
        self.tls_item.state = 1 if self.cfg.get("use_starttls", True) else 0

        settings = rumps.MenuItem("Einstellungen")
        settings.update([
            rumps.MenuItem("Listen-Host…", callback=self.set_listen_host),
            rumps.MenuItem("Listen-Port…", callback=self.set_listen_port),
            None,
            rumps.MenuItem("Upstream-Host…", callback=self.set_upstream_host),
            rumps.MenuItem("Upstream-Port…", callback=self.set_upstream_port),
            rumps.MenuItem("Benutzername…", callback=self.set_username),
            rumps.MenuItem("Passwort…", callback=self.set_password),
            self.tls_item,
            None,
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

    # ----------------------------------------------------------- Start/Stop
    def start_relay(self):
        if self.controller:
            return
        try:
            handler = RelayHandler(SPOOL, self.log)
            self.controller = Controller(
                handler,
                hostname=self.cfg["listen_host"],
                port=int(self.cfg["listen_port"]),
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
        self.title = "✉️"
        self.log.info("Relay gestartet auf %s:%s",
                      self.cfg["listen_host"], self.cfg["listen_port"])

    def stop_relay(self):
        if self.controller:
            self.controller.stop()
            self.controller = None
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.status_item.title = "Status: gestoppt"
        self.toggle_item.title = "Start"
        self.title = "✉︎"
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

    # ----------------------------------------------------------- Aktionen
    def flush_now(self, _):
        for f in SPOOL.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
                rec["next_try"] = 0
                f.write_text(json.dumps(rec))
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
