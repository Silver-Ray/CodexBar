"""Native DPI setup and conversion from 96-DPI settings to device pixels."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


_USER32 = ctypes.windll.user32 if os.name == "nt" else None


def enable_high_dpi() -> None:
    """Call before Tk creates any windows (the EXE also has a DPI manifest)."""
    if _USER32 is None:
        return
    try:
        set_context = _USER32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = wintypes.BOOL
        # The first can fail when the executable's manifest already set it.
        if set_context(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
        if set_context(ctypes.c_void_p(-3)):  # Windows 10 before PMv2
            return
    except (AttributeError, OSError):
        pass
    try:
        set_awareness = ctypes.windll.shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        set_awareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE; no-op if already set
    except (AttributeError, OSError):
        _USER32.SetProcessDPIAware()


def window_scale(hwnd: int) -> float:
    if _USER32 is not None and hwnd:
        try:
            get_dpi = _USER32.GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            value = get_dpi(hwnd)
            if value:
                return value / 96
        except (AttributeError, OSError):
            pass
    return 1.0


def pixels(value: float, scale: float) -> int:
    return round(value * scale)


def font_pixels(points: float, scale: float) -> int:
    # Negative Tk font sizes specify device pixels, independent of Tk's
    # process-wide point scale. This avoids double scaling on mixed-DPI PCs.
    return -max(1, round(points * 96 / 72 * scale))
