"""Frozen-app entry point for the windowed osmsg desktop UI."""

import multiprocessing

from osmsg.gui import launch

if __name__ == "__main__":
    multiprocessing.freeze_support()
    launch()
