.PHONY: venv run icon app alias clean install-dev

VENV := .venv
PYTHON ?= python3
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

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
	@echo "Fertig: dist/MailRelay.app"

# Schneller Alias-Build (nur lokal lauffähig)
alias: install-dev icon
	$(PY) setup.py py2app -A
	@echo "Fertig (Alias): dist/MailRelay.app"

clean:
	rm -rf build dist $(VENV) assets/icon.icns
