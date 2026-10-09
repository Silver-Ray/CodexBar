"""Protect the boundary between Explorer's input queue and application UI."""
import queue
import threading
import unittest
from pathlib import Path
from unittest import mock

from codexbar import config, taskbar, taskbar_display, ui


class DisplayIsolationTests(unittest.TestCase):
    def test_frozen_manifest_declares_layered_child_window_compatibility(self):
        manifest = Path(taskbar_display.__file__).with_name("app.manifest").read_text(encoding="utf-8")
        self.assertIn('xmlns="urn:schemas-microsoft-com:compatibility.v1"', manifest)
        self.assertIn("8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a", manifest)

    def test_child_transparency_preserves_native_styles_and_uses_color_key(self):
        native = mock.Mock()
        with mock.patch.object(taskbar, "_USER32", native), mock.patch.object(
            taskbar, "_GET_WINDOW_LONG_PTR", return_value=0x8000088
        ), mock.patch.object(taskbar, "_SET_WINDOW_LONG_PTR") as set_style:
            taskbar_display._enable_transparency(123)
        set_style.assert_called_once_with(123, taskbar.GWL_EXSTYLE, 0x8080088)
        native.SetLayeredWindowAttributes.assert_called_once_with(123, 0xfe00ff, 255, 1)

    def test_recorded_font_is_a_plain_pixel_font_not_a_tk_object(self):
        canvas = object.__new__(taskbar_display.RecordedCanvas)
        canvas.commands = []
        font = mock.Mock(spec=taskbar_display.tkfont.Font)
        font.cget.side_effect = {"family": "Segoe UI", "size": -16,
                                 "weight": "bold", "slant": "roman"}.__getitem__
        with mock.patch.object(taskbar_display.tk.Canvas, "create_text", return_value=5):
            canvas.create_text(12, 8, text="57%", font=font)
        self.assertEqual(canvas.commands[0][2]["font"], ("Segoe UI", -16, "bold"))

    def test_frame_submission_copies_mutable_options(self):
        display = object.__new__(taskbar_display.TaskbarDisplay)
        display._lock, display._pending = threading.Lock(), {}
        options = {"text": "57%", "font": ("Segoe UI", -16, "bold")}
        commands = [("text", (12, 8), options)]
        display.draw(commands)
        options["text"] = "0%"
        commands.clear()
        self.assertEqual(display._pending["draw"][0][2]["text"], "57%")

    def test_controller_positions_display_without_embedding_or_showing_its_root(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = False
        widget._dpi_scale = 1
        widget.settings = dict(config.DEFAULTS)
        widget.W, widget.H = config.DEFAULTS["width"], config.DEFAULTS["height"]
        widget.root = mock.Mock()
        widget._taskbar_display = mock.Mock()
        widget._build_ui = mock.Mock()
        widget._hide_hover = mock.Mock()
        info = taskbar.TaskbarInfo(22, 0, 900, 1920, 948, 1700)
        bounds = (1500, 904, 1596, 944)
        with mock.patch.object(taskbar, "primary_scale", return_value=1), mock.patch.object(
            taskbar, "taskbar_placement", return_value=(info, bounds)
        ), mock.patch.object(taskbar, "position_taskbar_popup") as embed:
            widget._position_at_taskbar()
        embed.assert_not_called()
        widget.root.winfo_id.assert_not_called()
        widget.root.deiconify.assert_not_called()
        widget._taskbar_display.place.assert_called_once_with(info, bounds)
        self.assertTrue(widget._collapsed)

    def test_pointer_packets_run_callbacks_on_controller_and_preserve_coordinates(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._closed = False
        widget.root = mock.Mock()
        widget._taskbar_display = mock.Mock()
        widget._taskbar_display.events = queue.SimpleQueue()
        widget._on_dashboard_click = mock.Mock()
        widget._on_settings_double_click = mock.Mock()
        widget._on_right_click = mock.Mock()
        for action in ("click", "settings", "menu"):
            widget._taskbar_display.events.put((action, 1200, 930))
        widget._poll_display()
        widget._on_dashboard_click.assert_called_once_with()
        widget._on_settings_double_click.assert_called_once_with()
        event = widget._on_right_click.call_args.args[0]
        self.assertEqual((event.x_root, event.y_root), (1200, 930))
        widget.root.after.assert_called_once_with(25, widget._poll_display)

    def test_stopping_display_never_waits_indefinitely_for_explorer(self):
        display = object.__new__(taskbar_display.TaskbarDisplay)
        display._stop, display._thread = threading.Event(), mock.Mock()
        display.close()
        self.assertTrue(display._stop.is_set())
        display._thread.join.assert_called_once_with(timeout=0.5)

    def test_existing_dashboard_restore_uses_async_window_api(self):
        native = mock.Mock()
        native.EnumWindows.side_effect = lambda callback, _: callback(123, 0)
        native.GetWindowThreadProcessId.side_effect = lambda _, pid: setattr(pid._obj, "value", 77)
        native.IsWindowVisible.return_value = True
        with mock.patch.object(taskbar, "_IS_WINDOWS", True), mock.patch.object(taskbar, "_USER32", native):
            taskbar.activate_process_window(77)
        native.ShowWindow.assert_not_called()
        native.ShowWindowAsync.assert_called_once_with(123, 9)


if __name__ == "__main__":
    unittest.main()
