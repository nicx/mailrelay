.PHONY: venv run icon app alias clean install-dev

VENV := .venv
PYTHON ?= python3
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Signier-Identität. Default "-" = ad-hoc: baut ohne Zertifikat, vergibt aber keine
# Code-Identität — der CDHash wechselt bei jedem Rebuild, macOS erkennt die App nicht
# wieder und verwirft erteilte Berechtigungen (Mitteilungen, Gatekeeper). Mit stabiler
# selbstsignierter Identität bleiben sie erhalten:
#     CODESIGN_IDENTITY="nicx Selfsign" make app
# Verfügbare Identitäten: security find-identity -v -p codesigning
CODESIGN_IDENTITY ?= -

# Virtuelle Umgebung + Abhängigkeiten
venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-dev: venv
	$(PIP) install py2app

# Aus dem Quelltext starten (zum Testen, ohne Build)
run: venv
	$(PY) mailrelay.py

# .icns aus dem mitgelieferten iconset bauen (benötigt macOS: iconutil)
icon:
	iconutil -c icns assets/icon.iconset -o assets/icon.icns

# Echte, doppelklickbare .app bauen -> dist/MailRelay.app
app: install-dev icon
	$(PY) setup.py py2app
	codesign --force --deep --sign "$(CODESIGN_IDENTITY)" dist/MailRelay.app
	codesign --verify --deep --strict dist/MailRelay.app
	@echo "Fertig: dist/MailRelay.app (signiert mit: $(CODESIGN_IDENTITY))"
	@echo "HINWEIS: nicht aus dem Terminal starten (kein 'open') — sonst blockiert eine"
	@echo "         headless Instanz Port 2525. Per Doppelklick starten."

# Schneller Alias-Build (nur lokal lauffähig)
alias: install-dev icon
	$(PY) setup.py py2app -A
	@echo "Fertig (Alias): dist/MailRelay.app"

clean:
	rm -rf build dist $(VENV) assets/icon.icns
