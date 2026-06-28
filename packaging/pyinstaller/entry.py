"""Frozen-app entry point: PyInstaller needs a real script to launch the Typer CLI."""

import multiprocessing

from osmsg.cli import app

if __name__ == "__main__":
    multiprocessing.freeze_support()  # ProcessPoolExecutor workers would recursively relaunch the exe otherwise
    app()
