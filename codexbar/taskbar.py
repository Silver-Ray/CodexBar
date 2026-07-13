"""Windows 任务栏集成与单实例保护。

本模块隔离所有 Win32 API：

- named mutex 保证程序只有一个实例；
- 查找主 ``Shell_TrayWnd`` / ``TrayNotifyWnd`` 定位通知区域；
- 将 Tk 顶层窗口设置为任务栏的 owned popup，使点击任务栏后仍位于上方。

UI 只需要传入窗口句柄和尺寸，不必理解 HWND、窗口样式或 Z-order。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os

from . import config, diagnostics


_IS_WINDOWS = os.name == "nt"
_USER32 = ctypes.windll.user32 if _IS_WINDOWS else None
_KERNEL32 = ctypes.windll.kernel32 if _IS_WINDOWS else None

ERROR_ALREADY_EXISTS = 183
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
HWND_TOP = 0
GW_HWNDNEXT = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


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


if _IS_WINDOWS:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
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
    taskbars: list[tuple[int, RECT]] = []

    @_ENUM_WINDOWS_PROC
    def enum_proc(hwnd, _lparam):
        class_name = _window_class(hwnd)
        if class_name == "Shell_TrayWnd":
            rectangle = _window_rect(hwnd)
            if _valid_rect(rectangle):
                taskbars.append((hwnd, rectangle))
        return True

    _USER32.EnumWindows(enum_proc, 0)
    return taskbars


def primary_taskbar() -> TaskbarInfo | None:
    """读取主任务栏以及隐藏图标/通知区域的左边界。"""

    global _LAST_GOOD_TASKBAR
    taskbars = find_taskbars()
    if not taskbars:
        if _LAST_GOOD_TASKBAR:
            _log_taskbar_state("taskbar_using_cached_info", _LAST_GOOD_TASKBAR)
            return _LAST_GOOD_TASKBAR
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

    if _LAST_GOOD_TASKBAR:
        _log_taskbar_state("taskbar_using_cached_info", _LAST_GOOD_TASKBAR)
        return _LAST_GOOD_TASKBAR

    # 首次启动或 Explorer 刚重建托盘时没有缓存，只回退到排序后的主任务栏。
    return _taskbar_info(hwnd, rectangle)


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


def _configure_owned_popup(hwnd: int, taskbar_hwnd: int) -> tuple[int, bool]:
    top = _top_level_hwnd(hwnd)
    extended_style = _GET_WINDOW_LONG_PTR(top, GWL_EXSTYLE)
    _SET_WINDOW_LONG_PTR(
        top,
        GWL_EXSTYLE,
        extended_style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
    )
    current_owner = _GET_WINDOW_LONG_PTR(top, GWLP_HWNDPARENT)
    owner_changed = current_owner != taskbar_hwnd
    if owner_changed:
        # Owned popup 始终位于任务栏 owner 上方，但不会嵌入 Win11 的 XAML 子窗口层。
        _SET_WINDOW_LONG_PTR(top, GWLP_HWNDPARENT, taskbar_hwnd)
    return top, owner_changed


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
) -> tuple[int, int] | None:
    """把窗口放到通知区域左侧，返回实际 ``(width, height)``。"""

    if not _IS_WINDOWS:
        return None
    info = primary_taskbar()
    if not info:
        return None

    taskbar_height = info.bottom - info.top
    actual_height = int(
        config.clamp(min(height, taskbar_height - 8), config.H_MIN, config.H_MAX)
    )
    x = info.tray_left - width - margin
    y = info.top + max(0, (taskbar_height - actual_height) // 2)
    popup, owner_changed = _configure_owned_popup(hwnd, info.hwnd)
    above_taskbar = _is_above_in_z_order(popup, info.hwnd)
    z_order_lost = above_taskbar is False
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
    insert_after = HWND_TOP
    if owner_changed or z_order_lost:
        # 截图遮罩退出时 Explorer 可能重新提升任务栏。只有确认组件已经落到
        # 任务栏下方才恢复 TOPMOST，避免周期定位盖住系统托盘弹出面板。
        insert_after = HWND_TOPMOST
        if z_order_lost and not owner_changed:
            diagnostics.log_event(
                "taskbar_z_order_recovered",
                hwnd=popup,
                taskbar_hwnd=info.hwnd,
            )
    else:
        # 后续只更新坐标，不覆盖已经打开的系统托盘弹出面板。
        flags |= SWP_NOZORDER
    _USER32.SetWindowPos(
        popup,
        wintypes.HWND(insert_after),
        int(x),
        int(y),
        int(width),
        int(actual_height),
        flags,
    )
    return width, actual_height
