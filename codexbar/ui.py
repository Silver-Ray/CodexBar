"""Tk 任务栏小组件与设置界面。

这个模块只关心“怎样显示”和“怎样响应鼠标”。额度数据来自 ``quota_api``，
凭据清理由 ``credentials`` 完成，窗口位置交给 ``taskbar``。后台线程不能直接
修改 Tk 控件，所以网络请求完成后必须通过 ``root.after`` 回到主线程。
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, font as tkfont, messagebox
from types import SimpleNamespace

from . import __version__
from . import config, diagnostics, dpi, pricing, runtime, taskbar, token_usage, tray
from .credentials import (
    AuthRequiredError,
    CREDENTIAL_LOCK,
    ReloginRequiredError,
    clear_credentials,
    delete_account,
    has_credentials,
    list_accounts,
    set_active_account,
)
from .quota_api import QuotaData, error_status, fetch_quota, format_reset_time
from .taskbar_display import RecordedCanvas, TaskbarDisplay


COLORS = {
    "good": config.DEFAULTS["color_good"],
    "warn": config.DEFAULTS["color_warn"],
    "low": config.DEFAULTS["color_low"],
}


def set_colors(settings: config.AppConfig) -> None:
    """将用户颜色设置应用到额度数字。"""

    COLORS["good"] = settings["color_good"]
    COLORS["warn"] = settings["color_warn"]
    COLORS["low"] = settings["color_low"]


def quota_color(percent: float | None) -> str:
    """按剩余额度返回充足、偏少或告急颜色。"""

    if percent is None:
        return config.SUB
    if percent >= 50:
        return COLORS["good"]
    if percent >= 20:
        return COLORS["warn"]
    return COLORS["low"]


def contrast_foreground(background: str) -> str:
    """根据背景相对亮度选择深色或浅色普通文字。"""

    color = config.valid_widget_background(background)
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return config.BG if luminance > 0.45 else config.FG


def round_rect_points(x1: int, y1: int, x2: int, y2: int, radius: int) -> list[int]:
    """生成 Tk Canvas 平滑圆角矩形所需的控制点。"""

    return [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]


def _styled_scale(
    parent: tk.Misc,
    variable: tk.IntVar,
    lower: int,
    upper: int,
    length: int,
    command,
    dpi_scale: float = 1.0,
) -> tk.Scale:
    """创建深色设置面板里默认可见的 Tk 滑块。"""

    return tk.Scale(
        parent,
        from_=lower,
        to=upper,
        resolution=1,
        orient="horizontal",
        variable=variable,
        showvalue=False,
        bg=config.ACCENT,
        fg=config.FG,
        troughcolor=config.BAR_BG,
        highlightthickness=0,
        highlightbackground=config.CARD,
        highlightcolor=config.CARD,
        activebackground="#6dd5f5",
        bd=0,
        sliderrelief="flat",
        length=dpi.pixels(length, dpi_scale),
        width=dpi.pixels(15, dpi_scale),
        sliderlength=dpi.pixels(30, dpi_scale),
        command=command,
    )


def settings_layout_metrics() -> dict[str, int]:
    """设置窗口关键坐标，集中约束避免控件和底部按钮重叠。"""

    return {
        "width": 360,
        "height": 642,
        "color_controls_bottom_y": 554,
        "action_divider_y": 588,
        "action_button_y": 622,
        "close_font_size": 15,
    }


def scale_canvas_layout(canvas: tk.Canvas, scale: float) -> None:
    """Scale dialog coordinates and embedded widths, leaving fonts in pixels."""
    window_sizes = []
    for item in canvas.find_all():
        if canvas.type(item) == "window":
            for dimension in ("width", "height"):
                value = float(canvas.itemcget(item, dimension))
                if value:
                    window_sizes.append((item, dimension, value))
    canvas.scale("all", 0, 0, scale, scale)
    # Some Tk versions already scale embedded window dimensions. Always base
    # the final size on the original geometry so it is never scaled twice.
    for item, dimension, value in window_sizes:
        canvas.itemconfigure(item, **{dimension: dpi.pixels(value, scale)})


class QuotaWidget:
    """显示 5h/每周额度并负责定时刷新的任务栏窗口。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = config.load_config()
        set_colors(self.settings)
        self._refresh_after_id: str | None = None
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._refresh_generation = 0
        self._closed = False
        self._restart_requested = False
        self._status_code: str | None = None
        self._last_success_at: float | None = None
        self._stale_status: str | None = None
        self._quota_account_id: str | None = None
        self._context_menu_open = False
        self._context_menu_generation = 0
        self._collapsed = False
        self._tray_entry = None
        self._tray_mode = False
        self._hover_window = None
        self._hover_show_id = None
        self._hover_hide_id = None
        self._hover_visible = False
        self._dashboard_click_id = None
        self._taskbar_display = None
        self.data = self._empty_data()

        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-toolwindow", True)
            root.attributes("-transparentcolor", config.MAGIC)
        except tk.TclError:
            pass
        root.configure(bg=config.MAGIC)

        self._dpi_scale = taskbar.primary_scale()
        self.W = self._px(self.settings["width"])
        self.desired_H = self._px(self.settings["height"])
        self.H = self.desired_H
        root.geometry(f"{self.W}x{self.H}")

        self.canvas = RecordedCanvas(root, bg=config.MAGIC, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_ui()
        self._bind_mouse()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.update_idletasks()
        if os.name == "nt":
            self._taskbar_display = TaskbarDisplay()
            self._redraw()
        self._position_at_taskbar()

        self.refresh_async()
        self._tick()
        self._poll_tray()
        self._poll_display()

    def _empty_data(self) -> dict:
        return {
            "plan": "",
            "rows": {"h": {}, "w": {}},
            "usage": {"tokens": "--", "cost": "$--"},
        }

    def _position_at_taskbar(self) -> None:
        if self._context_menu_open:
            return
        old_scale = getattr(self, "_dpi_scale", 1.0)
        self._dpi_scale = taskbar.primary_scale()
        settings = getattr(self, "settings", {})
        target_height = self._px(settings.get(
            "height", getattr(self, "desired_H", self.H) / old_scale
        ))
        target_width = self._px(settings.get("width", self.W / old_scale))
        self.desired_H = target_height
        display = getattr(self, "_taskbar_display", None)
        if display:
            placement = taskbar.taskbar_placement(
                target_width, target_height, margin=self._px(4), compact_width=self._px(96),
            )
            if placement:
                info, bounds = placement
                display.place(info, bounds)
                positioned = (bounds[2] - bounds[0], bounds[3] - bounds[1])
            else:
                display.hide()
                positioned = None
        else:
            positioned = taskbar.position_taskbar_popup(
                self.root.winfo_id(), target_width, target_height, margin=self._px(4),
                compact_width=self._px(96),
            )
        if not positioned:
            self.root.withdraw()
            self._tray_mode = True
            self._collapsed = True
            if old_scale != self._dpi_scale:
                self._build_ui()
            self._update_tray()
            if getattr(self, "_hover_visible", False):
                self._position_hover()
            return
        was_tray = getattr(self, "_tray_mode", False)
        self._tray_mode = False
        entry = getattr(self, "_tray_entry", None)
        if entry:
            entry.hide()
        if not display and self.root.state() == "withdrawn":
            self.root.deiconify()
            # Tk may restore its saved geometry; apply physical bounds again.
            taskbar.position_taskbar_popup(
                self.root.winfo_id(), target_width, target_height, margin=self._px(4),
                compact_width=self._px(96),
            )
        collapsed = positioned[0] < target_width
        changed = collapsed != getattr(self, "_collapsed", False)
        self._collapsed = collapsed
        if was_tray or not collapsed:
            self._hide_hover()
        if positioned != (self.W, self.H) or old_scale != self._dpi_scale or changed:
            self.W, self.H = positioned
            self._build_ui()
        if getattr(self, "_hover_visible", False):
            self._position_hover()

    def _px(self, value: float) -> int:
        return dpi.pixels(value, getattr(self, "_dpi_scale", 1.0))

    def _scale(self) -> float:
        # 字体按用户设置的目标高度计算，避免任务栏临时返回较小高度时文字跳小。
        target_height = getattr(self, "desired_H", self.H)
        font_scale = config.clamp(
            int(self.settings.get("font_scale_percent", 100)),
            config.FONT_SCALE_MIN,
            config.FONT_SCALE_MAX,
        ) / 100
        display_scale = getattr(self, "_dpi_scale", 1.0)
        full_width = self._px(self.settings.get("width", self.W / display_scale))
        base_scale = min(full_width / config.BASE_W, target_height / config.BASE_H) / display_scale
        return config.clamp(base_scale * font_scale, 0.75, 1.35)

    def _preview_font_scale(self, value: object) -> None:
        """临时预览任务栏字体百分比，不写入配置文件。"""

        self.settings["font_scale_percent"] = int(
            config.clamp(
                int(float(value)), config.FONT_SCALE_MIN, config.FONT_SCALE_MAX
            )
        )
        self._build_ui()

    def _preview_width(self, value: object) -> None:
        """临时预览任务栏组件宽度，不写入配置文件。"""

        self.settings["width"] = int(
            config.clamp(int(float(value)), config.W_MIN, config.W_MAX)
        )
        self._apply_widget_geometry_from_settings()

    def _preview_color(self, key: str, value: object) -> None:
        """临时预览额度数字颜色，不写入配置文件。"""

        self.settings[key] = config.valid_hex(value, config.DEFAULTS[key])
        set_colors(self.settings)
        self._redraw()

    def _preview_widget_background(self, value: object) -> None:
        """临时预览任务栏组件背景，不写入配置文件。"""

        self.settings["widget_background"] = config.valid_widget_background(value)
        self._redraw()

    def _restore_settings_snapshot(self, snapshot: dict[str, object]) -> None:
        """取消设置时恢复打开窗口前的任务栏外观。"""

        self.settings.update(snapshot)
        set_colors(self.settings)
        self._apply_widget_geometry_from_settings()

    def _apply_widget_geometry_from_settings(self) -> None:
        """按当前设置刷新任务栏窗口尺寸和位置。"""

        self.W = self._px(self.settings["width"])
        self.desired_H = self._px(self.settings["height"])
        self.H = self.desired_H
        self.root.geometry(f"{self.W}x{self.H}")
        self._build_ui()
        self._position_at_taskbar()

    def _build_ui(self) -> None:
        scale = self._scale()
        font_size = lambda base: dpi.font_pixels(
            max(6, base * scale), getattr(self, "_dpi_scale", 1.0)
        )
        self.canvas.config(width=self.W, height=self.H)
        self.f_row_label = tkfont.Font(
            family="Segoe UI", size=font_size(8), weight="bold"
        )
        self.f_row_pct = tkfont.Font(
            family="Segoe UI", size=font_size(9), weight="bold"
        )
        self.f_row_reset = tkfont.Font(family="Segoe UI", size=font_size(8))
        self.f_row_stat = tkfont.Font(
            family="Segoe UI", size=font_size(8), weight="bold"
        )
        self.f_status = tkfont.Font(
            family="Segoe UI", size=font_size(9), weight="bold"
        )
        self._redraw()

    def _redraw(self) -> None:
        self._draw(self.canvas, self.W, self.H, getattr(self, "_collapsed", False))
        display = getattr(self, "_taskbar_display", None)
        if display:
            display.draw(self.canvas.commands)
        if getattr(self, "_hover_visible", False):
            self._draw(self._hover_canvas, self._hover_width, self._hover_height, False)

    def _draw(self, canvas, width: int, height: int, compact: bool) -> None:
        canvas.delete("all")
        radius = max(self._px(8), min(self._px(14), height // 2 - 1))
        background = config.valid_widget_background(
            self.settings.get("widget_background")
        )
        ordinary_fg = contrast_foreground(background)
        canvas.create_polygon(
            round_rect_points(1, 1, width - 1, height - 1, radius),
            smooth=True,
            fill=background,
            outline=config.BORDER,
        )

        if self._status_code:
            canvas.create_text(
                width / 2,
                height / 2,
                text=self._status_code if compact else f"CodexBar {self._status_code}",
                anchor="center",
                fill=config.LOW,
                font=self.f_status,
            )
            return

        rows = self.data.get("rows", {})
        usage = self.data.get("usage", {})
        stale_labels = self._stale_labels() if getattr(self, "_stale_status", None) else None
        row_y = (height * 0.28, height * 0.72)
        for y, key, label in (
            (row_y[0], "h", "5h"),
            (row_y[1], "w", "每周"),
        ):
            row = rows.get(key, {})
            percent = row.get("remain")
            percent_text = "--" if percent is None else f"{percent:.0f}%"
            reset_text = format_reset_time(row.get("reset")) or "--"
            if stale_labels:
                percent_text += "*"
                reset_text = stale_labels[0 if key == "h" else 1]
            canvas.create_text(
                self._px(9), round(y), text=label, anchor="w", fill=config.ACCENT, font=self.f_row_label
            )
            canvas.create_text(
                width - self._px(9) if compact else self._px(43),
                round(y),
                text=percent_text,
                anchor="e" if compact else "w",
                fill=quota_color(percent),
                font=self.f_row_pct,
            )
            if compact:
                continue
            canvas.create_text(
                self._px(82),
                round(y),
                text=reset_text,
                anchor="w",
                fill=ordinary_fg,
                font=self.f_row_reset,
            )
            stat_text = usage.get("tokens") if key == "h" else usage.get("cost")
            canvas.create_text(
                width - self._px(10),
                round(y),
                text=stat_text or "--",
                anchor="e",
                fill=config.ACCENT if key == "h" else ordinary_fg,
                font=self.f_row_stat,
            )

    def _bind_mouse(self) -> None:
        self.canvas.bind("<Button-1>", self._on_dashboard_click)
        self.canvas.bind("<Double-Button-1>", self._on_settings_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Enter>", self._enter_bar)
        self.canvas.bind("<Leave>", self._leave_bar)

    def _on_dashboard_click(self, _event=None) -> None:
        self._cancel_hover_timer("_dashboard_click_id")
        self._dashboard_click_id = self.root.after(
            taskbar.double_click_interval(), self._finish_dashboard_click
        )

    def _finish_dashboard_click(self) -> None:
        self._dashboard_click_id = None
        if not self._closed:
            self._hide_hover()
            self.open_usage_dashboard()

    def _on_settings_double_click(self, _event=None) -> None:
        self._cancel_hover_timer("_dashboard_click_id")
        self._hide_hover()
        self.open_settings()

    def _cancel_hover_timer(self, attribute: str) -> None:
        timer = getattr(self, attribute, None)
        if timer:
            self.root.after_cancel(timer)
            setattr(self, attribute, None)

    def _enter_bar(self, _event=None) -> None:
        self.canvas.config(cursor="hand2")
        self._cancel_hover_timer("_hover_hide_id")
        if getattr(self, "_collapsed", False) and not self._context_menu_open:
            self._cancel_hover_timer("_hover_show_id")
            self._hover_show_id = self.root.after(150, self._show_hover)

    def _leave_bar(self, _event=None) -> None:
        self.canvas.config(cursor="")
        self._cancel_hover_timer("_hover_show_id")
        self._cancel_hover_timer("_hover_hide_id")
        self._hover_hide_id = self.root.after(250, self._check_hover_leave)

    def _hover_anchor(self):
        if getattr(self, "_tray_mode", False):
            return self._tray_entry.bounds() if self._tray_entry else None
        display = getattr(self, "_taskbar_display", None)
        return display.bounds() if display else taskbar.window_bounds(self.root.winfo_id())

    def _position_hover(self) -> bool:
        anchor = self._hover_anchor()
        if not anchor:
            return False
        size = taskbar.position_hover_popup(
            self._hover_window.winfo_id(), anchor,
            self._px(self.settings["width"]), self.desired_H, margin=self._px(4),
        )
        if size:
            self._hover_width, self._hover_height = size
            self._hover_canvas.config(width=size[0], height=size[1])
        return bool(size)

    def _show_hover(self) -> None:
        self._hover_show_id = None
        if self._closed or not self._collapsed or self._context_menu_open:
            return
        if self._hover_window is None:
            window = self._hover_window = tk.Toplevel(self.root)
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.attributes("-toolwindow", True)
            window.attributes("-transparentcolor", config.MAGIC)
            window.configure(bg=config.MAGIC)
            self._hover_canvas = tk.Canvas(window, bg=config.MAGIC, highlightthickness=0)
            self._hover_canvas.pack(fill="both", expand=True)
            self._hover_canvas.bind("<Enter>", lambda _e: self._cancel_hover_timer("_hover_hide_id"))
            self._hover_canvas.bind("<Leave>", self._leave_bar)
            self._hover_canvas.bind("<Button-1>", self._on_dashboard_click)
            self._hover_canvas.bind("<Double-Button-1>", self._on_settings_double_click)
            self._hover_canvas.bind("<Button-3>", self._on_right_click)
        self._hover_window.update_idletasks()
        if not self._position_hover():
            self._hide_hover()
            return
        self._hover_window.deiconify()
        self._position_hover()
        self._hover_visible = True
        self._redraw()

    def _check_hover_leave(self) -> None:
        self._hover_hide_id = None
        if not self._hover_visible:
            return
        x, y = self.root.winfo_pointerxy()
        bounds = [self._hover_anchor(), taskbar.window_bounds(self._hover_window.winfo_id())]
        if any(rect and rect[0] - self._px(6) <= x < rect[2] + self._px(6)
               and rect[1] - self._px(6) <= y < rect[3] + self._px(6) for rect in bounds):
            self._hover_hide_id = self.root.after(250, self._check_hover_leave)
        elif self._context_menu_open:
            self._hover_hide_id = self.root.after(250, self._check_hover_leave)
        else:
            self._hide_hover()

    def _hide_hover(self) -> None:
        self._cancel_hover_timer("_hover_show_id")
        self._cancel_hover_timer("_hover_hide_id")
        window = getattr(self, "_hover_window", None)
        if window:
            window.withdraw()
        self._hover_visible = False

    def _update_tray(self) -> None:
        if os.name != "nt":
            return
        if getattr(self, "_tray_entry", None) is None:
            self._tray_entry = tray.TrayEntry()
        rows = self.data.get("rows", {})
        amounts = []
        for key, label in (("h", "5h"), ("w", "每周")):
            percent = rows.get(key, {}).get("remain")
            amounts.append(f"{label} {'--' if percent is None else f'{percent:.0f}%'}")
        if getattr(self, "_status_code", None):
            amounts = [self._status_code]
        elif getattr(self, "_stale_status", None):
            amounts.append(self._stale_description())
        self._tray_entry.show("CodexBar " + " / ".join(amounts))

    def _stale_labels(self) -> tuple[str, str]:
        label = {"NET": "网络异常", "TLS": "证书异常"}.get(
            self._stale_status, "更新异常"
        )
        clock = time.strftime("%m/%d %H:%M", time.localtime(self._last_success_at))
        return f"{label} · 旧数据", f"上次 {clock}"

    def _stale_description(self) -> str:
        return " · ".join(self._stale_labels())

    def _reset_quota_data(self) -> None:
        """Forget old-account data and its freshness whenever login changes."""
        self.data = self._empty_data()
        self._last_success_at = None
        self._stale_status = None
        self._status_code = None
        self._quota_account_id = None

    def _poll_display(self) -> None:
        """Dispatch plain pointer packets on the independent application UI."""
        if self._closed:
            return
        display = self._taskbar_display
        if display:
            while True:
                try:
                    action, x, y = display.events.get_nowait()
                except queue.Empty:
                    break
                if action == "click":
                    self._on_dashboard_click()
                elif action == "settings":
                    self._on_settings_double_click()
                elif action == "menu":
                    self._on_right_click(SimpleNamespace(x_root=x, y_root=y))
                elif action == "hover":
                    self._enter_bar()
                elif action == "leave":
                    self._leave_bar()
                elif action == "close":
                    self.close()
                    return
        self.root.after(25, self._poll_display)

    def _poll_tray(self) -> None:
        if self._closed:
            return
        entry = self._tray_entry
        if entry:
            events, entry.events = entry.events, []
            for event in events:
                if event == "hover":
                    self._enter_bar()
                elif event == "leave":
                    self._leave_bar()
                elif event == "click":
                    self._on_dashboard_click()
                elif event == "settings":
                    self._on_settings_double_click()
                elif event == "menu":
                    x, y = self.root.winfo_pointerxy()
                    self._on_right_click(type("TrayEvent", (), {"x_root": x, "y_root": y})())
        self.root.after(100, self._poll_tray)

    def _on_right_click(self, event) -> None:
        menu_font = ("Segoe UI", dpi.font_pixels(9, self._dpi_scale))
        menu = tk.Menu(self.root, tearoff=0, font=menu_font)
        menu.add_command(
            label="立即刷新", command=self._menu_command(self.refresh_async)
        )
        menu.add_command(label="设置…", command=self._menu_command(self.open_settings))
        menu.add_command(
            label="Token 用量...",
            command=self._menu_command(self.open_usage_dashboard),
        )
        menu.add_command(
            label="打开错误日志...",
            command=self._menu_command(self.open_error_log),
        )
        menu.add_command(
            label="导出脱敏诊断...",
            command=self._menu_command(self.export_diagnostics),
        )
        accounts = []
        try:
            accounts = list_accounts()
        except Exception:
            accounts = []
        account_menu = tk.Menu(menu, tearoff=0, font=menu_font)
        if accounts:
            for account in accounts:
                label = f"{'✓ ' if account['active'] else ''}{account['label']}"
                account_menu.add_command(
                    label=label,
                    command=self._menu_command(
                        lambda account_id=account["account_id"]: self._switch_account(
                            account_id
                        )
                    ),
                )
        else:
            account_menu.add_command(label="没有已保存账号", state="disabled")
        menu.add_cascade(label="账号", menu=account_menu)
        menu.add_command(
            label="清除当前账号",
            command=self._menu_command(self._clear_current_account),
            state="normal" if has_credentials() else "disabled",
        )
        menu.add_command(
            label="清除所有账号",
            command=self._menu_command(self._clear_all_accounts),
            state="normal" if has_credentials() else "disabled",
        )
        menu.add_separator()
        menu.add_command(label="退出", command=self._menu_command(self.close))
        self._open_context_menu()
        menu.bind("<Unmap>", lambda _event: self._close_context_menu(), add="+")
        menu.tk_popup(event.x_root, event.y_root)

    def _open_context_menu(self) -> None:
        self._context_menu_open = True
        self._context_menu_generation += 1
        generation = self._context_menu_generation

        def fallback_close() -> None:
            if generation == self._context_menu_generation:
                self._close_context_menu()

        self.root.after(30 * 1000, fallback_close)

    def _close_context_menu(self) -> None:
        if not self._context_menu_open:
            return
        self._context_menu_open = False
        try:
            self.root.after(0, self._position_at_taskbar)
        except Exception:
            pass

    def _menu_command(self, callback):
        def run():
            self._close_context_menu()
            return callback()

        return run

    def _switch_account(self, account_id: str) -> None:
        with CREDENTIAL_LOCK:
            set_active_account(account_id)
        self._reset_quota_data()
        self.refresh_async()

    def _clear_current_account(self) -> None:
        confirmed = messagebox.askyesno(
            "清除当前账号",
            "删除当前选中的 CodexBar 账号缓存？",
            parent=self.root,
        )
        if not confirmed:
            return
        with CREDENTIAL_LOCK:
            delete_account()
        self._invalidate_refresh()
        self._reset_quota_data()
        if list_accounts():
            self.refresh_async()
        else:
            self._apply_error("AUTH")

    def _clear_all_accounts(self) -> None:
        confirmed = messagebox.askyesno(
            "清除所有账号",
            "删除所有加密保存的 CodexBar 账号缓存？\n当前 OAuth 登录仍存在时，下次刷新会重新捕获。",
            parent=self.root,
        )
        if not confirmed:
            return
        with CREDENTIAL_LOCK:
            clear_credentials()
        self._invalidate_refresh()
        self._reset_quota_data()
        self._apply_error("AUTH")

    def _invalidate_refresh(self) -> None:
        """Make any in-flight result stale and cancel its scheduled successor."""

        self._refresh_generation += 1
        self._refresh_pending = False
        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None

    def open_usage_dashboard(self) -> None:
        """打开 WebView token 周期统计主页面。"""

        current = getattr(self, "_usage_dashboard_process", None)
        if current and current.poll() is None:
            taskbar.activate_process_window(current.pid)
            return
        command, cwd = runtime.dashboard_command()
        try:
            self._usage_dashboard_process = subprocess.Popen(
                command,
                cwd=cwd,
                close_fds=True,
            )
        except Exception as error:
            diagnostics.log_exception("web_dashboard_launch", error)
            messagebox.showerror(
                "Token 用量",
                "无法打开 Token 用量页面，请查看错误日志。",
                parent=self.root,
            )

    def open_error_log(self) -> None:
        """打开本地诊断日志，便于分析偶发 ERR。"""

        if not os.path.exists(config.ERROR_LOG_PATH):
            diagnostics.log_event("log_created", stage="manual_open")
        try:
            os.startfile(config.ERROR_LOG_PATH)
        except OSError:
            os.startfile(os.path.dirname(config.ERROR_LOG_PATH))

    def export_diagnostics(self) -> None:
        """Ask for a destination and export diagnostics with a second redaction pass."""

        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出脱敏诊断信息",
            defaultextension=".txt",
            initialfile="CodexBar-diagnostics.txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not destination:
            return
        try:
            diagnostics.export_sanitized_diagnostics(destination, __version__)
        except OSError as error:
            diagnostics.log_exception("diagnostic_export", error)
            messagebox.showerror(
                "导出诊断信息",
                "无法导出诊断信息，请查看本地错误日志。",
                parent=self.root,
            )
            return
        messagebox.showinfo(
            "导出诊断信息",
            "脱敏诊断信息已导出。提交前仍建议人工检查内容。",
            parent=self.root,
        )

    def close(self) -> None:
        """Stop future callbacks and destroy the Tk root safely."""

        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._cancel_hover_timer("_dashboard_click_id")
        self._hide_hover()
        entry = getattr(self, "_tray_entry", None)
        if entry:
            entry.close()
        display = getattr(self, "_taskbar_display", None)
        if display:
            display.close()
        self._refresh_generation += 1
        self._refresh_pending = False
        if self._refresh_after_id:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except (tk.TclError, RuntimeError):
                pass
            self._refresh_after_id = None
        # A new Tk interpreter can otherwise receive old poll/menu callbacks
        # after Explorer recovery, referring to commands destroyed with root.
        try:
            pending = self.root.tk.splitlist(self.root.tk.call("after", "info"))
            for callback_id in pending:
                self.root.after_cancel(callback_id)
        except (tk.TclError, RuntimeError, TypeError):
            pass
        self.root.destroy()

    def open_settings(self) -> None:
        """打开刷新、显示和颜色设置窗口。"""

        current = getattr(self, "_settings_win", None)
        if current and tk.Toplevel.winfo_exists(current):
            current.lift()
            return

        window = tk.Toplevel(self.root)
        self._settings_win = window
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        try:
            window.attributes("-transparentcolor", config.MAGIC)
        except tk.TclError:
            pass
        window.configure(bg=config.MAGIC)

        original_settings = dict(self.settings)
        scale = self._dpi_scale
        px = lambda value: dpi.pixels(value, scale)
        font_size = lambda points: dpi.font_pixels(points, scale)
        layout = settings_layout_metrics()
        width, height = layout["width"], layout["height"]
        anchor = self._hover_anchor()
        if not anchor:
            info = taskbar.primary_taskbar()
            anchor = taskbar._info_bounds(info) if info else (0, px(height + 8), px(width), px(height + 8))
        x = max(0, anchor[2] - px(width))
        y = anchor[1] - px(height + 8)
        if y < 0:
            y = anchor[3] + px(8)
        window.geometry(f"{px(width)}x{px(height)}{x:+d}{y:+d}")

        canvas = tk.Canvas(window, bg=config.MAGIC, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_polygon(
            round_rect_points(1, 1, width - 1, height - 1, config.RADIUS),
            smooth=True,
            fill=config.CARD,
            outline=config.BORDER,
        )

        heading_font = tkfont.Font(family="Bahnschrift SemiBold", size=font_size(14))
        section_font = tkfont.Font(family="Segoe UI", size=font_size(9), weight="bold")
        label_font = tkfont.Font(family="Segoe UI", size=font_size(9))
        close_font = tkfont.Font(
            family="Segoe UI", size=font_size(layout["close_font_size"]), weight="bold"
        )
        value_font = tkfont.Font(family="Cascadia Mono", size=font_size(9), weight="bold")
        tiny_font = tkfont.Font(family="Segoe UI", size=font_size(8))

        canvas.create_text(
            22, 18, text="CodexBar", anchor="nw", fill=config.FG, font=heading_font
        )
        canvas.create_text(
            23, 44, text="任务栏额度控制台", anchor="nw", fill=config.SUB, font=tiny_font
        )
        for offset, bar_height in ((0, 16), (8, 28), (16, 10), (24, 22)):
            canvas.create_rectangle(
                width - 74 + offset,
                28,
                width - 70 + offset,
                28 + bar_height,
                fill=config.ACCENT if offset != 16 else config.BORDER,
                outline="",
            )
        canvas.create_line(22, 78, width - 22, 78, fill=config.BORDER)

        def cancel_settings() -> None:
            self._restore_settings_snapshot(original_settings)
            window.destroy()

        def section_title(y: int, title: str, caption: str) -> None:
            canvas.create_text(
                22, y, text=title, anchor="nw", fill=config.ACCENT, font=section_font
            )
            canvas.create_text(
                width - 22,
                y,
                text=caption,
                anchor="ne",
                fill=config.SUB,
                font=tiny_font,
            )
            canvas.create_line(22, y + 24, width - 22, y + 24, fill=config.BAR_BG)

        def label_row(y: int, label: str, value: str):
            canvas.create_text(26, y, text=label, anchor="nw", fill=config.FG, font=label_font)
            return canvas.create_text(
                width - 26,
                y,
                text=value,
                anchor="ne",
                fill=config.ACCENT,
                font=value_font,
            )

        def make_button(parent, text, command, primary=False, button_width=9):
            return tk.Button(
                parent,
                text=text,
                width=button_width,
                font=label_font,
                relief="flat",
                cursor="hand2",
                bg=config.ACCENT if primary else config.BAR_BG,
                fg=config.BG if primary else config.FG,
                activebackground="#6dd5f5" if primary else config.BORDER,
                activeforeground=config.BG if primary else config.FG,
                bd=0,
                command=command,
            )

        close_button = canvas.create_text(
            width - 22, 17, text="x", anchor="ne", fill=config.SUB, font=close_font
        )
        canvas.tag_bind(close_button, "<Button-1>", lambda _event: cancel_settings())
        canvas.tag_bind(
            close_button,
            "<Enter>",
            lambda _event: canvas.itemconfig(close_button, fill=config.FG),
        )
        canvas.tag_bind(
            close_button,
            "<Leave>",
            lambda _event: canvas.itemconfig(close_button, fill=config.SUB),
        )

        def drag_start(event):
            window._drag = (event.x_root, event.y_root, window.winfo_x(), window.winfo_y())

        def drag_move(event):
            start_x, start_y, origin_x, origin_y = window._drag
            window.geometry(
                f"+{origin_x + event.x_root - start_x}+{origin_y + event.y_root - start_y}"
            )

        canvas.bind("<Button-1>", drag_start)
        canvas.bind("<B1-Motion>", drag_move)

        section_title(96, "刷新", "后台请求节奏")
        refresh_var = tk.IntVar(value=self.settings["refresh_minutes"])
        refresh_label = label_row(130, "刷新间隔", f"{refresh_var.get()} min")

        def set_refresh_value(value) -> None:
            numeric = int(float(value))
            refresh_var.set(numeric)
            canvas.itemconfig(refresh_label, text=f"{numeric} min")

        refresh_scale = _styled_scale(
            window,
            refresh_var,
            config.REFRESH_MIN,
            config.REFRESH_MAX,
            292,
            set_refresh_value,
            dpi_scale=scale,
        )
        canvas.create_window(26, 156, anchor="nw", window=refresh_scale, width=292)

        presets = tk.Frame(window, bg=config.CARD)
        for minutes in (1, 5, 15, 30):
            make_button(
                presets,
                f"{minutes} min",
                lambda value=minutes: set_refresh_value(value),
                button_width=6,
            ).pack(side="left", padx=(0, px(7)))
        canvas.create_window(26, 194, anchor="nw", window=presets)

        section_title(238, "显示", "直接调整任务栏本体")
        width_var = tk.IntVar(value=self.settings["width"])
        width_label = label_row(272, "页面宽度", f"{width_var.get()} px")

        def preview_width(value) -> None:
            numeric = int(float(value))
            canvas.itemconfig(width_label, text=f"{numeric} px")
            self._preview_width(numeric)

        width_scale = _styled_scale(
            window,
            width_var,
            config.W_MIN,
            config.W_MAX,
            292,
            preview_width,
            dpi_scale=scale,
        )
        canvas.create_window(26, 298, anchor="nw", window=width_scale, width=292)

        font_scale_var = tk.IntVar(value=self.settings["font_scale_percent"])
        font_scale_label = label_row(338, "字体大小", f"{font_scale_var.get()}%")

        def preview_font_scale(value) -> None:
            numeric = int(float(value))
            canvas.itemconfig(font_scale_label, text=f"{numeric}%")
            self._preview_font_scale(numeric)

        font_scale = _styled_scale(
            window,
            font_scale_var,
            config.FONT_SCALE_MIN,
            config.FONT_SCALE_MAX,
            292,
            preview_font_scale,
            dpi_scale=scale,
        )
        canvas.create_window(26, 364, anchor="nw", window=font_scale, width=292)

        section_title(404, "颜色", "任务栏本体")
        background_var = tk.StringVar(value=self.settings["widget_background"])
        color_vars = {
            "color_good": tk.StringVar(value=self.settings["color_good"]),
            "color_warn": tk.StringVar(value=self.settings["color_warn"]),
            "color_low": tk.StringVar(value=self.settings["color_low"]),
        }

        canvas.create_text(
            26,
            440,
            text="组件背景",
            anchor="nw",
            fill=config.FG,
            font=label_font,
        )
        background_row = tk.Frame(window, bg=config.CARD)

        def update_background_swatch(color: str) -> None:
            background_swatch.config(
                bg=color,
                fg=contrast_foreground(color),
                activebackground=color,
                activeforeground=contrast_foreground(color),
            )

        def pick_background() -> None:
            _rgb, hex_color = colorchooser.askcolor(
                color=background_var.get(), parent=window, title="选择组件背景"
            )
            if hex_color:
                validated = config.valid_widget_background(hex_color)
                background_var.set(validated)
                update_background_swatch(validated)
                self._preview_widget_background(validated)

        def restore_background() -> None:
            background_var.set(config.CARD)
            update_background_swatch(config.CARD)
            self._preview_widget_background(config.CARD)

        background_swatch = make_button(
            background_row,
            "背景色",
            pick_background,
            button_width=8,
        )
        update_background_swatch(background_var.get())
        background_swatch.pack(side="left", padx=(0, px(8)))
        make_button(
            background_row,
            "恢复默认",
            restore_background,
            button_width=9,
        ).pack(side="left")
        canvas.create_window(122, 434, anchor="nw", window=background_row)

        canvas.create_text(
            26,
            484,
            text="额度状态",
            anchor="nw",
            fill=config.FG,
            font=label_font,
        )
        swatch_row = tk.Frame(window, bg=config.CARD)
        swatches: dict[str, tk.Button] = {}
        for key, label in (
            ("color_good", "充足"),
            ("color_warn", "偏少"),
            ("color_low", "告急"),
        ):
            swatch = make_button(
                swatch_row,
                label,
                lambda color_key=key: None,
                button_width=6,
            )
            swatch.config(
                bg=color_vars[key].get(),
                fg=config.BG,
                activebackground=color_vars[key].get(),
            )
            swatch.pack(side="left", padx=(0, px(8)))
            swatches[key] = swatch

            def pick_color(color_key=key):
                _rgb, hex_color = colorchooser.askcolor(
                    color=color_vars[color_key].get(), parent=window, title="选择颜色"
                )
                if hex_color:
                    color_vars[color_key].set(hex_color)
                    swatches[color_key].config(
                        bg=hex_color, activebackground=hex_color
                    )
                    self._preview_color(color_key, hex_color)

            swatch.config(command=pick_color)
        canvas.create_window(122, 478, anchor="nw", window=swatch_row)

        canvas.create_text(
            26,
            528,
            text="模型价格",
            anchor="nw",
            fill=config.FG,
            font=label_font,
        )
        price_button = make_button(
            window,
            "打开模型价格设置",
            lambda: self._open_price_settings(window),
            button_width=17,
        )
        canvas.create_window(188, 522, anchor="nw", window=price_button)
        canvas.create_line(
            22,
            layout["action_divider_y"],
            width - 22,
            layout["action_divider_y"],
            fill=config.BORDER,
        )

        def apply_settings():
            self.settings["width"] = int(width_var.get())
            self.settings["font_scale_percent"] = int(font_scale_var.get())
            self.settings["refresh_minutes"] = int(refresh_var.get())
            self.settings["widget_background"] = config.valid_widget_background(
                background_var.get()
            )
            for key, variable in color_vars.items():
                self.settings[key] = config.valid_hex(
                    variable.get(), config.DEFAULTS[key]
                )
            if not self._persist_settings(window):
                return
            set_colors(self.settings)
            self._apply_widget_geometry_from_settings()
            self.refresh_async()
            window.destroy()

        ok_button = make_button(window, "应用", apply_settings, primary=True)
        cancel_button = make_button(window, "取消", cancel_settings)
        canvas.create_window(
            width - 22, layout["action_button_y"], anchor="se", window=ok_button
        )
        canvas.create_window(
            width - 112, layout["action_button_y"], anchor="se", window=cancel_button
        )
        scale_canvas_layout(canvas, scale)

    def _persist_settings(self, parent: tk.Misc) -> bool:
        """Save settings and keep the editor open when the disk write fails."""

        try:
            config.save_config(self.settings)
        except OSError as error:
            diagnostics.log_exception("config_save", error)
            messagebox.showerror(
                "保存失败",
                "无法保存 CodexBar 设置，请检查磁盘空间或文件权限后重试。",
                parent=parent,
            )
            return False
        return True

    def _open_price_settings(self, parent: tk.Toplevel) -> None:
        """打开模型价格编辑窗口，保存用户对官方价格表的覆盖项。"""

        current = getattr(self, "_price_win", None)
        if current and tk.Toplevel.winfo_exists(current):
            current.lift()
            return

        window = tk.Toplevel(parent)
        self._price_win = window
        window.title("模型价格")
        window.attributes("-topmost", True)
        try:
            window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        window.configure(bg=config.CARD)
        scale = dpi.window_scale(parent.winfo_id())
        px = lambda value: dpi.pixels(value, scale)
        font_size = lambda points: dpi.font_pixels(points, scale)
        screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
        win_w = min(px(820), screen_width - px(32))
        win_h = min(px(480), screen_height - px(96))
        x = parent.winfo_rootx() + px(16)
        y = parent.winfo_rooty() - win_h - px(8)
        if y < 0:
            y = parent.winfo_rooty() + parent.winfo_height() + px(8)
        x = max(0, min(x, screen_width - win_w))
        y = max(0, min(y, screen_height - win_h))
        window.geometry(f"{win_w}x{win_h}{x:+d}{y:+d}")
        window.resizable(True, True)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        heading_font = tkfont.Font(family="Segoe UI", size=font_size(11), weight="bold")
        label_font = tkfont.Font(family="Segoe UI", size=font_size(9))
        small_font = tkfont.Font(family="Segoe UI", size=font_size(8))

        tk.Label(
            window,
            text="模型价格（USD / 1M tokens）",
            bg=config.CARD,
            fg=config.ACCENT,
            font=heading_font,
        ).grid(row=0, column=0, columnspan=7, sticky="w", padx=px(16), pady=(px(14), px(2)))
        tk.Label(
            window,
            text="官网价格每日自动更新；离线使用缓存。本地修改优先于官网价格。",
            bg=config.CARD,
            fg=config.SUB,
            font=small_font,
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=px(16), pady=(0, px(10)))

        table_container = tk.Frame(window, bg=config.CARD)
        table_container.grid(row=2, column=0, columnspan=7, sticky="nsew", padx=px(16))
        table_container.grid_columnconfigure(0, weight=1)
        table_container.grid_rowconfigure(0, weight=1)
        viewport = tk.Canvas(table_container, bg=config.CARD, highlightthickness=0)
        vertical = tk.Scrollbar(table_container, orient="vertical", command=viewport.yview)
        horizontal = tk.Scrollbar(table_container, orient="horizontal", command=viewport.xview)
        viewport.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        viewport.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table = tk.Frame(viewport, bg=config.CARD)
        viewport.create_window(0, 0, window=table, anchor="nw")
        table.bind("<Configure>", lambda _e: viewport.configure(scrollregion=viewport.bbox("all")))
        window.bind("<MouseWheel>", lambda e: viewport.yview_scroll(-int(e.delta / 120), "units"))
        headers = (
            ("model", "模型", 28),
            ("input", "输入", 9),
            ("cached_input", "缓存", 9),
            ("output", "输出", 9),
            ("long_input", "长输入", 9),
            ("long_cached_input", "长缓存", 9),
            ("long_output", "长输出", 9),
        )
        for column, (_key, label, width) in enumerate(headers):
            tk.Label(
                table,
                text=label,
                width=width,
                bg=config.CARD,
                fg=config.FG,
                font=label_font,
                anchor="w",
            ).grid(row=0, column=column, padx=(0, px(6)), pady=(0, px(4)), sticky="w")

        overrides = token_usage.load_model_price_overrides()
        official_prices = pricing.load_official_prices()
        model_names = list(official_prices)
        model_names.extend(
            name
            for name in sorted(overrides)
            if name not in official_prices
        )
        model_names.append("")
        rows: list[tuple[tk.StringVar, dict[str, tk.StringVar]]] = []

        def fmt_price(value: float | None) -> str:
            return "" if value is None else f"{value:g}"

        for row_index, model in enumerate(model_names, start=1):
            model_var = tk.StringVar(value=model)
            entry_options = {
                "textvariable": model_var,
                "width": 28,
                "relief": "flat",
                "bg": config.BAR_BG,
                "fg": config.FG,
                "insertbackground": config.FG,
                "font": label_font,
            }
            model_entry = tk.Entry(table, **entry_options)
            if model:
                model_entry.config(
                    state="disabled",
                    disabledbackground=config.BAR_BG,
                    disabledforeground=config.FG,
                )
            model_entry.grid(row=row_index, column=0, padx=(0, px(6)), pady=px(3), sticky="w")

            price_vars: dict[str, tk.StringVar] = {}
            base = official_prices.get(model, {})
            override = overrides.get(model, {})
            for column, key in enumerate(token_usage.PRICE_KEYS, start=1):
                value = override.get(key, base.get(key))
                variable = tk.StringVar(value=fmt_price(value))
                price_vars[key] = variable
                tk.Entry(
                    table,
                    textvariable=variable,
                    width=9,
                    relief="flat",
                    bg=config.BAR_BG,
                    fg=config.FG,
                    insertbackground=config.FG,
                    font=label_font,
                ).grid(row=row_index, column=column, padx=(0, px(6)), pady=px(3), sticky="w")
            rows.append((model_var, price_vars))

        def collect_overrides() -> dict:
            result: dict[str, dict[str, float]] = {}
            for model_var, price_vars in rows:
                model = model_var.get().strip()
                raw_values = {key: var.get() for key, var in price_vars.items()}
                if not model and not any(value.strip() for value in raw_values.values()):
                    continue
                if not model:
                    raise ValueError("新增模型需要填写模型名")

                values: dict[str, float] = {}
                for key, raw in raw_values.items():
                    try:
                        parsed = token_usage.parse_price_override_text(raw)
                    except ValueError as exc:
                        raise ValueError(f"{model} 的价格必须是非负数字") from exc
                    if parsed is not None:
                        values[key] = parsed
                base = official_prices.get(model)
                if base:
                    completed = token_usage.complete_price_override(model, values, base_prices=official_prices)
                    changed = any(
                        key not in base
                        or abs(completed[key] - base[key]) > 1e-9
                        for key in completed
                    )
                    if changed:
                        result[model] = completed
                elif values:
                    try:
                        result[model] = token_usage.complete_price_override(
                            model, values, base_prices=official_prices
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"{model} 需要填写输入、缓存和输出价格"
                        ) from exc
            return result

        def save_prices() -> None:
            try:
                overrides_to_save = collect_overrides()
            except ValueError as exc:
                messagebox.showerror("模型价格", str(exc), parent=window)
                return
            token_usage.save_model_price_overrides(overrides_to_save)
            self.refresh_async()
            window.destroy()

        def restore_official_prices() -> None:
            token_usage.clear_model_price_overrides()
            self.refresh_async()
            window.destroy()

        button_row = tk.Frame(window, bg=config.CARD)
        button_row.grid(row=3, column=0, columnspan=7, sticky="e", padx=px(16), pady=px(16))
        for text, command, background, foreground in (
            ("恢复官方价格", restore_official_prices, config.BAR_BG, config.FG),
            ("取消", window.destroy, config.BAR_BG, config.FG),
            ("保存", save_prices, config.ACCENT, config.BG),
        ):
            tk.Button(
                button_row,
                text=text,
                width=12,
                font=label_font,
                relief="flat",
                cursor="hand2",
                bg=background,
                fg=foreground,
                activebackground=config.BORDER,
                activeforeground=config.FG,
                bd=0,
                command=command,
            ).pack(side="left", padx=(px(8), 0))

    def refresh_async(self) -> None:
        """Coalesce refresh requests so only one worker runs at a time."""

        if getattr(self, "_closed", False):
            return

        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._redraw()
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._start_refresh_worker(generation)

    def _start_refresh_worker(self, generation: int) -> None:
        self._refresh_in_progress = True
        self._refresh_pending = False
        threading.Thread(
            target=self._refresh_worker,
            args=(generation,),
            daemon=True,
        ).start()

    def _refresh_worker(self, generation: int | None = None) -> None:
        if generation is None:
            generation = getattr(self, "_refresh_generation", 0)
        selected_accounts = []

        def post(data, usage, code):
            self._post_refresh_result(
                generation, data, usage, code,
                selected_accounts[0] if selected_accounts else None,
            )

        pricing.refresh_prices()
        try:
            data = fetch_quota(account_selected=selected_accounts.append)
            try:
                usage = token_usage.collect_today_usage()
            except Exception as error:
                diagnostics.log_exception("token_usage", error)
                usage = None
            post(data, usage, None)
        except AuthRequiredError as error:
            diagnostics.log_exception("quota_refresh", error, status="AUTH")
            post(None, None, "AUTH")
        except ReloginRequiredError as error:
            diagnostics.log_exception("quota_refresh", error, status="RELOGIN")
            post(None, None, "RELOGIN")
        except Exception as error:
            code = error_status(error)
            diagnostics.log_exception("quota_refresh", error, status=code)
            post(None, None, code)

    def _post_refresh_result(
        self,
        generation: int,
        data: QuotaData | None,
        usage: token_usage.TokenUsageData | None,
        error_code: str | None,
        account_id: str | None = None,
    ) -> None:
        """Marshal one worker result back to Tk unless shutdown has begun."""

        if getattr(self, "_closed", False):
            return
        try:
            self.root.after(
                0,
                self._finish_refresh,
                generation,
                data,
                usage,
                error_code,
                account_id,
            )
        except (tk.TclError, RuntimeError):
            return

    def _finish_refresh(
        self,
        generation: int,
        data: QuotaData | None,
        usage: token_usage.TokenUsageData | None,
        error_code: str | None,
        account_id: str | None = None,
    ) -> None:
        """Apply only the newest result, then run one coalesced pending refresh."""

        self._refresh_in_progress = False
        if getattr(self, "_closed", False):
            return

        if generation == self._refresh_generation:
            if account_id is not None and account_id != getattr(self, "_quota_account_id", None):
                # resolve_credentials can also select a new Desktop login, even
                # when the user has not used this widget's account menu.
                self._reset_quota_data()
                self._quota_account_id = account_id
            if error_code:
                self._apply_error(error_code)
            elif data is not None:
                self._apply(data, usage)

        if self._refresh_pending:
            self._start_refresh_worker(self._refresh_generation)

    def _schedule_next(self, minutes: int) -> None:
        if getattr(self, "_closed", False):
            return
        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.root.after(
            minutes * 60 * 1000, self.refresh_async
        )

    def _apply(
        self,
        data: QuotaData,
        usage: token_usage.TokenUsageData | None = None,
    ) -> None:
        self._status_code = None
        self._last_success_at = time.time()
        self._stale_status = None
        diagnostics.log_event("quota_refresh_success")
        token_text = token_usage.format_token_millions(
            usage["total_tokens"] if usage else 0
        )
        cost_text = token_usage.format_cost_usd(
            usage["cost_usd"] if usage else None,
            partial=bool(
                usage
                and usage.get("has_unknown_prices")
                and usage.get("cost_usd") is not None
            ),
        )
        if usage and usage.get("incomplete"):
            if token_text != "--":
                token_text = f"~{token_text}"
            if cost_text != "$--" and not cost_text.startswith("~"):
                cost_text = f"~{cost_text}"
        self.data = {
            "plan": data["plan"],
            "rows": {
                "h": {"remain": data["h_remain"], "reset": data["h_reset"]},
                "w": {"remain": data["w_remain"], "reset": data["w_reset"]},
            },
            "usage": {
                "tokens": token_text,
                "cost": cost_text,
            },
        }
        self._schedule_next(self.settings["refresh_minutes"])
        self._position_at_taskbar()
        self._redraw()

    def _apply_error(self, code: str) -> None:
        if code in ("AUTH", "RELOGIN", "HTTP401", "HTTP403"):
            self._reset_quota_data()
        if getattr(self, "_last_success_at", None) is not None:
            self._stale_status = code
            self._status_code = None
        else:
            self._stale_status = None
            self._status_code = code
        self._schedule_next(1)
        self._position_at_taskbar()
        self._redraw()

    def _tick(self) -> None:
        if getattr(self, "_closed", False):
            return
        if not taskbar.window_exists(self.root.winfo_id()):
            self._restart_requested = True
            self.root.quit()
            return
        # Explorer 重启或任务栏尺寸变化后重新定位；失去原生窗口则重建 Tk。
        self._position_at_taskbar()
        self._redraw()
        self.root.after(500, self._tick)
