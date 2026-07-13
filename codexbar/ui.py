"""Tk 任务栏小组件与设置界面。

这个模块只关心“怎样显示”和“怎样响应鼠标”。额度数据来自 ``quota_api``，
凭据清理由 ``credentials`` 完成，窗口位置交给 ``taskbar``。后台线程不能直接
修改 Tk 控件，所以网络请求完成后必须通过 ``root.after`` 回到主线程。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, font as tkfont, messagebox

from . import __version__
from . import config, diagnostics, runtime, taskbar, token_usage
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
from .quota_api import QuotaData, fetch_quota, format_reset_time


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
        length=length,
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
        self._status_code: str | None = None
        self._context_menu_open = False
        self._context_menu_generation = 0
        self.data = self._empty_data()

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-toolwindow", True)
            root.attributes("-transparentcolor", config.MAGIC)
        except tk.TclError:
            pass
        root.configure(bg=config.MAGIC)

        self.W = self.settings["width"]
        self.desired_H = self.settings["height"]
        self.H = self.desired_H
        root.geometry(f"{self.W}x{self.H}")

        self.canvas = tk.Canvas(root, bg=config.MAGIC, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_ui()
        self._bind_mouse()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.update_idletasks()
        self._position_at_taskbar()

        self.refresh_async()
        self._tick()

    def _empty_data(self) -> dict:
        return {
            "plan": "",
            "rows": {"h": {}, "w": {}},
            "usage": {"tokens": "--", "cost": "$--"},
        }

    def _position_at_taskbar(self) -> None:
        if self._context_menu_open:
            return
        target_height = getattr(self, "desired_H", self.H)
        positioned = taskbar.position_taskbar_popup(
            self.root.winfo_id(), self.W, target_height, margin=4
        )
        if positioned and positioned[1] != self.H:
            self.H = positioned[1]
            self._build_ui()

    def _scale(self) -> float:
        # 字体按用户设置的目标高度计算，避免任务栏临时返回较小高度时文字跳小。
        target_height = getattr(self, "desired_H", self.H)
        font_scale = config.clamp(
            int(self.settings.get("font_scale_percent", 100)),
            config.FONT_SCALE_MIN,
            config.FONT_SCALE_MAX,
        ) / 100
        base_scale = min(self.W / config.BASE_W, target_height / config.BASE_H)
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
        self.W = int(self.settings["width"])
        self.desired_H = int(self.settings["height"])
        self.H = self.desired_H
        self.root.geometry(f"{self.W}x{self.H}")
        self._build_ui()
        self._position_at_taskbar()

    def _apply_widget_geometry_from_settings(self) -> None:
        """按当前设置刷新任务栏窗口尺寸和位置。"""

        self.W = int(self.settings["width"])
        self.desired_H = int(self.settings["height"])
        self.H = self.desired_H
        self.root.geometry(f"{self.W}x{self.H}")
        self._build_ui()
        self._position_at_taskbar()

    def _build_ui(self) -> None:
        scale = self._scale()
        font_size = lambda base: max(6, round(base * scale))
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
        canvas = self.canvas
        canvas.delete("all")
        radius = max(8, min(14, self.H // 2 - 1))
        background = config.valid_widget_background(
            self.settings.get("widget_background")
        )
        ordinary_fg = contrast_foreground(background)
        canvas.create_polygon(
            round_rect_points(1, 1, self.W - 1, self.H - 1, radius),
            smooth=True,
            fill=background,
            outline=config.BORDER,
        )

        if self._status_code:
            canvas.create_text(
                self.W / 2,
                self.H / 2,
                text=f"CodexBar {self._status_code}",
                anchor="center",
                fill=config.LOW,
                font=self.f_status,
            )
            return

        rows = self.data.get("rows", {})
        usage = self.data.get("usage", {})
        row_y = (self.H * 0.28, self.H * 0.72)
        for y, key, label in (
            (row_y[0], "h", "5h"),
            (row_y[1], "w", "每周"),
        ):
            row = rows.get(key, {})
            percent = row.get("remain")
            percent_text = "--" if percent is None else f"{percent:.0f}%"
            reset_text = format_reset_time(row.get("reset")) or "--"
            canvas.create_text(
                9, y, text=label, anchor="w", fill=config.ACCENT, font=self.f_row_label
            )
            canvas.create_text(
                43,
                y,
                text=percent_text,
                anchor="w",
                fill=quota_color(percent),
                font=self.f_row_pct,
            )
            canvas.create_text(
                82,
                y,
                text=reset_text,
                anchor="w",
                fill=ordinary_fg,
                font=self.f_row_reset,
            )
            stat_text = usage.get("tokens") if key == "h" else usage.get("cost")
            canvas.create_text(
                self.W - 10,
                y,
                text=stat_text or "--",
                anchor="e",
                fill=config.ACCENT if key == "h" else ordinary_fg,
                font=self.f_row_stat,
            )

    def _bind_mouse(self) -> None:
        self.canvas.bind("<Button-1>", lambda _event: self.refresh_async())
        self.canvas.bind("<Double-Button-1>", lambda _event: self.open_settings())
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.config(cursor="hand2"))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.config(cursor=""))

    def _on_right_click(self, event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
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
        account_menu = tk.Menu(menu, tearoff=0)
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
        self.data = self._empty_data()
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
        self.data = self._empty_data()
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
        self.data = self._empty_data()
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
        self._refresh_generation += 1
        self._refresh_pending = False
        if self._refresh_after_id:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except (tk.TclError, RuntimeError):
                pass
            self._refresh_after_id = None
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
        layout = settings_layout_metrics()
        width, height = layout["width"], layout["height"]
        x = max(0, self.root.winfo_rootx() - width + self.W)
        y = self.root.winfo_rooty() - height - 8
        if y < 0:
            y = self.root.winfo_rooty() + self.H + 8
        window.geometry(f"{width}x{height}+{x}+{y}")

        canvas = tk.Canvas(window, bg=config.MAGIC, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_polygon(
            round_rect_points(1, 1, width - 1, height - 1, config.RADIUS),
            smooth=True,
            fill=config.CARD,
            outline=config.BORDER,
        )

        heading_font = tkfont.Font(family="Bahnschrift SemiBold", size=14)
        section_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        label_font = tkfont.Font(family="Segoe UI", size=9)
        close_font = tkfont.Font(
            family="Segoe UI", size=layout["close_font_size"], weight="bold"
        )
        value_font = tkfont.Font(family="Cascadia Mono", size=9, weight="bold")
        tiny_font = tkfont.Font(family="Segoe UI", size=8)

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
        )
        canvas.create_window(26, 156, anchor="nw", window=refresh_scale, width=292)

        presets = tk.Frame(window, bg=config.CARD)
        for minutes in (1, 5, 15, 30):
            make_button(
                presets,
                f"{minutes} min",
                lambda value=minutes: set_refresh_value(value),
                button_width=6,
            ).pack(side="left", padx=(0, 7))
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
        background_swatch.pack(side="left", padx=(0, 8))
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
            swatch.pack(side="left", padx=(0, 8))
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
        win_w, win_h = 780, 360
        x = max(0, parent.winfo_rootx() + 16)
        y = max(0, parent.winfo_rooty() - win_h - 8)
        if y < 0:
            y = parent.winfo_rooty() + parent.winfo_height() + 8
        window.geometry(f"{win_w}x{win_h}+{x}+{y}")
        window.resizable(False, False)

        heading_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        label_font = tkfont.Font(family="Segoe UI", size=9)
        small_font = tkfont.Font(family="Segoe UI", size=8)

        tk.Label(
            window,
            text="模型价格（USD / 1M tokens）",
            bg=config.CARD,
            fg=config.ACCENT,
            font=heading_font,
        ).grid(row=0, column=0, columnspan=7, sticky="w", padx=16, pady=(14, 2))
        tk.Label(
            window,
            text="空白表示不覆盖；恢复官方价格会删除本地价格文件。",
            bg=config.CARD,
            fg=config.SUB,
            font=small_font,
        ).grid(row=1, column=0, columnspan=7, sticky="w", padx=16, pady=(0, 10))

        table = tk.Frame(window, bg=config.CARD)
        table.grid(row=2, column=0, columnspan=7, sticky="nw", padx=16)
        headers = (
            ("model", "模型", 18),
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
            ).grid(row=0, column=column, padx=(0, 6), pady=(0, 4), sticky="w")

        overrides = token_usage.load_model_price_overrides()
        model_names = list(token_usage.DEFAULT_MODEL_PRICES)
        model_names.extend(
            name
            for name in sorted(overrides)
            if name not in token_usage.DEFAULT_MODEL_PRICES
        )
        model_names.append("")
        rows: list[tuple[tk.StringVar, dict[str, tk.StringVar]]] = []

        def fmt_price(value: float | None) -> str:
            return "" if value is None else f"{value:g}"

        for row_index, model in enumerate(model_names, start=1):
            model_var = tk.StringVar(value=model)
            entry_options = {
                "textvariable": model_var,
                "width": 18,
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
            model_entry.grid(row=row_index, column=0, padx=(0, 6), pady=3, sticky="w")

            price_vars: dict[str, tk.StringVar] = {}
            base = token_usage.DEFAULT_MODEL_PRICES.get(model, {})
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
                ).grid(row=row_index, column=column, padx=(0, 6), pady=3, sticky="w")
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
                base = token_usage.DEFAULT_MODEL_PRICES.get(model)
                if base:
                    completed = token_usage.complete_price_override(model, values)
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
                            model, values
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
        button_row.grid(row=3, column=0, columnspan=7, sticky="e", padx=16, pady=16)
        for text, command, background, foreground in (
            ("恢复官方价格", restore_official_prices, config.BAR_BG, config.FG),
            ("取消", window.destroy, config.BAR_BG, config.FG),
            ("保存", save_prices, config.ACCENT, config.BG),
        ):
            tk.Button(
                button_row,
                text=text,
                width=12,
                relief="flat",
                cursor="hand2",
                bg=background,
                fg=foreground,
                activebackground=config.BORDER,
                activeforeground=config.FG,
                bd=0,
                command=command,
            ).pack(side="left", padx=(8, 0))

    def refresh_async(self) -> None:
        """Coalesce refresh requests so only one worker runs at a time."""

        if getattr(self, "_closed", False):
            return

        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._status_code = None
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
        try:
            data = fetch_quota()
            try:
                usage = token_usage.collect_today_usage()
            except Exception as error:
                diagnostics.log_exception("token_usage", error)
                usage = None
            self._post_refresh_result(generation, data, usage, None)
        except AuthRequiredError as error:
            diagnostics.log_exception("quota_refresh", error, status="AUTH")
            self._post_refresh_result(generation, None, None, "AUTH")
        except ReloginRequiredError as error:
            diagnostics.log_exception("quota_refresh", error, status="RELOGIN")
            self._post_refresh_result(generation, None, None, "RELOGIN")
        except Exception as error:
            diagnostics.log_exception("quota_refresh", error, status="ERR")
            self._post_refresh_result(generation, None, None, "ERR")

    def _post_refresh_result(
        self,
        generation: int,
        data: QuotaData | None,
        usage: token_usage.TokenUsageData | None,
        error_code: str | None,
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
            )
        except (tk.TclError, RuntimeError):
            return

    def _finish_refresh(
        self,
        generation: int,
        data: QuotaData | None,
        usage: token_usage.TokenUsageData | None,
        error_code: str | None,
    ) -> None:
        """Apply only the newest result, then run one coalesced pending refresh."""

        self._refresh_in_progress = False
        if getattr(self, "_closed", False):
            return

        if generation == self._refresh_generation:
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
        self._status_code = code
        self._refresh_after_id = self.root.after(60 * 1000, self.refresh_async)
        self._position_at_taskbar()
        self._redraw()

    def _tick(self) -> None:
        if getattr(self, "_closed", False):
            return
        # explorer 重启、任务栏尺寸变化后，定期重新建立 owner 和位置。
        self._position_at_taskbar()
        self._redraw()
        self.root.after(5 * 1000, self._tick)
