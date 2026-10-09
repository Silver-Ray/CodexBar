"""Windows 任务栏集成与单实例保护。

本模块隔离所有 Win32 API：

- named mutex 保证程序只有一个实例；
- 查找主 ``Shell_TrayWnd`` / ``TrayNotifyWnd`` 定位通知区域；
- 额度条作为任务栏子窗口，详情作为独立、不激活的工具窗口。

UI 只需要传入窗口句柄和尺寸，不必理解 HWND、窗口样式或 Z-order。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os

from . import config, diagnostics, dpi
from .taskbar_accessibility import Bounds, TaskbarProbe


_IS_WINDOWS = os.name == "nt"
_USER32 = ctypes.windll.user32 if _IS_WINDOWS else None
_KERNEL32 = ctypes.windll.kernel32 if _IS_WINDOWS else None

ERROR_ALREADY_EXISTS = 183
GWL_EXSTYLE = -20
GWL_STYLE = -16
GWLP_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
HWND_TOPMOST = -1
HWND_TOP = 0
GW_HWNDNEXT = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
_CONTROL_PROBE = TaskbarProbe()


@dataclass(frozen=True)
class TaskbarInfo:
    """任务栏和通知区域的屏幕坐标。"""

    hwnd: int
    left: int
    top: int
    right: int
    bottom: int
    tray_left: int


_LAST_GOOD_TASKBAR: TaskbarInfo | None = None
_LAST_TASKBAR_LOG_STATE: str | None = None
_NATIVE_OBSTACLE_KEY = None
_NATIVE_OBSTACLE_WINDOWS: dict[int, tuple[int, str]] = {}


if _IS_WINDOWS:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
            ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
        ]


    _ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    _GET_WINDOW_LONG_PTR = getattr(
        _USER32, "GetWindowLongPtrW", _USER32.GetWindowLongW
    )
    _SET_WINDOW_LONG_PTR = getattr(
        _USER32, "SetWindowLongPtrW", _USER32.SetWindowLongW
    )
    _GET_WINDOW_LONG_PTR.argtypes = [wintypes.HWND, ctypes.c_int]
    _GET_WINDOW_LONG_PTR.restype = ctypes.c_ssize_t
    _SET_WINDOW_LONG_PTR.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _SET_WINDOW_LONG_PTR.restype = ctypes.c_ssize_t
    _USER32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _USER32.SetWindowPos.restype = wintypes.BOOL
    _USER32.GetTopWindow.argtypes = [wintypes.HWND]
    _USER32.GetTopWindow.restype = wintypes.HWND
    _USER32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _USER32.GetWindow.restype = wintypes.HWND
    _USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    _USER32.GetWindowRect.restype = wintypes.BOOL
    _USER32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _USER32.FindWindowW.restype = wintypes.HWND
    _USER32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    _USER32.FindWindowExW.restype = wintypes.HWND
    _USER32.GetParent.argtypes = [wintypes.HWND]
    _USER32.GetParent.restype = wintypes.HWND
    _USER32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    _USER32.SetParent.restype = wintypes.HWND
    _USER32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _USER32.ScreenToClient.restype = wintypes.BOOL
    _USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    _USER32.IsWindow.argtypes = [wintypes.HWND]
    _USER32.IsWindow.restype = wintypes.BOOL
    _USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _USER32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM_WINDOWS_PROC, wintypes.LPARAM]
    _USER32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
    _USER32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _USER32.MonitorFromWindow.restype = wintypes.HMONITOR
    _USER32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    _USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _USER32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    _USER32.ShowWindowAsync.restype = wintypes.BOOL
    _USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _USER32.SetForegroundWindow.restype = wintypes.BOOL

    _KERNEL32.CreateMutexW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
    ]
    _KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.SetLastError.argtypes = [wintypes.DWORD]
    _KERNEL32.SetLastError.restype = None
    _KERNEL32.GetLastError.argtypes = []
    _KERNEL32.GetLastError.restype = wintypes.DWORD


def acquire_single_instance(name: str = config.SINGLETON_MUTEX_NAME):
    """创建 named mutex；已存在时返回 ``None``，否则返回句柄。"""

    if not _IS_WINDOWS:
        return True
    _KERNEL32.SetLastError(0)
    handle = _KERNEL32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError()
    if _KERNEL32.GetLastError() == ERROR_ALREADY_EXISTS:
        _KERNEL32.CloseHandle(handle)
        return None
    return handle


def release_single_instance(handle) -> None:
    """关闭 mutex 句柄；进程异常退出时 Windows 也会自动释放。"""

    if _IS_WINDOWS and handle:
        _KERNEL32.CloseHandle(handle)


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(128)
    length = _USER32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value[:length]


def _window_rect(hwnd: int) -> RECT | None:
    if not hwnd:
        return None
    rectangle = RECT()
    if not _USER32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
        return None
    return rectangle


def _find_child_window(parent: int, class_name: str) -> int | None:
    hwnd = _USER32.FindWindowExW(parent, 0, class_name, None)
    return hwnd or None


def _valid_rect(rectangle) -> bool:
    return bool(
        rectangle
        and rectangle.right > rectangle.left
        and rectangle.bottom > rectangle.top
    )


def _taskbar_info(hwnd: int, rectangle, tray_rectangle=None) -> TaskbarInfo:
    tray_left = tray_rectangle.left if _valid_rect(tray_rectangle) else rectangle.right
    return TaskbarInfo(
        hwnd=hwnd,
        left=rectangle.left,
        top=rectangle.top,
        right=rectangle.right,
        bottom=rectangle.bottom,
        tray_left=tray_left,
    )


def _log_taskbar_state(state: str, info: TaskbarInfo) -> None:
    global _LAST_TASKBAR_LOG_STATE
    if _LAST_TASKBAR_LOG_STATE == state:
        return
    _LAST_TASKBAR_LOG_STATE = state
    diagnostics.log_event(
        state,
        hwnd=info.hwnd,
        left=info.left,
        top=info.top,
        right=info.right,
        bottom=info.bottom,
        tray_left=info.tray_left,
    )


def find_taskbars() -> list[tuple[int, RECT]]:
    """返回 Windows 主任务栏；副屏任务栏不能作为定位候选。"""

    if not _IS_WINDOWS:
        return []
    # Start/Search promote the taskbar into a shell window band which
    # EnumWindows omits. The window still exists and retains its geometry.
    hwnd = _USER32.FindWindowW("Shell_TrayWnd", None)
    rectangle = _window_rect(hwnd)
    return [(hwnd, rectangle)] if _valid_rect(rectangle) else []


def primary_taskbar() -> TaskbarInfo | None:
    """读取主任务栏以及隐藏图标/通知区域的左边界。"""

    global _LAST_GOOD_TASKBAR
    taskbars = find_taskbars()
    if not taskbars:
        _LAST_GOOD_TASKBAR = None
        return None

    # ``find_taskbars`` only returns the real primary Shell_TrayWnd. Looking at a
    # secondary tray here can make the widget jump monitors while Explorer redraws.
    hwnd, rectangle = taskbars[0]
    tray_hwnd = _find_child_window(hwnd, "TrayNotifyWnd")
    tray_rectangle = _window_rect(tray_hwnd) if tray_hwnd else None
    if _valid_rect(tray_rectangle):
        info = _taskbar_info(hwnd, rectangle, tray_rectangle)
        _LAST_GOOD_TASKBAR = info
        _log_taskbar_state("taskbar_cache_updated", info)
        return info

    if (_LAST_GOOD_TASKBAR and _LAST_GOOD_TASKBAR.hwnd == hwnd
            and _bounds(rectangle) == _info_bounds(_LAST_GOOD_TASKBAR)):
        _log_taskbar_state("taskbar_using_cached_info", _LAST_GOOD_TASKBAR)
        return _LAST_GOOD_TASKBAR

    # 首次启动或 Explorer 刚重建托盘时没有缓存，只回退到排序后的主任务栏。
    return _taskbar_info(hwnd, rectangle)


def _bounds(rectangle) -> Bounds:
    return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom


def primary_scale() -> float:
    info = primary_taskbar()
    return dpi.window_scale(info.hwnd) if info else 1.0


def _info_bounds(info: TaskbarInfo) -> Bounds:
    return info.left, info.top, info.right, info.bottom


def _work_area(hwnd: int) -> Bounds | None:
    monitor = _USER32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if monitor and _USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return _bounds(info.rcWork)
    return None


def _child_windows(parent: int) -> list[int]:
    """Find children even while shell window bands omit them from enumeration."""
    pending = [parent]
    seen = set()
    children = []
    while pending and len(seen) < 4096:
        ancestor = pending.pop()
        previous = 0
        while len(seen) < 4096:
            child = _USER32.FindWindowExW(ancestor, previous, None, None)
            if not child or child in seen:
                break
            seen.add(child)
            children.append(child)
            pending.append(child)
            previous = child
    return children


def _native_obstacles(info: TaskbarInfo) -> list[Bounds]:
    """Include custom-drawn taskbar children (e.g. Traffic Monitor) and popups."""
    global _NATIVE_OBSTACLE_KEY
    key = (info.hwnd, _info_bounds(info))
    if _NATIVE_OBSTACLE_KEY != key:
        _NATIVE_OBSTACLE_WINDOWS.clear()
        _NATIVE_OBSTACLE_KEY = key
    containers = {
        "ReBarWindow32", "MSTaskSwWClass", "MSTaskListWClass", "TrayNotifyWnd",
        "Windows.UI.Composition.DesktopWindowContentBridge",
        "Windows.UI.Core.CoreWindow",
        "Windows.UI.Input.InputSite.WindowClass", "TrayDummySearchControl",
    }
    occupied = []
    discovered = {}
    shell_enumerated = False

    def visit(hwnd, *, child: bool, identity=None, include_hidden=False):
        if hwnd == info.hwnd or (not include_hidden and not _USER32.IsWindowVisible(hwnd)):
            return
        process_id = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == os.getpid():
            return
        class_name = _window_class(hwnd)
        current_identity = (process_id.value, class_name)
        if identity is not None and current_identity != identity:
            return  # A destroyed HWND may have been reused by another window.
        if child and class_name in containers:
            return
        rect = _window_rect(hwnd)
        if not _valid_rect(rect):
            return
        if rect.left >= info.right or rect.right <= info.left:
            return
        if rect.top >= info.bottom or rect.bottom <= info.top:
            return
        # Desktop windows and transient menus extending above the taskbar do
        # not consume a taskbar slot. Embedded children always do.
        if child or (rect.top >= info.top and rect.bottom <= info.bottom):
            occupied.append(_bounds(rect))
            if not child:
                discovered[hwnd] = current_identity

    @_ENUM_WINDOWS_PROC
    def popup_proc(hwnd, _lparam):
        nonlocal shell_enumerated
        if hwnd == info.hwnd:
            shell_enumerated = True
        visit(hwnd, child=False)
        return True

    for child in _child_windows(info.hwnd):
        visit(child, child=True)
    _USER32.EnumWindows(popup_proc, 0)
    # Shell promotion also removes Traffic Monitor's top-level window from
    # EnumWindows. Revalidate known handles against live visibility/identity/
    # bounds, rather than treating their missing enumeration as free space.
    for hwnd, identity in tuple(_NATIVE_OBSTACLE_WINDOWS.items()):
        if hwnd not in discovered:
            visit(hwnd, child=False, identity=identity, include_hidden=not shell_enumerated)
    _NATIVE_OBSTACLE_WINDOWS.clear()
    _NATIVE_OBSTACLE_WINDOWS.update(discovered)
    return occupied


def taskbar_bounds(
    info: TaskbarInfo, width: int, height: int,
    occupied: tuple[Bounds, ...] | list[Bounds] | None,
    margin: int = 4,
) -> Bounds | None:
    """Use only an actual taskbar gap; never move the persistent bar above it."""
    horizontal = info.right - info.left > info.bottom - info.top
    bar_height = info.bottom - info.top
    if horizontal and occupied is not None and bar_height >= config.H_MIN + 2 * margin:
        actual_height = min(height, bar_height - 2 * margin)
        left, right = info.left + margin, min(info.tray_left, info.right) - margin
        intervals = sorted(
            (max(left, rect[0] - margin), min(right, rect[2] + margin))
            for rect in occupied
            if rect[1] < info.bottom and rect[3] > info.top
            and rect[0] < right and rect[2] > left
        )
        gaps = []
        cursor = left
        for start, end in intervals:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        gaps.append((cursor, right))
        for start, end in reversed(gaps):
            if end - start >= width:
                y = info.top + (bar_height - actual_height) // 2
                return end - width, y, end, y + actual_height
    return None


def popup_bounds(
    info: TaskbarInfo, width: int, height: int,
    occupied: tuple[Bounds, ...] | list[Bounds] | None,
    work: Bounds, margin: int = 4,
) -> Bounds:
    """Position a user-requested hover preview on the desktop side of the bar."""
    horizontal = info.right - info.left > info.bottom - info.top
    # Keep the configured dimensions and all information whenever the screen
    # can fit them. Clamp against this monitor, including negative origins.
    width = min(width, max(1, work[2] - work[0] - 2 * margin))
    height = min(height, max(1, work[3] - work[1] - 2 * margin))
    if horizontal:
        x = info.tray_left - width - margin
        y = info.bottom + margin if info.top < work[1] else info.top - height - margin
    else:
        x = info.right + margin if info.left < work[0] else info.left - width - margin
        y = work[3] - height - margin
    x = max(work[0] + margin, min(x, work[2] - width - margin))
    y = max(work[1] + margin, min(y, work[3] - height - margin))
    return x, y, x + width, y + height


def _top_level_hwnd(hwnd: int) -> int:
    current = hwnd
    while current:
        # GetAncestor(GA_ROOT) 在窗口已有 owner 后可能返回任务栏 owner。
        # Tk 自己的外层窗口类名稳定为 TkTopLevel，因此在这里显式停止。
        if _window_class(current) == "TkTopLevel":
            return current
        parent = _USER32.GetParent(current)
        if not parent:
            break
        current = parent
    return hwnd


def _configure_popup(hwnd: int) -> tuple[int, bool]:
    top = _top_level_hwnd(hwnd)
    extended_style = _GET_WINDOW_LONG_PTR(top, GWL_EXSTYLE)
    _SET_WINDOW_LONG_PTR(
        top,
        GWL_EXSTYLE,
        extended_style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
    )
    current_owner = _GET_WINDOW_LONG_PTR(top, GWLP_HWNDPARENT)
    owner_changed = bool(current_owner)
    if owner_changed:
        # Search, clock menus and ShowOwnedPopups can hide Explorer's owned
        # windows. The widget must not share Explorer's visibility lifecycle.
        _SET_WINDOW_LONG_PTR(top, GWLP_HWNDPARENT, 0)
    return top, owner_changed or not bool(extended_style & WS_EX_TOPMOST)


def _place_popup(hwnd: int, info: TaskbarInfo, bounds: Bounds) -> tuple[int, int]:
    x, y, right, bottom = bounds
    popup, needs_topmost = _configure_popup(hwnd)
    z_order_lost = _is_above_in_z_order(popup, info.hwnd) is False
    hidden = not _USER32.IsWindowVisible(popup)
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
    insert_after = HWND_TOPMOST
    if not needs_topmost and not z_order_lost and not hidden:
        # Preserve the stacking order of system flyouts that were opened later.
        flags |= SWP_NOZORDER
    if z_order_lost:
        diagnostics.log_event("taskbar_z_order_recovered", hwnd=popup, taskbar_hwnd=info.hwnd)
    _USER32.SetWindowPos(
        popup, wintypes.HWND(insert_after), x, y, right - x, bottom - y, flags
    )
    return right - x, bottom - y


def _configure_taskbar_child(hwnd: int, parent: int) -> int | None:
    """Share the shell's window band without sharing its owned-popup lifecycle."""
    child = _top_level_hwnd(hwnd)
    style = _GET_WINDOW_LONG_PTR(child, GWL_STYLE)
    extended = _GET_WINDOW_LONG_PTR(child, GWL_EXSTYLE)
    _SET_WINDOW_LONG_PTR(child, GWL_EXSTYLE, extended | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    if _USER32.GetParent(child) != parent:
        # Tk withdraw/deiconify can remove the native topmost band. Establish
        # it before SetParent, or attaching to a promoted shell fails with
        # ERROR_INVALID_PARAMETER even though the taskbar HWND is valid.
        _SET_WINDOW_LONG_PTR(child, GWL_STYLE, (style & ~WS_CHILD) | WS_POPUP)
        _SET_WINDOW_LONG_PTR(child, GWLP_HWNDPARENT, 0)
        _USER32.SetWindowPos(child, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOACTIVATE)
        _SET_WINDOW_LONG_PTR(child, GWL_STYLE, (style & ~WS_POPUP) | WS_CHILD)
        _USER32.SetParent(child, parent)
    else:
        _SET_WINDOW_LONG_PTR(child, GWL_STYLE, (style & ~WS_POPUP) | WS_CHILD)
    return child if _USER32.GetParent(child) == parent else None


def _client_coordinates(parent: int, x: int, y: int) -> tuple[int, int] | None:
    point = wintypes.POINT(x, y)
    if not _USER32.ScreenToClient(parent, ctypes.byref(point)):
        return None
    return point.x, point.y


def _place_taskbar_child(hwnd: int, info: TaskbarInfo, bounds: Bounds) -> tuple[int, int] | None:
    child = _configure_taskbar_child(hwnd, info.hwnd)
    origin = _client_coordinates(info.hwnd, bounds[0], bounds[1])
    if child is None or origin is None:
        return None
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    # Child coordinates are relative to the taskbar. Raise only within its
    # siblings; a desktop topmost window cannot cross the shell's input band.
    if not _USER32.SetWindowPos(
        child, wintypes.HWND(HWND_TOP), *origin, width, height,
        SWP_NOACTIVATE | SWP_SHOWWINDOW,
    ):
        return None
    return width, height


def position_hover_popup(
    hwnd: int, anchor: Bounds, width: int, height: int, margin: int = 4,
) -> tuple[int, int] | None:
    if not _IS_WINDOWS:
        return None
    info = primary_taskbar()
    work = _work_area(info.hwnd) if info else None
    if info is None or work is None:
        return None
    anchor_info = TaskbarInfo(
        info.hwnd, info.left, info.top, info.right, info.bottom, anchor[2]
    )
    return _place_popup(hwnd, info, popup_bounds(anchor_info, width, height, None, work, margin))


def _is_above_in_z_order(hwnd: int, reference_hwnd: int) -> bool | None:
    """Compare two top-level windows without changing focus or Z-order."""

    if not hwnd or not reference_hwnd or hwnd == reference_hwnd:
        return None
    current = _USER32.GetTopWindow(None)
    seen: set[int] = set()
    while current:
        current_value = int(current)
        if current_value in seen:
            break
        seen.add(current_value)
        if current_value == hwnd:
            return True
        if current_value == reference_hwnd:
            return False
        current = _USER32.GetWindow(current, GW_HWNDNEXT)
    return None


def position_taskbar_popup(
    hwnd: int,
    width: int,
    height: int,
    margin: int = 4,
    compact_width: int | None = None,
) -> tuple[int, int] | None:
    """优先完整显示；空间不足时折叠，无空位时由 UI 提供托盘入口。"""

    if not _IS_WINDOWS:
        return None
    info = primary_taskbar()
    if not info:
        _USER32.ShowWindow(_top_level_hwnd(hwnd), 0)  # SW_HIDE during Explorer restart
        return None
    placement = taskbar_placement(width, height, margin, compact_width, info=info)
    return _place_taskbar_child(hwnd, *placement) if placement else None


def taskbar_placement(width: int, height: int, margin: int = 4, compact_width: int | None = None, *, info=None):
    """Read geometry only; applying it belongs to the display's owning thread."""
    info = info or primary_taskbar()
    if info is None:
        return None
    controls = _CONTROL_PROBE.get(info.hwnd, _info_bounds(info))
    # An incomplete accessibility tree containing only the tray does not prove
    # that the application area is empty. Until a full read succeeds, stay out.
    occupied = None
    if controls and any(
        rect[0] < info.tray_left and rect[2] > info.left
        and rect[1] < info.bottom and rect[3] > info.top
        for rect in controls
    ):
        occupied = [*controls, *_native_obstacles(info)]
    bounds = taskbar_bounds(info, width, height, occupied, margin)
    if bounds is None and compact_width is not None:
        bounds = taskbar_bounds(info, min(width, compact_width), height, occupied, margin)
    if bounds is None:
        return None
    return info, bounds


def window_bounds(hwnd: int) -> Bounds | None:
    rectangle = _window_rect(_top_level_hwnd(hwnd)) if _IS_WINDOWS else None
    return _bounds(rectangle) if _valid_rect(rectangle) else None


def window_exists(hwnd: int) -> bool:
    """Explorer exiting can destroy an embedded window without deleting Tk's path."""
    return bool(_USER32.IsWindow(hwnd)) if _IS_WINDOWS else True


def double_click_interval() -> int:
    return int(_USER32.GetDoubleClickTime()) if _IS_WINDOWS else 500


def activate_process_window(process_id: int) -> bool:
    """A user click restores an already-open dashboard rather than duplicating it."""
    if not _IS_WINDOWS:
        return False
    found = []

    @_ENUM_WINDOWS_PROC
    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process_id and _USER32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    _USER32.EnumWindows(visit, 0)
    if found:
        # The separate dashboard may be busy. Never wait for its UI thread.
        _USER32.ShowWindowAsync(found[0], 9)  # SW_RESTORE
        return bool(_USER32.SetForegroundWindow(found[0]))
    return False
