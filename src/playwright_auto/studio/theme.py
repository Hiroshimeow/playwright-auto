from __future__ import annotations

import tkinter as tk
from tkinter import ttk

COLORS = {
    "bg": "#0b1220",
    "panel": "#111a2b",
    "panel_alt": "#162237",
    "card": "#172235",
    "card_hover": "#1d2b42",
    "border": "#2a3a54",
    "text": "#e8eef8",
    "muted": "#93a4bc",
    "accent": "#45d483",
    "accent_dark": "#1f8f5a",
    "blue": "#54a7ff",
    "amber": "#f5b942",
    "red": "#ff6b6b",
    "purple": "#a78bfa",
    "input": "#0e1728",
    "selection": "#264a68",
}

STATUS_COLORS = {
    "idle": COLORS["muted"],
    "queued": COLORS["blue"],
    "preparing": COLORS["amber"],
    "running": COLORS["accent"],
    "waiting": COLORS["amber"],
    "completed": COLORS["accent"],
    "paused": COLORS["amber"],
    "stopping": COLORS["amber"],
    "stopped": COLORS["muted"],
    "error": COLORS["red"],
    "disconnected": COLORS["red"],
    "auth_required": COLORS["red"],
}

GLOW_COLORS = (
    "#173c2c",
    "#1c5c3d",
    "#238552",
    "#2dbf70",
    "#46e48e",
    "#2dbf70",
    "#238552",
    "#1c5c3d",
)


def configure_theme(root: tk.Misc) -> ttk.Style:
    root.option_add("*Font", ("Segoe UI", 10))
    root.option_add("*TCombobox*Listbox.background", COLORS["panel_alt"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["selection"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("Studio.TFrame", background=COLORS["bg"])
    style.configure("Panel.TFrame", background=COLORS["panel"])
    style.configure("Card.TFrame", background=COLORS["card"])
    style.configure("Studio.TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
    style.configure(
        "Title.TLabel",
        background=COLORS["panel"],
        foreground=COLORS["text"],
        font=("Segoe UI Semibold", 13),
    )
    style.configure(
        "Header.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["text"],
        font=("Segoe UI Semibold", 12),
    )
    style.configure(
        "Accent.TButton",
        background=COLORS["accent_dark"],
        foreground="#ffffff",
        padding=(12, 7),
        borderwidth=0,
    )
    style.map(
        "Accent.TButton",
        background=[("active", COLORS["accent"]), ("disabled", COLORS["border"])],
        foreground=[("disabled", COLORS["muted"])],
    )
    style.configure(
        "Danger.TButton",
        background="#8e3340",
        foreground="#ffffff",
        padding=(12, 7),
        borderwidth=0,
    )
    style.map("Danger.TButton", background=[("active", COLORS["red"])])
    style.configure(
        "Quiet.TButton",
        background=COLORS["panel_alt"],
        foreground=COLORS["text"],
        padding=(9, 6),
        borderwidth=0,
    )
    style.map("Quiet.TButton", background=[("active", COLORS["border"])])
    style.configure(
        "Studio.TEntry",
        fieldbackground=COLORS["input"],
        foreground=COLORS["text"],
        insertcolor=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        padding=6,
    )
    style.configure(
        "Studio.TCombobox",
        fieldbackground=COLORS["input"],
        background=COLORS["panel_alt"],
        foreground=COLORS["text"],
        arrowcolor=COLORS["muted"],
        bordercolor=COLORS["border"],
        padding=5,
    )
    style.map(
        "Studio.TCombobox",
        fieldbackground=[("readonly", COLORS["input"])],
        foreground=[("readonly", COLORS["text"])],
    )
    style.configure(
        "Studio.TCheckbutton",
        background=COLORS["card"],
        foreground=COLORS["text"],
        focuscolor=COLORS["card"],
    )
    style.map("Studio.TCheckbutton", background=[("active", COLORS["card_hover"])])
    style.configure(
        "Studio.TNotebook",
        background=COLORS["panel"],
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "Studio.TNotebook.Tab",
        background=COLORS["panel_alt"],
        foreground=COLORS["muted"],
        padding=(10, 7),
        borderwidth=0,
    )
    style.map(
        "Studio.TNotebook.Tab",
        background=[("selected", COLORS["card"]), ("active", COLORS["border"])],
        foreground=[("selected", COLORS["text"])],
    )
    style.configure(
        "Studio.Vertical.TScrollbar",
        background=COLORS["panel_alt"],
        troughcolor=COLORS["bg"],
        arrowcolor=COLORS["muted"],
        borderwidth=0,
    )
    return style
