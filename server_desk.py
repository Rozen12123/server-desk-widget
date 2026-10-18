"""A compact Windows desktop monitor for Linux hosts reachable over SSH."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "servers.json"
EXAMPLE = ROOT / "servers.example.json"
BG = "#EDF3F5"
HEADER = "#102D3D"
PANEL = "#FFFFFF"
PANEL_2 = "#1A4254"
TEXT = "#173749"
MUTED = "#607C8B"
ACCENT = "#0D9688"
WARN = "#B67516"
BAD = "#C83F53"
TRACK = "#DFE9ED"
LINE = "#D8E4E9"
FONT = "Bahnschrift"

REMOTE_SCRIPT = r"""LC_ALL=C
first=$(awk '/^cpu / { idle=$5+$6; total=0; for (i=2;i<=NF;i++) total+=$i; print idle, total; exit }' /proc/stat)
sleep 0.25
second=$(awk '/^cpu / { idle=$5+$6; total=0; for (i=2;i<=NF;i++) total+=$i; print idle, total; exit }' /proc/stat)
cpu=$(awk -v a="$first" -v b="$second" 'BEGIN { split(a,x," "); split(b,y," "); dt=y[2]-x[2]; di=y[1]-x[1]; if(dt>0) printf "%.1f", 100*(dt-di)/dt; else print "0" }')
memory=$(awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {available=$2} END {if(total>0) printf "%.1f %.1f", 100*(total-available)/total, total/1048576; else print "0 0"}' /proc/meminfo)
disk=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
uptime=$(awk '{printf "%.0f", $1}' /proc/uptime)
printf 'cpu=%s\n' "$cpu"
printf 'memory=%s\n' "$memory"
printf 'disk=%s\n' "$disk"
printf 'uptime=%s\n' "$uptime"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | awk -F, 'BEGIN {n=0;max=0;used=0;total=0} {gsub(/ /,"",$1);gsub(/ /,"",$2);gsub(/ /,"",$3);if($1+0>max)max=$1+0;used+=$2+0;total+=$3+0;n++} END {if(n>0) printf "gpu=%.1f %d %d %d\n", max, used, total, n}'
fi
"""


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    user: str
    port: int = 22
    key_file: str = ""


def load_config(path: Path = CONFIG) -> tuple[list[Server], int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise ValueError("配置需要包含 servers 数组")
    interval = data.get("refresh_seconds", 30)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 3600:
        raise ValueError("refresh_seconds 必须是 5–3600 的整数")
    servers = []
    names = set()
    for index, item in enumerate(data["servers"], 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 台服务器配置不是对象")
        name = item.get("name", "")
        host = item.get("host", "")
        user = item.get("user", "")
        port = item.get("port", 22)
        key_file = item.get("key_file", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 台服务器缺少 name")
        if name.strip() in names:
            raise ValueError(f"服务器名称重复: {name.strip()}")
        names.add(name.strip())
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
            raise ValueError(f"{name}: host 格式无效")
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
            raise ValueError(f"{name}: user 格式无效")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{name}: port 必须在 1–65535 之间")
        if not isinstance(key_file, str):
            raise ValueError(f"{name}: key_file 必须是路径字符串")
        servers.append(Server(name.strip(), host, user, port, key_file))
    return servers, interval


def parse_metrics(output: str) -> dict[str, float | int]:
    pairs = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            pairs[key] = value.strip()
    result: dict[str, float | int] = {}
    for key in ("cpu", "disk"):
        value = float(pairs[key])
        if not math.isfinite(value):
            raise ValueError(f"{key} 不是有限数值")
        result[key] = max(0.0, min(100.0, value))
    memory = pairs["memory"].split()
    memory_percent, memory_total = float(memory[0]), float(memory[1])
    if not math.isfinite(memory_percent) or not math.isfinite(memory_total) or memory_total <= 0:
        raise ValueError("内存指标无效")
    result["memory"] = max(0.0, min(100.0, memory_percent))
    result["memory_total_gb"] = memory_total
    result["uptime"] = int(pairs["uptime"])
    if "gpu" in pairs:
        gpu = pairs["gpu"].split()
        gpu_percent = float(gpu[0])
        if not math.isfinite(gpu_percent):
            raise ValueError("GPU 指标无效")
        result["gpu"] = max(0.0, min(100.0, gpu_percent))
        result["gpu_used_mb"] = int(gpu[1])
        result["gpu_total_mb"] = int(gpu[2])
        result["gpu_count"] = int(gpu[3])
    return result


def poll_server(server: Server) -> tuple[dict[str, float | int] | None, str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=yes", "-p", str(server.port)]
    if server.key_file:
        command.extend(["-i", str(Path(server.key_file).expanduser())])
    command.extend([f"{server.user}@{server.host}", "sh", "-s"])
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        for attempt in range(2):
            # Send LF bytes: text=True converts stdin to CRLF on Windows, which breaks remote sh.
            proc = subprocess.run(command, input=REMOTE_SCRIPT.encode("utf-8"), capture_output=True,
                                  timeout=12, creationflags=flags, check=False)
            if proc.returncode == 0:
                return parse_metrics(proc.stdout.decode("utf-8", errors="replace")), ""
            detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
            error = detail[-1][:90] if detail else f"SSH 退出码 {proc.returncode}"
            if attempt == 0 and ("Connection closed" in error or "Connection reset" in error):
                time.sleep(0.5)
                continue
            return None, error
        return None, "SSH 连接失败"
    except FileNotFoundError:
        return None, "找不到 OpenSSH 客户端 (ssh)"
    except subprocess.TimeoutExpired:
        return None, "连接超时"
    except (ValueError, KeyError, IndexError):
        return None, "服务器返回的指标格式不正确"


def color_for(value: float) -> str:
    return BAD if value >= 90 else WARN if value >= 75 else ACCENT


class Metric(tk.Frame):
    def __init__(self, parent: tk.Widget, label: str):
        super().__init__(parent, bg=PANEL)
        line = tk.Frame(self, bg=PANEL)
        line.pack(fill="x")
        tk.Label(line, text=label, bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self.number = tk.Label(line, text="—", bg=PANEL, fg=TEXT, font=(FONT, 12, "bold"))
        self.number.pack(side="right")
        self.track = tk.Canvas(self, height=4, bg=PANEL, highlightthickness=0)
        self.track.pack(fill="x", pady=(7, 0))
        self.value = 0.0
        self.track.bind("<Configure>", lambda _: self.draw())

    def set(self, value: float, suffix: str = "%") -> None:
        self.value = value
        self.number.configure(text=f"{value:.0f}{suffix}", fg=color_for(value))
        self.draw()

    def clear(self) -> None:
        self.value = 0.0
        self.number.configure(text="—", fg=MUTED)
        self.draw()

    def draw(self) -> None:
        width = max(self.track.winfo_width(), 1)
        self.track.delete("all")
        self.track.create_rectangle(0, 0, width, 4, fill=TRACK, outline="")
        self.track.create_rectangle(0, 0, width * min(max(self.value, 0), 100) / 100,
                                    4, fill=color_for(self.value), outline="")


class ServerCard(tk.Frame):
    def __init__(self, parent: tk.Widget, server: Server):
        super().__init__(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.server = server
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", padx=14, pady=6)
        identity = tk.Frame(row, bg=PANEL, width=164, height=42)
        identity.pack(side="left")
        identity.pack_propagate(False)
        icon = tk.Canvas(identity, width=28, height=30, bg=PANEL, highlightthickness=0)
        icon.pack(side="left", padx=(0, 9))
        for y in (5, 15):
            icon.create_rectangle(2, y, 25, y + 8, fill="#E6F2F2", outline="#B5D8D6")
            icon.create_oval(5, y + 3, 7, y + 5, fill=ACCENT, outline="")
        names = tk.Frame(identity, bg=PANEL)
        names.pack(side="left", fill="y")
        tk.Label(names, text=server.name, bg=PANEL, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(anchor="w")
        self.sub = tk.Label(names, text=server.host, bg=PANEL, fg=MUTED, font=(FONT, 8))
        self.sub.pack(anchor="w", pady=(1, 0))
        self.status = tk.Label(row, text="● 等待", bg=PANEL, fg=MUTED,
                               width=7, anchor="e", font=(FONT, 9, "bold"))
        self.status.pack(side="right", padx=(8, 0))
        body = tk.Frame(row, bg=PANEL)
        body.pack(side="left", fill="x", expand=True)
        self.metrics = {name: Metric(body, label) for name, label in
                        (("cpu", "CPU"), ("memory", "内存"), ("disk", "磁盘"), ("gpu", "GPU"))}
        for column, name in enumerate(("cpu", "memory", "disk", "gpu")):
            body.grid_columnconfigure(column, weight=1, uniform="metric")
            self.metrics[name].grid(row=0, column=column, sticky="ew", padx=6)
        self.foot = tk.Label(self, text="", bg=PANEL, fg=MUTED,
                             font=(FONT, 8), anchor="w")

    def update_data(self, metrics: dict[str, float | int] | None, error: str) -> None:
        if metrics is None:
            self.status.configure(text="● 离线", fg=BAD)
            for metric in self.metrics.values():
                metric.clear()
            self.foot.configure(text=error or "无法连接", fg=BAD)
            self.foot.pack(fill="x", padx=16, pady=(0, 8))
            self.sub.configure(text=self.server.host)
            return
        peak = max(float(metrics[name]) for name in ("cpu", "memory", "disk", "gpu")
                   if name in metrics)
        self.status.configure(text="● 告警" if peak >= 90 else "● 在线",
                              fg=BAD if peak >= 90 else ACCENT)
        for name in ("cpu", "memory", "disk"):
            self.metrics[name].set(float(metrics[name]))
        if "gpu" in metrics:
            self.metrics["gpu"].set(float(metrics["gpu"]))
        else:
            self.metrics["gpu"].clear()
        days = int(metrics["uptime"]) // 86400
        self.sub.configure(text=f"{self.server.host} · {days} 天")
        self.foot.pack_forget()


class App(tk.Tk):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.title("Server Desk · 服务器监控")
        window_height = min(800, max(520, self.winfo_screenheight() - 100))
        self.geometry(f"690x{window_height}")
        self.minsize(620, 420)
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.demo = demo
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_reload = False
        self.poll_timer: str | None = None
        self.closed = False
        self.interval = 20
        self.servers: list[Server] = []
        self.cards: dict[str, ServerCard] = {}
        self.states: dict[str, tuple[dict[str, float | int] | None, str]] = {}
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="server-desk")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.build_shell()
        self.reload()
        self.after(100, self.drain_events)

    def build_shell(self) -> None:
        top = tk.Frame(self, bg=HEADER)
        top.pack(fill="x")
        upper = tk.Frame(top, bg=HEADER)
        upper.pack(fill="x", padx=20, pady=(15, 12))
        title = tk.Frame(upper, bg=HEADER)
        title.pack(side="left")
        tk.Label(title, text="SERVER DESK", bg=HEADER, fg="#F4FBFD",
                 font=(FONT, 19, "bold")).pack(anchor="w")
        self.summary = tk.Label(title, text="服务器状态", bg=HEADER, fg="#ABC7D1", font=(FONT, 9))
        self.summary.pack(anchor="w")
        actions = tk.Frame(upper, bg=HEADER)
        actions.pack(side="right")
        self.pin = self.button(actions, "置顶 ✓", self.toggle_pin)
        self.pin.pack(side="left", padx=(0, 5))
        self.button(actions, "刷新", self.refresh).pack(side="left")

        stats = tk.Frame(top, bg=HEADER)
        stats.pack(fill="x", padx=20, pady=(0, 15))
        self.stat_values = {}
        for key, label in (("total", "监控节点"), ("online", "在线节点"), ("attention", "需要关注")):
            box = tk.Frame(stats, bg=PANEL_2, padx=14, pady=8)
            box.pack(side="left", fill="x", expand=True, padx=(0, 7) if key != "attention" else (0, 0))
            value = tk.Label(box, text="—", bg=PANEL_2, fg="#F4FBFD",
                             font=(FONT, 17, "bold"))
            value.pack(side="left")
            tk.Label(box, text=label, bg=PANEL_2, fg="#BED3DA",
                     font=(FONT, 9)).pack(side="left", padx=(10, 0), pady=(5, 0))
            self.stat_values[key] = value

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.scroll = tk.Canvas(self, width=9, bg=BG, highlightthickness=0, cursor="hand2")
        self.scroll_range = (0.0, 1.0)
        self.canvas.configure(yscrollcommand=self.draw_scrollbar)
        self.scroll.pack(side="right", fill="y")
        self.scroll.bind("<Configure>", lambda _: self.draw_scrollbar(*self.scroll_range))
        self.scroll.bind("<Button-1>", self.scroll_click)
        self.scroll.bind("<B1-Motion>", self.scroll_click)
        self.canvas.pack(fill="both", expand=True)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.window_id = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self.on_scroll)

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=18, pady=(8, 14))
        self.updated = tk.Label(bottom, text="尚未采集", bg=BG, fg=MUTED, font=(FONT, 9))
        self.updated.pack(side="left")
        self.button(bottom, "配置服务器", self.open_config).pack(side="right")

    def button(self, parent: tk.Widget, label: str, command) -> tk.Button:
        return tk.Button(parent, text=label, command=command, bg=PANEL_2, fg="#FFFFFF",
                         activebackground="#2A5B6D", activeforeground="#FFFFFF",
                         relief="flat", borderwidth=0, padx=10, pady=6,
                         cursor="hand2", font=(FONT, 9))

    def on_scroll(self, event) -> None:
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def draw_scrollbar(self, first: str | float, last: str | float) -> None:
        start, end = float(first), float(last)
        self.scroll_range = (start, end)
        height = max(self.scroll.winfo_height(), 1)
        self.scroll.delete("all")
        if end - start < 1:
            self.scroll.create_rectangle(2, height * start, 7, height * end,
                                         fill="#557184", outline="")

    def scroll_click(self, event) -> None:
        shown = self.scroll_range[1] - self.scroll_range[0]
        target = event.y / max(self.scroll.winfo_height(), 1) - shown / 2
        self.canvas.yview_moveto(max(0.0, min(1.0 - shown, target)))

    def toggle_pin(self) -> None:
        current = bool(self.attributes("-topmost"))
        self.attributes("-topmost", not current)
        self.pin.configure(text="置顶 ✓" if not current else "置顶")

    def open_config(self) -> None:
        if not CONFIG.exists():
            CONFIG.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        os.startfile(CONFIG)
        messagebox.showinfo("配置服务器", "保存 servers.json 后点击“刷新”，即可重新加载服务器列表。")

    def reload(self) -> None:
        if self.poll_timer is not None:
            self.after_cancel(self.poll_timer)
            self.poll_timer = None
        for card in self.cards.values():
            card.destroy()
        self.cards.clear()
        self.states.clear()
        self.update_overview()
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        if self.demo:
            self.servers = [Server("训练节点 A", "demo-a", "demo"),
                            Server("存储节点 B", "demo-b", "demo"),
                            Server("推理节点 C", "demo-c", "demo")]
            self.interval = 20
        elif not CONFIG.exists():
            self.servers = []
            self.empty("还没有服务器", "点击右下角“配置服务器”，填写 SSH 地址和用户。")
            self.summary.configure(text="等待配置 · 尚无真实数据")
            self.update_overview()
            return
        else:
            try:
                self.servers, self.interval = load_config()
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self.servers = []
                self.empty("配置有误", str(exc))
                self.summary.configure(text="无法加载配置")
                self.update_overview()
                return
        if not self.servers:
            self.empty("服务器列表为空", "请在 servers.json 中添加服务器。")
            self.summary.configure(text="等待配置 · 尚无真实数据")
            self.update_overview()
            return
        for server in self.servers:
            card = ServerCard(self.list_frame, server)
            card.pack(fill="x", padx=14, pady=(0, 6))
            self.cards[server.name] = card
        self.update_overview()
        self.summary.configure(text=f"{len(self.servers)} 台服务器 · " + ("演示数据" if self.demo else "真实采集"))
        self.start_poll()

    def update_overview(self) -> None:
        if not hasattr(self, "stat_values"):
            return
        self.stat_values["total"].configure(text=f"{len(self.servers):02}")
        online = sum(metrics is not None for metrics, _ in self.states.values())
        attention = 0
        for metrics, _ in self.states.values():
            if metrics is None or max(float(value) for name, value in metrics.items()
                                   if name in ("cpu", "memory", "disk", "gpu")) >= 90:
                attention += 1
        self.stat_values["online"].configure(text=f"{online:02}")
        self.stat_values["attention"].configure(text=f"{attention:02}",
                                                 fg="#FFBFC6" if attention else "#F4FBFD")

    def empty(self, title: str, detail: str) -> None:
        box = tk.Frame(self.list_frame, bg=PANEL, padx=18, pady=22)
        box.pack(fill="x", padx=18, pady=8)
        tk.Label(box, text=title, bg=PANEL, fg=TEXT, font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(box, text=detail, bg=PANEL, fg=MUTED, font=(FONT, 10),
                 justify="left", wraplength=340).pack(anchor="w", pady=(7, 0))

    def refresh(self) -> None:
        if self.busy:
            self.pending_reload = True
            self.updated.configure(text="本次采集结束后刷新…")
        else:
            self.reload()

    def start_poll(self) -> None:
        self.poll_timer = None
        if self.busy or not self.servers or self.closed:
            return
        self.busy = True
        self.updated.configure(text="采集中…")
        threading.Thread(target=self.collect, daemon=True).start()

    def collect(self) -> None:
        try:
            if self.demo:
                values = [
                    {"cpu": 37.0, "memory": 68.0, "memory_total_gb": 256.0, "disk": 42.0,
                     "uptime": 864000, "gpu": 82.0, "gpu_used_mb": 52000, "gpu_total_mb": 81920, "gpu_count": 2},
                    {"cpu": 16.0, "memory": 51.0, "memory_total_gb": 64.0, "disk": 91.0, "uptime": 1728000},
                    None,
                ]
                for server, value in zip(self.servers, values):
                    self.events.put((server.name, value, "演示：SSH 连接失败" if value is None else ""))
            else:
                futures = {self.pool.submit(poll_server, server): server.name for server in self.servers}
                for future in as_completed(futures):
                    try:
                        metrics, error = future.result()
                    except Exception as exc:
                        metrics, error = None, f"采集失败: {type(exc).__name__}"
                    self.events.put((futures[future], metrics, error))
        finally:
            self.events.put(("__done__", None, ""))

    def drain_events(self) -> None:
        try:
            while True:
                name, metrics, error = self.events.get_nowait()
                if name == "__done__":
                    self.busy = False
                    self.updated.configure(text="更新于 " + time.strftime("%H:%M:%S") +
                                           (" · 演示" if self.demo else ""))
                    if self.pending_reload:
                        self.pending_reload = False
                        self.reload()
                    else:
                        self.poll_timer = self.after(self.interval * 1000, self.start_poll)
                elif name in self.cards:
                    self.cards[name].update_data(metrics, error)
                    self.states[name] = (metrics, error)
                    self.update_overview()
        except queue.Empty:
            pass
        if not self.closed:
            self.after(100, self.drain_events)

    def close(self) -> None:
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Server Desk: Linux SSH 桌面监控")
    parser.add_argument("--demo", action="store_true", help="显示明确标记的演示数据")
    args = parser.parse_args()
    App(demo=args.demo).mainloop()


if __name__ == "__main__":
    main()