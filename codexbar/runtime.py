"""运行时路径与打包辅助。

这个模块把“源码运行”和“PyInstaller exe 运行”的差异收在一起。
其它模块只需要询问这里：资源在哪里、打开 dashboard 应该用什么命令。
"""

from __future__ import annotations

from pathlib import Path
import sys


def is_frozen() -> bool:
    """Return whether CodexBar is running from a bundled executable."""

    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    """Return the project root when running from source."""

    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Return the root that contains bundled read-only resources.

    PyInstaller extracts data files under ``sys._MEIPASS`` in one-file mode and
    points there in one-folder mode too. Source mode falls back to the repo root.
    """

    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return source_root()


def resource_path(*parts: str) -> Path:
    """Build an absolute path to a bundled or source resource."""

    return bundle_root().joinpath(*parts)


def dashboard_command() -> tuple[list[str], str]:
    """Return the command and cwd used to open the token dashboard.

    In source mode the dashboard remains a separate ``.pyw`` entry. In bundled
    mode there is only one exe, so we re-enter it with ``--dashboard`` and let
    ``app.main`` dispatch before acquiring the taskbar singleton mutex.
    """

    if is_frozen():
        exe = Path(sys.executable)
        return [str(exe), "--dashboard"], str(exe.parent)

    root = source_root()
    return [sys.executable, str(root / "codexbar_dashboard.pyw")], str(root)
