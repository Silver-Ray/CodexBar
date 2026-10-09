"""Read taskbar controls off the Tk thread; never invoke or modify them."""

from __future__ import annotations

import ctypes
import math
import os
import threading
import time

from . import diagnostics

if os.name == "nt":
    # Import on the main thread; each probe initializes its own COM apartment.
    import comtypes
    import comtypes.client


Bounds = tuple[int, int, int, int]


class TaskbarMenuOpen(Exception):
    """UIA exposes a shell menu in place of the taskbar control tree."""


def _menu_only_snapshot(occupied: list[Bounds], bounds: Bounds, has_menu: bool) -> bool:
    return has_menu and not any(
        rect[0] < bounds[2] and rect[2] > bounds[0]
        and rect[1] < bounds[3] and rect[3] > bounds[1]
        for rect in occupied
    )


def map_bounds(rect: Bounds, source: Bounds, target: Bounds) -> Bounds:
    """Convert UIA coordinates to the caller's taskbar coordinate space.

    UIA is queried with a per-monitor-aware thread, whereas Tk can be DPI
    unaware. Mapping against the same taskbar also handles negative origins.
    Round outwards so a fractional pixel cannot expose part of a button.
    """
    sx = (target[2] - target[0]) / (source[2] - source[0])
    sy = (target[3] - target[1]) / (source[3] - source[1])
    return (
        math.floor(target[0] + (rect[0] - source[0]) * sx),
        math.floor(target[1] + (rect[1] - source[1]) * sy),
        math.ceil(target[0] + (rect[2] - source[0]) * sx),
        math.ceil(target[1] + (rect[3] - source[1]) * sy),
    )


def _read_controls(hwnd: int, bounds: Bounds) -> tuple[Bounds, ...]:
    uia = comtypes.client.GetModule("UIAutomationCore.dll")
    client = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    cache = client.CreateCacheRequest()
    for prop in (
        uia.UIA_BoundingRectanglePropertyId, uia.UIA_ControlTypePropertyId,
        uia.UIA_IsOffscreenPropertyId, uia.UIA_ProcessIdPropertyId,
    ):
        cache.AddProperty(prop)
    root = client.ElementFromHandleBuildCache(hwnd, cache)

    def rectangle(element) -> Bounds:
        rect = element.CachedBoundingRectangle
        return rect.left, rect.top, rect.right, rect.bottom

    source = rectangle(root)
    if source[2] <= source[0] or source[3] <= source[1]:
        raise ValueError("Taskbar has no accessible bounds")
    elements = root.FindAllBuildCache(
        uia.TreeScope_Descendants, client.ControlViewCondition, cache
    )
    # These contain empty space, so their rectangles cannot be obstacles.
    containers = {
        uia.UIA_PaneControlTypeId, uia.UIA_WindowControlTypeId,
        uia.UIA_GroupControlTypeId, uia.UIA_ToolBarControlTypeId,
        uia.UIA_ListControlTypeId, uia.UIA_MenuBarControlTypeId,
    }
    occupied = []
    menu_types = {uia.UIA_MenuControlTypeId, uia.UIA_MenuItemControlTypeId}
    has_menu = root.CachedControlType in menu_types and not root.CachedIsOffscreen
    for index in range(elements.Length):
        element = elements.GetElement(index)
        if (element.CachedIsOffscreen or element.CachedProcessId == os.getpid()
                or element.CachedControlType in containers):
            continue
        has_menu = has_menu or element.CachedControlType in menu_types
        rect = rectangle(element)
        if rect[2] > rect[0] and rect[3] > rect[1]:
            occupied.append(map_bounds(rect, source, bounds))
    if _menu_only_snapshot(occupied, bounds, has_menu):
        raise TaskbarMenuOpen()
    return tuple(occupied)


def read_controls(hwnd: int, bounds: Bounds) -> tuple[Bounds, ...]:
    """Run only on a worker; release COM objects before leaving its apartment."""
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    user32 = ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        return _read_controls(hwnd, bounds)
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)
        comtypes.CoUninitialize()


class TaskbarProbe:
    """One probe; retain known geometry only for a recognized shell menu tree."""

    def __init__(self):
        self._lock = threading.Lock()
        self._busy = False
        self._key = None
        self._started = float("-inf")
        self._result = None
        self._failed = False

    def get(self, hwnd: int, bounds: Bounds) -> tuple[Bounds, ...] | None:
        key = (hwnd, bounds)
        now = time.monotonic()
        with self._lock:
            result = self._result if self._key == key and now - self._started < 2 else None
            if not self._busy and (self._key != key or now - self._started >= 1):
                self._busy = True
                threading.Thread(
                    target=self._update, args=(key, now), daemon=True,
                    name="CodexBar-taskbar-layout",
                ).start()
            return result

    def _update(self, key, started: float) -> None:
        result = None
        menu_open = False
        try:
            result = read_controls(*key)
            self._failed = False
        except TaskbarMenuOpen:
            # Windows 11's clock menu replaces the tree with off-taskbar menu
            # items. This is not evidence that applications disappeared. Keep
            # the last geometry only for this identical taskbar; continue
            # probing so closing the menu replaces it without withdrawing Tk.
            menu_open = True
            self._failed = False
        except Exception as error:
            result = None
            if not self._failed:
                diagnostics.log_exception("taskbar_accessibility", error)
            self._failed = True
        finally:
            with self._lock:
                if menu_open and self._key == key:
                    result = self._result
                self._key = key
                self._started = started
                self._result = result
                self._busy = False
