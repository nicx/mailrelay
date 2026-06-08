#!/usr/bin/env python3
"""
Erzeugt die Menüleisten-Template-Icons für MailRelay.

Zwei Zustände, beide als schwarz-auf-transparent Template (macOS recolort sie
automatisch für helle/dunkle Menüleiste):
    menubar.png         -> Relay gestoppt  (Umschlag-Outline)
    menubar-active.png  -> Relay läuft      (Umschlag + Sende-Pfeil)

Nur ein Build-Hilfsskript – benötigt Pillow, ist aber keine Laufzeit-Abhängigkeit:
    pip install pillow && python assets/menubar_icon_gen.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 8          # Supersampling für saubere Kanten
SIZE = 44      # finale Kantenlänge (px), quadratisch -> rumps zeigt es mit 20pt
HERE = Path(__file__).resolve().parent

# Geometrie im finalen 44er-Raster. Der Umschlag bleibt in beiden Zuständen
# exakt gleich positioniert; im aktiven Zustand kommt nur rechts der Pfeil dazu.
ENV_L, ENV_T, ENV_R, ENV_B = 5, 13, 33, 32   # Umschlag-Korpus
FLAP_MID_Y = 25                                # Spitze der V-Klappe
RADIUS = 3
STROKE = 3.4


def _canvas():
    img = Image.new("RGBA", (SIZE * S, SIZE * S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _p(*pts):
    return [(x * S, y * S) for x, y in pts]


def _draw_envelope(d):
    w = int(STROKE * S)
    # Korpus (Outline)
    d.rounded_rectangle(
        _p((ENV_L, ENV_T), (ENV_R, ENV_B))[0] + _p((ENV_L, ENV_T), (ENV_R, ENV_B))[1],
        radius=RADIUS * S, outline=(0, 0, 0, 255), width=w,
    )
    # Klappe als V
    mid_x = (ENV_L + ENV_R) / 2
    d.line(_p((ENV_L, ENV_T), (mid_x, FLAP_MID_Y), (ENV_R, ENV_T)),
           fill=(0, 0, 0, 255), width=w, joint="curve")


def _draw_arrow(d):
    w = int(STROKE * S)
    cy = (ENV_T + ENV_B) / 2          # vertikale Mitte des Umschlags
    # Schaft
    d.line(_p((33, cy), (40, cy)), fill=(0, 0, 0, 255), width=w)
    # Pfeilspitze
    d.line(_p((36.5, cy - 3.5), (40, cy), (36.5, cy + 3.5)),
           fill=(0, 0, 0, 255), width=w, joint="curve")


def build(active: bool):
    img, d = _canvas()
    _draw_envelope(d)
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
