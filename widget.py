"""Frameless desktop widget for the SSH server monitor (Windows)."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
import winreg

from PIL import Image, ImageDraw, ImageFont, ImageTk

from server_desk import CONFIG, EXAMPLE, Server, load_config, poll_server


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "widget_state.json"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "ServerDeskWidget"
WIDTH, HEIGHT, SCALE = 410, 612, 2
BG = "#E9F0EF"
WHITE = "#FFFFFF"
INK = "#152D35"
MUTED = "#405B63"
FAINT = "#718890"
TEAL = "#168779"
TEAL_LIGHT = "#EAF5F2"
RED = "#C95058"
AMBER = "#B98223"
LINE = "#DDE8E7"
TRACK = "#D5E4E1"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / path), size * SCALE)


FONTS = {
    "title": font("msyhbd.ttc", 20),
    "body": font("msyh.ttc", 12),
    "small": font("msyh.ttc", 10),
    "tiny": font("msyh.ttc", 9),
    "number": font("bahnschrift.ttf", 31),
    "metric": font("bahnschrift.ttf", 13),
    "metric_small": font("bahnschrift.ttf", 9),
}


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enabled:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if not pythonw.exists():
                raise FileNotFoundError(f"找不到 {pythonw}")
            command = f'"{pythonw}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def load_position(screen_width: int, screen_height: int) -> tuple[int, int]:
    default = (max(16, screen_width - WIDTH - 28), min(88, max(16, screen_height - HEIGHT - 16)))
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        x, y = int(data["x"]), int(data["y"])
        if 0 <= x <= screen_width - 100 and 0 <= y <= screen_height - 80:
            return x, y
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default


def save_position(x: int, y: int) -> None:
    STATE_FILE.write_text(json.dumps({"x": x, "y": y}, ensure_ascii=False), encoding="utf-8")


def color_for(value: float) -> str:
    return RED if value >= 90 else AMBER if value >= 75 else TEAL


def attach_to_desktop(root: tk.Tk, x: int, y: int) -> bool:
    """Keep the widget just above the desktop in the top-level Z order."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return False
    root.update_idletasks()
    inner = root.winfo_id()
    wrapper = user32.GetParent(inner) or inner
    above_desktop = user32.GetWindow(progman, 3)  # GW_HWNDPREV
    if not above_desktop:
        return False
    return bool(user32.SetWindowPos(wrapper, above_desktop, x, y, WIDTH, HEIGHT,
                                    0x0010 | 0x0040))


class Widget(tk.Tk):
    def __init__(self, preview: bool = False):
        super().__init__()
        self.title("Server Desk 小组件")
        self.overrideredirect(True)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.preview = preview
        self.x, self.y = load_position(self.winfo_screenwidth(), self.winfo_screenheight())
        self.geometry(f"{WIDTH}x{HEIGHT}+{self.x}+{self.y}")
        self.canvas = tk.Canvas(self, width=WIDTH, height=HEIGHT, bg=BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", self.popup_menu)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="立即刷新", command=self.refresh)
        self.menu.add_command(label="编辑服务器配置", command=self.open_config)
        self.menu.add_command(label="重新加载配置", command=self.load_servers)
        self.menu.add_separator()
        self.menu.add_command(label="开机启动：切换", command=self.toggle_autostart)
        self.menu.add_command(label="退出小组件", command=self.close)
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="server-widget")
        self.events: queue.Queue = queue.Queue()
        self.servers: list[Server] = []
        self.data: dict[str, tuple[dict[str, float | int] | None, str]] = {}
        self.interval = 30
        self.error_text = ""
        self.last_update = ""
        self.busy = False
        self.closed = False
        self.timer: str | None = None
        self.image = None
        self.desktop_attached = False
        self.dock_attempts = 0
        self.load_servers()
        self.after(80, self.drain)
        if not self.preview:
            self.after(300, self.dock)

    def dock(self) -> None:
        self.desktop_attached = attach_to_desktop(self, self.x, self.y)
        if not self.desktop_attached:
            # Explorer may not have created its desktop window yet during login.
            self.dock_attempts += 1
            self.attributes("-topmost", False)
            self.lower()
            if self.dock_attempts < 12:
                self.after(2500, self.dock)
        self.draw()

    def load_servers(self) -> None:
        if self.timer:
            self.after_cancel(self.timer)
            self.timer = None
        try:
            self.servers, self.interval = load_config()
            self.error_text = ""
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.servers = []
            self.error_text = f"配置未就绪：{exc}"
        self.data.clear()
        self.draw()
        self.refresh()

    def refresh(self) -> None:
        if self.busy or not self.servers or self.closed:
            return
        self.busy = True
        threading.Thread(target=self.collect, daemon=True).start()
        self.draw()

    def collect(self) -> None:
        try:
            futures = {self.pool.submit(poll_server, server): server.name for server in self.servers}
            for future in as_completed(futures):
                try:
                    payload = future.result()
                except Exception as exc:
                    payload = (None, f"采集失败：{type(exc).__name__}")
                self.events.put((futures[future], payload))
        finally:
            self.events.put(("__done__", None))

    def drain(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                if name == "__done__":
                    self.busy = False
                    self.last_update = time.strftime("%H:%M")
                    self.timer = self.after(self.interval * 1000, self.refresh)
                else:
                    self.data[name] = payload
                self.draw()
        except queue.Empty:
            pass
        if not self.closed:
            self.after(80, self.drain)

    def open_config(self) -> None:
        if not CONFIG.exists():
            CONFIG.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        os.startfile(CONFIG)

    def toggle_autostart(self) -> None:
        set_autostart(not autostart_enabled())
        self.draw()

    def press(self, event: tk.Event) -> None:
        if event.y < 78 and event.x > 350:
            self.refresh()
        elif event.y < 80:
            self.drag_origin = (event.x_root, event.y_root, self.x, self.y)

    def drag(self, event: tk.Event) -> None:
        if not self.drag_origin:
            return
        start_x, start_y, old_x, old_y = self.drag_origin
        self.x = old_x + event.x_root - start_x
        self.y = old_y + event.y_root - start_y
        self.geometry(f"{WIDTH}x{HEIGHT}+{self.x}+{self.y}")

    def release(self, _event: tk.Event) -> None:
        if self.drag_origin:
            self.drag_origin = None
            save_position(self.x, self.y)

    def popup_menu(self, event: tk.Event) -> None:
        self.menu.entryconfigure(4, label="关闭开机启动" if autostart_enabled() else "开启开机启动")
        self.menu.tk_popup(event.x_root, event.y_root)

    def close(self) -> None:
        self.closed = True
        if self.timer:
            self.after_cancel(self.timer)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    def draw(self) -> None:
        image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BG)
        d = ImageDraw.Draw(image)

        def box(coords, radius, fill, outline=None, width=1):
            bounds = tuple(int(v * SCALE) for v in coords)
            d.rounded_rectangle(bounds, radius=radius * SCALE, fill=fill,
                                outline=outline, width=width * SCALE)

        def text(x, y, value, style, fill=INK, anchor=None):
            d.text((x * SCALE, y * SCALE), str(value), font=FONTS[style], fill=fill, anchor=anchor)

        def line(coords, fill=LINE, width=1):
            d.line(tuple(int(v * SCALE) for v in coords), fill=fill, width=width * SCALE)

        box((3, 3, WIDTH - 4, HEIGHT - 4), 18, WHITE, "#CFDEDB")
        # Small rack mark and a restrained header.
        box((22, 22, 45, 48), 6, TEAL_LIGHT)
        for yy in (28, 36):
            box((27, yy, 40, yy + 5), 2, "#88C9BF")
            d.ellipse((29 * SCALE, (yy + 2) * SCALE, 31 * SCALE, (yy + 4) * SCALE), fill=TEAL)
        text(55, 20, "服务器概览", "title")
        text(56, 46, f"{len(self.servers)} 台节点  ·  {self.interval} 秒刷新", "small", MUTED)
        d.arc((362 * SCALE, 25 * SCALE, 386 * SCALE, 49 * SCALE),
              40, 325, fill=TEAL, width=2 * SCALE)
        d.polygon([(386 * SCALE, 29 * SCALE), (388 * SCALE, 37 * SCALE),
                   (380 * SCALE, 36 * SCALE)], fill=TEAL)

        total = len(self.servers)
        online = sum(data is not None for data, _ in self.data.values())
        attention = 0
        for metrics, _ in self.data.values():
            if metrics is None or max((float(metrics[k]) for k in ("cpu", "memory", "disk", "gpu")
                                       if k in metrics), default=0) >= 90:
                attention += 1
        box((18, 78, 392, 158), 13, TEAL_LIGHT)
        text(35, 87, f"{online:02}", "number", INK)
        text(77, 105, f"/ {total:02}", "metric", MUTED)
        text(35, 135, "在线节点", "small", MUTED)
        if attention:
            box((208, 102, 294, 134), 9, "#FFF4F1")
            text(251, 119, f"{attention} 需关注", "small", INK, "mm")
        else:
            box((208, 102, 294, 134), 9, "#D9F1EB")
            text(251, 119, "运行正常", "small", INK, "mm")
        ring = (325 * SCALE, 90 * SCALE, 382 * SCALE, 147 * SCALE)
        d.arc(ring, 0, 359, fill="#C9E5DF", width=5 * SCALE)
        if total:
            d.arc(ring, -90, -90 + 360 * online / total, fill=TEAL, width=5 * SCALE)
        text(353, 119, f"{round(100 * online / total) if total else 0}%", "metric", INK, "mm")

        text(28, 174, "节点", "tiny", MUTED)
        for xpos, label in ((200, "CPU"), (263, "内存"), (326, "根分区"), (389, "GPU")):
            text(xpos, 175, label, "tiny", MUTED, "ra")
        line((22, 193, 388, 193))

        metric_x = {"cpu": 150, "memory": 212, "disk": 274, "gpu": 336}
        for index, server in enumerate(self.servers[:9]):
            top = 198 + index * 40
            if index % 2:
                box((18, top, 392, top + 39), 6, "#F6FAF9")
            payload = self.data.get(server.name)
            metrics, error = payload if payload else (None, "")
            status = FAINT if payload is None else (RED if metrics is None else TEAL)
            d.ellipse((25 * SCALE, (top + 13) * SCALE, 32 * SCALE,
                       (top + 20) * SCALE), fill=status)
            text(39, top + 5, server.name, "body", INK)
            text(39, top + 24, server.host, "tiny", MUTED)
            for name, xpos in metric_x.items():
                value = float(metrics[name]) if metrics and name in metrics else None
                label = f"{value:.0f}%" if value is not None else "—"
                fill = color_for(value) if value is not None else FAINT
                text(xpos + 52, top + 6, label, "metric", INK if value is not None else MUTED, "ra")
                box((xpos, top + 27, xpos + 52, top + 30), 1, TRACK)
                if value is not None and value > 0:
                    box((xpos, top + 27, xpos + max(2, 52 * value / 100), top + 30), 1, fill)
            if index < len(self.servers) - 1:
                line((23, top + 39, 387, top + 39), LINE)

        line((22, 563, 388, 563))
        if self.error_text:
            footer = self.error_text[:33]
        elif any(metrics is None for metrics, _ in self.data.values()):
            footer = "部分节点未连接 · 右键可查看配置"
        else:
            footer = "数据来自 SSH · 右键打开设置"
        text(25, 575, footer, "small", MUTED)
        stamp = f"更新 {self.last_update}" if self.last_update else ("连接中" if self.busy else "等待采集")
        text(386, 575, stamp, "small", MUTED, "ra")

        image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        self.image = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.image, anchor="nw")


def main() -> None:
    parser = argparse.ArgumentParser(description="桌面服务器监控小组件")
    parser.add_argument("--install-startup", action="store_true")
    parser.add_argument("--remove-startup", action="store_true")
    parser.add_argument("--preview", action="store_true", help="显示普通浮动窗口，用于界面检查")
    args = parser.parse_args()
    if args.install_startup:
        set_autostart(True)
        print("开机启动已启用")
        return
    if args.remove_startup:
        set_autostart(False)
        print("开机启动已关闭")
        return
    Widget(preview=args.preview).mainloop()


if __name__ == "__main__":
    main()