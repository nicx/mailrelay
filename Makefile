.PHONY: venv run icon app alias clean install-dev guard-not-running

BUNDLE := dist/MailRelay.app
BINARY := $(CURDIR)/$(BUNDLE)/Contents/MacOS/MailRelay

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
# Läuft die App aus genau diesem dist/, würde der Build ihr das Bundle unter den Füßen
# weglöschen. Der Prozess liefe danach mit ALTEM Code aus einem gelöschten Bundle weiter,
# macOS graut ihn aus, das Menü reagiert nicht mehr — beenden ginge nur noch per `kill`.
# Bei MailRelay kommt dazu: der Port 2525 bliebe belegt. Aus /Applications gestartete
# Instanzen sind unkritisch. (Gleicher Guard wie in icloud-sync und phonebook-server.)
guard-not-running:
	@PIDS="$$(pgrep -f '$(BINARY)' || true)"; \
	if [ -n "$$PIDS" ]; then \
	  echo "ABBRUCH: MailRelay läuft aus $(CURDIR)/dist (PID: $$PIDS)." >&2; \
	  echo "         Der Build würde das laufende Bundle löschen." >&2; \
	  echo "         Erst beenden (Menüleiste -> Beenden), dann erneut bauen." >&2; \
	  exit 1; \
	fi

app: guard-not-running install-dev icon
	$(PY) setup.py py2app
	codesign --force --deep --sign "$(CODESIGN_IDENTITY)" $(BUNDLE)
	codesign --verify --deep --strict $(BUNDLE)
	@echo "Fertig: $(BUNDLE) (signiert mit: $(CODESIGN_IDENTITY))"
	@echo "HINWEIS: nicht aus dem Terminal starten (kein 'open') — sonst blockiert eine"
	@echo "         headless Instanz Port 2525. Per Doppelklick starten."

# Schneller Alias-Build (nur lokal lauffähig)
alias: guard-not-running install-dev icon
	$(PY) setup.py py2app -A
	@echo "Fertig (Alias): dist/MailRelay.app"

clean:
	rm -rf build dist $(VENV) assets/icon.icns
