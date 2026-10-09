"""Application assembly entrypoint for CodexBar."""

from __future__ import annotations

import sys
import tkinter as tk

from .taskbar import acquire_single_instance, release_single_instance
from .ui import QuotaWidget
from . import diagnostics, dpi, web_dashboard


def main(argv: list[str] | None = None) -> None:
    """Start the single CodexBar taskbar widget instance."""

    argv = sys.argv[1:] if argv is None else argv
    if "--dashboard" in argv:
        web_dashboard.main()
        return

    mutex_handle = acquire_single_instance()
    if mutex_handle is None:
        diagnostics.log_event("single_instance_already_running")
        return
    try:
        dpi.enable_high_dpi()
        while True:
            root = tk.Tk()
            root.title("CodexBar")
            widget = QuotaWidget(root)
            root.mainloop()
            if getattr(widget, "_restart_requested", False) is not True:
                break
            # An Explorer process exit destroys native children. Tk can still
            # retain their logical widget paths, so rebuild instead of reusing
            # invalid HWNDs. Keep the single-instance mutex across recovery.
            widget.close()
    finally:
        release_single_instance(mutex_handle)
