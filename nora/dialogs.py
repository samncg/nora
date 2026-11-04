"""Small Tk dialogs for the tray: paste a key, pick a model.

Tkinter is imported lazily so the rest of Nora keeps working on machines
without a desktop session.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("nora.dialogs")

BG = "#202225"
FIELD = "#2b2d31"
FG = "#e6e8ec"
MUTED = "#9aa0aa"
ACCENT = "#5865f2"


def _tk() -> Any:
    import tkinter as tk

    return tk


def ask_text(
    title: str,
    prompt: str,
    initial: str = "",
    secret: bool = False,
) -> str | None:
    """Ask for one line of text. Returns None when cancelled."""
    tk = _tk()
    root = tk.Tk()
    root.withdraw()
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=BG)
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    tk.Label(
        win,
        text=prompt,
        bg=BG,
        fg=FG,
        justify="left",
        wraplength=430,
    ).pack(padx=18, pady=(18, 8), anchor="w")

    value = tk.StringVar(value=initial)
    entry = tk.Entry(
        win,
        textvariable=value,
        show="*" if secret else "",
        width=54,
        bg=FIELD,
        fg=FG,
        insertbackground=FG,
        relief="flat",
    )
    entry.pack(padx=18, pady=(0, 14), ipady=6)

    result: dict[str, str | None] = {"value": None}

    def ok(_event: Any = None) -> None:
        result["value"] = value.get()
        win.destroy()

    def cancel(_event: Any = None) -> None:
        win.destroy()

    row = tk.Frame(win, bg=BG)
    row.pack(padx=18, pady=(0, 16), fill="x")
    tk.Button(
        row,
        text="Save",
        command=ok,
        bg=ACCENT,
        fg="white",
        relief="flat",
        padx=16,
        pady=4,
    ).pack(side="right")
    tk.Button(
        row,
        text="Cancel",
        command=cancel,
        bg=FIELD,
        fg=FG,
        relief="flat",
        padx=16,
        pady=4,
    ).pack(side="right", padx=(0, 8))

    win.bind("<Return>", ok)
    win.bind("<Escape>", cancel)
    entry.focus_set()
    entry.icursor("end")
    win.grab_set()
    root.wait_window(win)
    root.destroy()
    return result["value"]


def choose_model(options: list[str], current: str = "") -> str | None:
    """Pick a model from a list, or type any OpenRouter slug."""
    tk = _tk()
    root = tk.Tk()
    root.withdraw()
    win = tk.Toplevel(root)
    win.title("OpenRouter model")
    win.configure(bg=BG)
    win.resizable(False, False)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    tk.Label(
        win,
        text="Pick a free model, or type any slug from openrouter.ai/models.",
        bg=BG,
        fg=MUTED,
        justify="left",
        wraplength=470,
    ).pack(padx=18, pady=(16, 8), anchor="w")

    frame = tk.Frame(win, bg=BG)
    frame.pack(padx=18, pady=(0, 10), fill="both", expand=True)

    models = list(options)
    listbox = tk.Listbox(
        frame,
        height=14,
        width=64,
        bg=FIELD,
        fg=FG,
        selectbackground=ACCENT,
        selectforeground="white",
        relief="flat",
        activestyle="none",
    )
    scroll = tk.Scrollbar(frame, command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    for item in models:
        listbox.insert("end", item)
    if current and current in models:
        index = models.index(current)
        listbox.selection_set(index)
        listbox.see(index)

    value = tk.StringVar(value=current)
    entry = tk.Entry(
        win,
        textvariable=value,
        width=54,
        bg=FIELD,
        fg=FG,
        insertbackground=FG,
        relief="flat",
    )
    entry.pack(padx=18, pady=(0, 12), ipady=6)

    result: dict[str, str | None] = {"value": None}

    def pick(_event: Any = None) -> None:
        selected = listbox.curselection()
        if selected:
            value.set(models[selected[0]])
        text = value.get().strip()
        if not text:
            return
        result["value"] = text
        win.destroy()

    def fill_entry(_event: Any = None) -> None:
        selected = listbox.curselection()
        if selected:
            value.set(models[selected[0]])

    row = tk.Frame(win, bg=BG)
    row.pack(padx=18, pady=(0, 16), fill="x")
    tk.Button(
        row,
        text="Use model",
        command=pick,
        bg=ACCENT,
        fg="white",
        relief="flat",
        padx=16,
        pady=4,
    ).pack(side="right")
    tk.Button(
        row,
        text="Cancel",
        command=lambda: win.destroy(),
        bg=FIELD,
        fg=FG,
        relief="flat",
        padx=16,
        pady=4,
    ).pack(side="right", padx=(0, 8))

    listbox.bind("<<ListboxSelect>>", fill_entry)
    listbox.bind("<Double-Button-1>", lambda _event: pick())
    win.bind("<Return>", pick)
    win.bind("<Escape>", lambda _event: win.destroy())
    entry.focus_set()
    win.grab_set()
    root.wait_window(win)
    root.destroy()
    return result["value"]
