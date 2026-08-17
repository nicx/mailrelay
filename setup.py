"""
py2app-Build-Skript für MailRelay.

Standalone-App bauen:
    python3 setup.py py2app

Schneller Alias-Build (nur lokal lauffähig, kein Verteilen):
    python3 setup.py py2app -A
"""
from setuptools import setup

APP = ["mailrelay.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/icon.icns",
    "packages": ["aiosmtpd", "keyring"],
    # AppKit/Foundation/UserNotifications werden lazy in Funktionen importiert (damit das
    # Modul ohne GUI importierbar bleibt). Explizit aufführen, damit sie sicher im Bundle
    # landen und nicht von der Import-Analyse übersehen werden.
    "includes": ["objc", "AppKit", "Foundation", "UserNotifications"],
    # Menüleisten-Template-Icon landet in Contents/Resources/
    "resources": ["assets/menubar.png"],
    "plist": {
        "CFBundleName": "MailRelay",
        "CFBundleDisplayName": "MailRelay",
        "CFBundleIdentifier": "com.github.mailrelay",  # ggf. auf eigene Reverse-DNS anpassen
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        # Menüleisten-App: kein Dock-Icon, kein App-Switcher-Eintrag
        "LSUIElement": True,
        "NSHumanReadableCopyright": "MIT – nicx",
    },
}

setup(
    app=APP,
    name="MailRelay",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    # Hinweis: kein install_requires – py2app 0.28.x lehnt das ab
    # (build_app.py: "install_requires is no longer supported").
    # Laufzeit-Abhängigkeiten stehen in requirements.txt.
)
