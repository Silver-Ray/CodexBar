"""Keep Explorer's attached input queue away from the application UI.

Only this thread's small canvas is parented to Explorer. Dialogs, networking,
menus, and dashboard activation remain on the independent controller thread.
Packets contain ordinary Python values; Tk objects never cross threads.
"""

from __future__ import annotations

import queue
import ctypes
import threading
import tkinter as tk
import tkinter.font as tkfont

from . import config, diagnostics, taskbar


def _enable_transparency(hwnd):
    """Enable child composition without asking Tk to rebuild its WM styles."""
    native = taskbar._USER32
    style = taskbar._GET_WINDOW_LONG_PTR(hwnd, taskbar.GWL_EXSTYLE)
    taskbar._SET_WINDOW_LONG_PTR(hwnd, taskbar.GWL_EXSTYLE, style | 0x80000)  # WS_EX_LAYERED
    native.SetLayeredWindowAttributes.argtypes = [
        taskbar.wintypes.HWND, taskbar.wintypes.DWORD, taskbar.wintypes.BYTE, taskbar.wintypes.DWORD,
    ]
    native.SetLayeredWindowAttributes.restype = taskbar.wintypes.BOOL
    rgb = tuple(int(config.MAGIC[i:i+2], 16) for i in (1, 3, 5))
    color = rgb[0] | rgb[1] << 8 | rgb[2] << 16
    if not native.SetLayeredWindowAttributes(hwnd, color, 255, 1):  # LWA_COLORKEY
        raise OSError(ctypes.windll.kernel32.GetLastError(), "Taskbar child composition failed")


class RecordedCanvas(tk.Canvas):
    """Draw locally for font/layout compatibility and copy immutable commands."""

    def __init__(self, *args, **kwargs):
        self.commands = []
        super().__init__(*args, **kwargs)

    def delete(self, *items):
        if "all" in items:
            self.commands.clear()
        return super().delete(*items)

    def _record(self, kind, args, options):
        copied = dict(options)
        font = copied.get("font")
        if isinstance(font, tkfont.Font):
            styles = []
            if font.cget("weight") == "bold":
                styles.append("bold")
            if font.cget("slant") == "italic":
                styles.append("italic")
            copied["font"] = (font.cget("family"), int(font.cget("size")), " ".join(styles) or "normal")
        # Polygon coordinates are sometimes passed as a mutable list.
        copied_args = tuple(tuple(value) if isinstance(value, list) else value for value in args)
        self.commands.append((kind, copied_args, copied))
        return getattr(super(), "create_" + kind)(*args, **options)

    def create_polygon(self, *args, **kwargs):
        return self._record("polygon", args, kwargs)

    def create_text(self, *args, **kwargs):
        return self._record("text", args, kwargs)


class TaskbarDisplay:
    def __init__(self):
        self.events = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._pending = {}
        self._stop = threading.Event()
        self._hwnd = 0
        self._thread = threading.Thread(target=self._run, name="CodexBar-taskbar-display", daemon=True)
        self._thread.start()

    @property
    def hwnd(self):
        return self._hwnd

    def _submit(self, **values):
        with self._lock:
            self._pending.update(values)

    def draw(self, commands):
        self._submit(draw=tuple((kind, tuple(args), dict(options)) for kind, args, options in commands))

    def place(self, info, bounds):
        self._submit(place=(info, bounds))

    def hide(self):
        self._submit(place=None)

    def close(self):
        self._stop.set()
        # Never wait indefinitely for the queue that Explorer can block.
        self._thread.join(timeout=0.5)

    def bounds(self):
        return taskbar.window_bounds(self._hwnd) if self._hwnd else None

    def _run(self):
        latest = {}
        while not self._stop.is_set():
            root = None
            try:
                root = tk.Tk()
                root.withdraw()
                root.title("CodexBar taskbar display")
                root.overrideredirect(True)
                root.attributes("-topmost", True)
                root.attributes("-toolwindow", True)
                root.configure(bg=config.MAGIC)
                canvas = tk.Canvas(root, bg=config.MAGIC, highlightthickness=0, cursor="hand2")
                canvas.pack(fill="both", expand=True)
                for sequence, action in (
                    ("<Button-1>", "click"), ("<Double-Button-1>", "settings"),
                    ("<Button-3>", "menu"), ("<Enter>", "hover"), ("<Leave>", "leave"),
                ):
                    canvas.bind(sequence, lambda event, name=action: self.events.put((name, event.x_root, event.y_root)))
                root.update_idletasks()
                self._hwnd = root.winfo_id()
                root.protocol("WM_DELETE_WINDOW", lambda: self.events.put(("close", 0, 0)))
                applied_place = None
                mapped = False

                def pump():
                    nonlocal applied_place, mapped
                    if self._stop.is_set() or not taskbar.window_exists(root.winfo_id()):
                        root.quit()
                        return
                    with self._lock:
                        changes, self._pending = self._pending, {}
                    latest.update(changes)
                    placement = latest.get("place")
                    if "draw" in changes or (placement and applied_place is None):
                        canvas.delete("all")
                        for kind, args, options in latest.get("draw", ()):
                            getattr(canvas, "create_" + kind)(*args, **options)
                    if placement:
                        info, bounds = placement
                        hidden = not taskbar._USER32.IsWindowVisible(taskbar._top_level_hwnd(root.winfo_id()))
                        if placement != applied_place or hidden or "place" in changes:
                            width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                            canvas.config(width=width, height=height)
                            if not mapped:
                                root.geometry(f"{width}x{height}")
                                root.deiconify()
                                root.update()
                                mapped = True
                            top = taskbar._top_level_hwnd(root.winfo_id())
                            attaching = taskbar._USER32.GetParent(top) != info.hwnd
                            if taskbar._place_taskbar_child(root.winfo_id(), info, bounds):
                                if attaching:
                                    _enable_transparency(top)
                                applied_place = placement
                    elif mapped:
                        # Tk's WM geometry/style updates assume a desktop
                        # top-level. After attaching, use native calls only.
                        taskbar._USER32.ShowWindow(taskbar._top_level_hwnd(root.winfo_id()), 0)
                        applied_place = None
                    root.after(25, pump)

                # Replay the previous frame after Explorer destroys the native
                # child. The controller and any open settings stay alive.
                if latest:
                    with self._lock:
                        self._pending = {**latest, **self._pending}
                pump()
                root.mainloop()
            except Exception as error:
                diagnostics.log_exception("taskbar_display", error)
                self._stop.wait(0.5)
            finally:
                self._hwnd = 0
                if root is not None:
                    try:
                        for timer in root.tk.splitlist(root.tk.call("after", "info")):
                            root.after_cancel(timer)
                        root.destroy()
                    except (tk.TclError, RuntimeError):
                        pass
                # Destroy all Tcl objects on their creating thread.
                canvas = root = None
