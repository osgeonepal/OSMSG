"""Minimal tkinter desktop UI for running osmsg and saving the output."""

from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from .__version__ import __version__
from .exceptions import NoDataFoundError, OsmsgError
from .pipeline import RunConfig, run

UTC = dt.UTC
FORMATS = ["parquet", "csv", "json", "markdown"]
ABOUT_LINKS = [
    ("Star osmsg on GitHub", "https://github.com/osgeonepal/osmsg"),
    ("Report a bug or request a feature", "https://github.com/osgeonepal/osmsg/issues"),
    ("Sponsor the developer", "https://github.com/sponsors/kshitijrajsharma"),
]
PRESETS = ["Last hour", "Last day", "Last week", "Last month", "Last year", "All time"]
_PRESET_DELTAS = {
    "Last hour": dt.timedelta(hours=1),
    "Last day": dt.timedelta(days=1),
    "Last week": dt.timedelta(days=7),
    "Last month": dt.timedelta(days=30),
    "Last year": dt.timedelta(days=365),
}


def preset_range(name: str, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Resolve a quick-range label to a (start, end) window."""
    now = now or dt.datetime.now(UTC)
    if name == "All time":
        return dt.datetime(2005, 1, 1, tzinfo=UTC), now
    return now - _PRESET_DELTAS[name], now


def _fmt(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _parse_date(value: str) -> dt.datetime | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise OsmsgError(f"Unrecognized date: {value!r}. Use YYYY-MM-DD.")


def _split(value: str | None) -> list[str] | None:
    items: list[str] = [part.strip() for part in (value or "").split(",") if part.strip()]
    return items if items else None


def _parse_int(value: object, field: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise OsmsgError(f"{field} must be a whole number.") from exc
    if number < 1:
        raise OsmsgError(f"{field} must be at least 1.")
    return number


def build_config(form: dict[str, object], output_dir: str) -> RunConfig:
    """Map the form fields to a RunConfig, raising OsmsgError on invalid input."""
    formats = [name for name in FORMATS if form.get(name)]
    if not formats:
        raise OsmsgError("Pick at least one output format.")
    start = _parse_date(str(form.get("start", "")))
    if start is None:
        raise OsmsgError("Start date is required (YYYY-MM-DD).")
    return RunConfig(
        name=str(form.get("name") or "stats"),
        start_date=start,
        end_date=_parse_date(str(form.get("end", ""))),
        hashtags=_split(str(form.get("hashtags") or "")),
        additional_tags=_split(str(form.get("tags") or "")),
        tag_mode="all" if form.get("all_tags") else "none",
        summary=bool(form.get("summary")),
        formats=formats,
        workers=_parse_int(form.get("workers"), "Workers"),
        output_dir=Path(output_dir or "."),
    )


def _open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        import subprocess

        subprocess.run(["open", str(path)], check=False)
    else:
        import subprocess

        subprocess.run(["xdg-open", str(path)], check=False)


class _Redirector:
    def __init__(self, sink: queue.Queue) -> None:
        self.sink = sink

    def write(self, text: str) -> None:
        if text:
            self.sink.put(("log", text))

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class App:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, scrolledtext, ttk

        self._tk = tk
        self._ttk = ttk
        self._filedialog = filedialog
        self.events: queue.Queue = queue.Queue()
        self.out_dir = str(Path.home() / "osmsg")

        self.root = tk.Tk()
        self.root.title("osmsg")
        self.vars: dict[str, Any] = {}
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")

        rows = [
            ("Name", "name", "stats"),
            ("Start (YYYY-MM-DD)", "start", ""),
            ("End (blank = now)", "end", ""),
            ("Hashtags (comma-sep)", "hashtags", ""),
            ("Tags (comma-sep)", "tags", ""),
            ("Workers", "workers", str(os.cpu_count() or 4)),
        ]
        for i, (label, key, default) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=default)
            ttk.Entry(frame, textvariable=var, width=40).grid(row=i, column=1, columnspan=3, sticky="we", pady=2)
            self.vars[key] = var

        preset_frame = ttk.LabelFrame(frame, text="Quick range", padding=6)
        preset_frame.grid(row=6, column=0, columnspan=4, sticky="we", pady=6)
        for i, name in enumerate(PRESETS):
            ttk.Button(preset_frame, text=name, width=11, command=lambda n=name: self._apply_preset(n)).grid(
                row=0, column=i, padx=2
            )

        self.vars["all_tags"] = tk.BooleanVar()
        self.vars["summary"] = tk.BooleanVar()
        ttk.Checkbutton(frame, text="All tags", variable=self.vars["all_tags"]).grid(row=7, column=0, sticky="w")
        ttk.Checkbutton(frame, text="Daily summary", variable=self.vars["summary"]).grid(row=7, column=1, sticky="w")

        fmt_frame = ttk.LabelFrame(frame, text="Formats", padding=6)
        fmt_frame.grid(row=8, column=0, columnspan=4, sticky="we", pady=6)
        for i, name in enumerate(FORMATS):
            var = tk.BooleanVar(value=name in ("parquet", "csv"))
            ttk.Checkbutton(fmt_frame, text=name, variable=var).grid(row=0, column=i, padx=4)
            self.vars[name] = var

        self.out_label = ttk.Label(frame, text=f"Output: {self.out_dir}")
        self.out_label.grid(row=9, column=0, columnspan=3, sticky="w")
        ttk.Button(frame, text="Choose folder", command=self._choose_folder).grid(row=9, column=3, sticky="e")

        self.run_btn = ttk.Button(frame, text="Compute", command=self._on_run)
        self.run_btn.grid(row=10, column=0, pady=8, sticky="w")
        self.open_btn = ttk.Button(frame, text="Open output folder", command=lambda: _open_folder(Path(self.out_dir)))
        self.open_btn.grid(row=10, column=1, pady=8, sticky="w")
        self.spinner = ttk.Progressbar(frame, mode="indeterminate", length=160)
        self.spinner.grid(row=10, column=2, columnspan=2, pady=8, sticky="we")

        self.log = scrolledtext.ScrolledText(frame, width=70, height=14, state="disabled")
        self.log.grid(row=11, column=0, columnspan=4, sticky="nsew")

        ttk.Button(frame, text="About", command=self._show_about).grid(row=12, column=0, pady=(6, 0), sticky="w")
        ttk.Label(frame, text="A project of OSGeo Nepal").grid(row=12, column=1, columnspan=3, pady=(6, 0), sticky="e")
        self.root.after(120, self._drain)

    def _show_about(self) -> None:
        tk, ttk = self._tk, self._ttk
        win = tk.Toplevel(self.root)
        win.title("About osmsg")
        box = ttk.Frame(win, padding=16)
        box.grid(sticky="nsew")
        ttk.Label(box, text=f"osmsg {__version__}", font=("", 12, "bold")).grid(sticky="w")
        ttk.Label(box, text="OpenStreetMap Stats Generator").grid(sticky="w")
        ttk.Label(box, text="A project of OSGeo Nepal").grid(sticky="w", pady=(0, 10))
        for text, url in ABOUT_LINKS:
            link = ttk.Label(box, text=text, foreground="#1a73e8", cursor="hand2")
            link.grid(sticky="w", pady=2)
            link.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))
        ttk.Button(box, text="Close", command=win.destroy).grid(sticky="e", pady=(12, 0))

    def _apply_preset(self, name: str) -> None:
        start, end = preset_range(name)
        self.vars["start"].set(_fmt(start))
        self.vars["end"].set(_fmt(end))

    def _choose_folder(self) -> None:
        chosen = self._filedialog.askdirectory(initialdir=self.out_dir)
        if chosen:
            self.out_dir = chosen
            self.out_label.config(text=f"Output: {self.out_dir}")

    def _append(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_run(self) -> None:
        try:
            cfg = build_config({k: v.get() for k, v in self.vars.items()}, self.out_dir)
        except OsmsgError as exc:
            self._append(f"\n{exc}\n")
            return
        self.run_btn.config(state="disabled", text="Running...")
        self.spinner.start(12)
        self._append(f"\nComputing into {self.out_dir} ...\n")
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg: RunConfig) -> None:
        saved = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _Redirector(self.events)  # type: ignore[assignment]
        try:
            result = run(cfg)
            self.events.put(("done", f"Done. {result['rows']} rows. Files in {self.out_dir}"))
        except NoDataFoundError:
            self.events.put(("done", "No data found for that range."))
        except OsmsgError as exc:
            self.events.put(("done", f"Error: {exc}"))
        except Exception as exc:
            self.events.put(("done", f"Unexpected error: {type(exc).__name__}: {exc}"))
        finally:
            sys.stdout, sys.stderr = saved

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload)
                else:
                    self._append(f"\n{payload}\n")
                    self.spinner.stop()
                    self.run_btn.config(state="normal", text="Compute")
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def run(self) -> None:
        self.root.mainloop()


def launch() -> None:
    App().run()
