"""Native notification-area entry when the taskbar has no safe widget slot."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os

from . import runtime


if os.name == "nt":
    _user = ctypes.WinDLL("user32", use_last_error=True)
    _shell = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class _GUID(ctypes.Structure):
        _fields_ = [("a", w.DWORD), ("b", w.WORD), ("c", w.WORD), ("d", w.BYTE * 8)]

    class _CLASS(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("proc", _PROC), ("extra", ctypes.c_int),
                    ("window_extra", ctypes.c_int), ("instance", w.HINSTANCE),
                    ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
                    ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

    class _DATA(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT),
                    ("flags", w.UINT), ("message", w.UINT), ("icon", w.HICON),
                    ("tip", w.WCHAR * 128), ("state", w.DWORD), ("mask", w.DWORD),
                    ("info", w.WCHAR * 256), ("version", w.UINT),
                    ("title", w.WCHAR * 64), ("info_flags", w.DWORD),
                    ("guid", _GUID), ("balloon_icon", w.HICON)]

    class _IDENTIFIER(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT), ("guid", _GUID)]

    _user.RegisterClassW.argtypes = [ctypes.POINTER(_CLASS)]
    _user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
    _user.CreateWindowExW.restype = w.HWND
    _user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    _user.DefWindowProcW.restype = ctypes.c_ssize_t
    _user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
    _user.LoadImageW.restype = w.HANDLE
    _user.DestroyIcon.argtypes = [w.HICON]
    _user.DestroyWindow.argtypes = [w.HWND]
    _user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
    _user.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    _user.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
    _kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    _kernel.GetModuleHandleW.restype = w.HINSTANCE
    _shell.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(_DATA)]
    _shell.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(_IDENTIFIER), ctypes.POINTER(w.RECT)]
    _shell.Shell_NotifyIconGetRect.restype = ctypes.c_long


class TrayEntry:
    """Owned by the Tk thread; callbacks enqueue events instead of calling Tk."""

    def __init__(self):
        self.events: list[str] = []
        self.active = False
        self._closed = False
        self._tip = "CodexBar"
        self._instance = _kernel.GetModuleHandleW(None)
        self._name = f"CodexBarTray_{os.getpid()}_{id(self)}"
        self._restart = _user.RegisterWindowMessageW("TaskbarCreated")
        self._proc = _PROC(self._message)
        definition = _CLASS(instance=self._instance, name=self._name, proc=self._proc)
        if not _user.RegisterClassW(ctypes.byref(definition)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = _user.CreateWindowExW(0, self._name, "", 0, 0, 0, 0, 0, None, None, self._instance, None)
        if not self.hwnd:
            _user.UnregisterClassW(self._name, self._instance)
            raise ctypes.WinError(ctypes.get_last_error())
        self._icon = _user.LoadImageW(None, str(runtime.resource_path("codexbar", "assets", "codexbar.ico")), 1, 0, 0, 0x10)
        if not self._icon:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def _data(self):
        return _DATA(size=ctypes.sizeof(_DATA), hwnd=self.hwnd, id=1,
                     flags=1 | 2 | 4, message=0x8001, icon=self._icon, tip=self._tip[:127])

    def _add(self):
        data = self._data()
        self.active = bool(_shell.Shell_NotifyIconW(0, ctypes.byref(data)))
        if self.active:
            data.version = 4
            _shell.Shell_NotifyIconW(4, ctypes.byref(data))

    def show(self, tip: str):
        changed = self._tip != tip
        self._tip = tip
        if not self.active:
            self._add()
        elif changed:
            _shell.Shell_NotifyIconW(1, ctypes.byref(self._data()))

    def hide(self):
        if self.active:
            _shell.Shell_NotifyIconW(2, ctypes.byref(self._data()))
        self.active = False
        self.events.clear()

    def _message(self, hwnd, message, wp, lp):
        if message == self._restart and self.active:
            self.active = False
            self._add()
        elif message == 0x8001:
            event = {0x406: "hover", 0x407: "leave", 0x400: "click",
                     0x401: "hover", 0x7B: "menu", 0x203: "settings"}.get(lp & 0xFFFF)
            if event and self.active:
                self.events.append(event)
            return 0
        return _user.DefWindowProcW(hwnd, message, wp, lp)

    def bounds(self):
        identifier = _IDENTIFIER(size=ctypes.sizeof(_IDENTIFIER), hwnd=self.hwnd, id=1)
        rect = w.RECT()
        if _shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect)) == 0:
            return rect.left, rect.top, rect.right, rect.bottom
        return None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.hide()
        _user.DestroyWindow(self.hwnd)
        if self._icon:
            _user.DestroyIcon(self._icon)
        _user.UnregisterClassW(self._name, self._instance)
