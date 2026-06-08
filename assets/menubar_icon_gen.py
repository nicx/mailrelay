#!/usr/bin/env python3
"""
Erzeugt die Menüleisten-Template-Icons für MailRelay.

Zwei Zustände, beide als schwarz-auf-transparent Template (macOS recolort sie
automatisch für helle/dunkle Menüleiste):
    menubar.png         -> Relay gestoppt  (Umschlag-Outline)
    menubar-active.png  -> Relay läuft      (Umschlag + Sende-Pfeil)

rumps rendert das Icon in einer festen 20x20-pt-Box; daher füllt das Zeichen die
Zeichenfläche bewusst weitgehend aus, damit es so groß wirkt wie System-Icons.
Der Umschlag bleibt in beiden Zuständen gleich hoch positioniert; im aktiven
Zustand wird er minimal schmaler, um rechts Platz für den Pfeil zu schaffen.

Nur ein Build-Hilfsskript – benötigt Pillow, ist aber keine Laufzeit-Abhängigkeit:
    pip install pillow && python assets/menubar_icon_gen.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 8          # Supersampling für saubere Kanten
SIZE = 44      # finale Kantenlänge (px), quadratisch -> rumps zeigt es mit 20pt
HERE = Path(__file__).resolve().parent

# Geometrie im finalen 44er-Raster. Höhe in beiden Zuständen identisch (T..B),
# damit beim Umschalten kein vertikaler Sprung entsteht.
ENV_T, ENV_B = 7, 37          # Umschlag oben/unten (Höhe 30 -> ~75% der Box)
ENV_L = 4                     # linke Kante (beide Zustände)
ENV_R_IDLE = 40               # rechte Kante gestoppt  (Breite 36, zentriert)
ENV_R_ACTIVE = 33             # rechte Kante laufend    (schmaler, Platz für Pfeil)
RADIUS = 3.5
STROKE = 3.6
CY = (ENV_T + ENV_B) / 2      # vertikale Mitte


def _canvas():
    img = Image.new("RGBA", (SIZE * S, SIZE * S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _p(*pts):
    return [(x * S, y * S) for x, y in pts]


def _draw_envelope(d, right):
    w = int(STROKE * S)
    black = (0, 0, 0, 255)
    # Korpus (Outline)
    d.rounded_rectangle(_p((ENV_L, ENV_T), (right, ENV_B)),
                        radius=RADIUS * S, outline=black, width=w)
    # Klappe als V
    mid_x = (ENV_L + right) / 2
    d.line(_p((ENV_L, ENV_T), (mid_x, CY), (right, ENV_T)),
           fill=black, width=w, joint="curve")


def _draw_arrow(d):
    w = int(STROKE * S)
    black = (0, 0, 0, 255)
    d.line(_p((33, CY), (41, CY)), fill=black, width=w)                 # Schaft
    d.line(_p((37.5, CY - 4.5), (41.5, CY), (37.5, CY + 4.5)),          # Spitze
           fill=black, width=w, joint="curve")


def build(active: bool):
    img, d = _canvas()
    _draw_envelope(d, ENV_R_ACTIVE if active else ENV_R_IDLE)
    if active:
        _draw_arrow(d)
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    name = "menubar-active.png" if active else "menubar.png"
    out = HERE / name
    img.save(out)
    print("geschrieben:", out)


if __name__ == "__main__":
    build(active=False)
    build(active=True)
