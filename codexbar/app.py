"""Application assembly entrypoint for CodexBar."""

from __future__ import annotations

import sys
import tkinter as tk

from .taskbar import acquire_single_instance, release_single_instance
from .ui import QuotaWidget
from . import web_dashboard


def main(argv: list[str] | None = None) -> None:
    """Start the single CodexBar taskbar widget instance."""

    argv = sys.argv[1:] if argv is None else argv
    if "--dashboard" in argv:
        web_dashboard.main()
        return

    mutex_handle = acquire_single_instance()
    if mutex_handle is None:
        return
    try:
        root = tk.Tk()
        root.title("CodexBar")
        QuotaWidget(root)
        root.mainloop()
    finally:
        release_single_instance(mutex_handle)
