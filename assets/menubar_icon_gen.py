#!/usr/bin/env python3
"""
Erzeugt die Menüleisten-Template-Icons für MailRelay.

Zwei Zustände, beide als schwarz-auf-transparent Template (macOS recolort sie
automatisch für helle/dunkle Menüleiste):
    menubar.png         -> Relay gestoppt  (Umschlag-Outline)
    menubar-active.png  -> Relay läuft      (Umschlag gefüllt)

Gefüllt vs. umrandet ist bei Menüleisten-Größe klar unterscheidbar – ohne einen
zusätzlichen Punkt/Pfeil, der dort sonst nur wie ein Klecks wirkt. Geometrie in
beiden Zuständen identisch, daher kein Sprung beim Start/Stop.

rumps rendert das Icon in einer festen 20x20-pt-Box; daher füllt der Umschlag
die Zeichenfläche bewusst weitgehend aus, damit er so groß wirkt wie System-Icons.

Nur ein Build-Hilfsskript – benötigt Pillow, ist aber keine Laufzeit-Abhängigkeit:
    pip install pillow && python assets/menubar_icon_gen.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 8          # Supersampling für saubere Kanten
SIZE = 44      # finale Kantenlänge (px), quadratisch -> rumps zeigt es mit 20pt
HERE = Path(__file__).resolve().parent

# Umschlag-Geometrie im finalen 44er-Raster (zentriert, füllt die Box gut aus).
ENV_L, ENV_R = 4, 40          # links/rechts (Breite 36)
ENV_T, ENV_B = 7, 37          # oben/unten   (Höhe 30 -> ~75% der Box)
RADIUS = 3.5
STROKE = 3.6
CY = (ENV_T + ENV_B) / 2      # Spitze der V-Klappe (vertikale Mitte)
MID_X = (ENV_L + ENV_R) / 2
BLACK = (0, 0, 0, 255)


def _p(*pts):
    return [(x * S, y * S) for x, y in pts]


def _outline():
    """Relay gestoppt: nur die Umschlag-Kontur."""
    img = Image.new("RGBA", (SIZE * S, SIZE * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = int(STROKE * S)
    d.rounded_rectangle(_p((ENV_L, ENV_T), (ENV_R, ENV_B)),
                        radius=RADIUS * S, outline=BLACK, width=w)
    d.line(_p((ENV_L, ENV_T), (MID_X, CY), (ENV_R, ENV_T)),
           fill=BLACK, width=w, joint="curve")
    return img


def _filled():
    """Relay läuft: gefüllter Umschlag mit ausgesparter Klappen-Linie."""
    # Alpha-Maske bauen: Korpus voll, V-Klappe als Aussparung (Wert 0).
    mask = Image.new("L", (SIZE * S, SIZE * S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle(_p((ENV_L, ENV_T), (ENV_R, ENV_B)),
                         radius=RADIUS * S, fill=255)
    md.line(_p((ENV_L, ENV_T), (MID_X, CY), (ENV_R, ENV_T)),
            fill=0, width=int(STROKE * S), joint="curve")
    img = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    img.paste((0, 0, 0, 255), (0, 0), mask)   # schwarz dort, wo Maske > 0
    return img


def build(active: bool):
    img = (_filled() if active else _outline()).resize((SIZE, SIZE), Image.LANCZOS)
    name = "menubar-active.png" if active else "menubar.png"
    out = HERE / name
    img.save(out)
    print("geschrieben:", out)


if __name__ == "__main__":
    build(active=False)
    build(active=True)
