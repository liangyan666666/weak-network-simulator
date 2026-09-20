# -*- coding: utf-8 -*-
"""
弱网模拟器 (Weak Network Simulator)
基于 WinDivert (pydivert) 按进程/全局对 TCP/UDP 流量做:
  上行/下行 延时、延时抖动、带宽限速、随机丢包、周期连丢
快捷键:
  HOME        隐藏/显示窗口
  自定义热键  启动/停止 (可在界面修改绑定)
仅用于弱网测试。需要以管理员身份运行。
"""
import os, sys, json, time, threading, heapq, random, traceback, copy, socket, struct, re
try:
    import winsound
except Exception:
    winsound = None

import tkinter as tk
from tkinter import ttk, messagebox
import psutil

from pydivert import WinDivert
from pydivert.consts import Layer, Flag
import win32gui, win32process, win32api, win32con

try:
    import keyboard
except Exception:
    keyboard = None

# 打包成单 exe 后,配置文件写到 exe 所在目录;开发时写到脚本目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# ---------- 深色主题 ----------
BG        = "#1b1c2b"
BG_PANEL  = "#252740"
BG_ENTRY  = "#0f1020"
FG        = "#e6e6f0"
FG_DIM    = "#9a9ab0"
ACCENT    = "#7c5cff"
ACCENT_HV = "#947dff"
UPLINK_C  = "#e8912d"
DOWNLINK_C= "#2ecc71"
DANGER    = "#e74c3c"
OK_GREEN  = "#27ae60"

UNIT_FACTOR = {"B/s": 1, "KB/s": 1024, "MB/s": 1024*1024}


# ============================================================
# 弱网引擎
# ============================================================
class DirParams:
    __slots__ = ("delay_ms", "jitter_ms", "bandwidth_Bps", "loss_pct",
                 "burst_pass", "burst_drop")
    def __init__(self):
        self.delay_ms = 0
        self.jitter_ms = 0
        self.bandwidth_Bps = 0      # 0 = 不限速
        self.loss_pct = 0.0
        self.burst_pass = 0
        self.burst_drop = 0


class HeldPacket:
    """延迟队列里持有一份独立拷贝(recv 缓冲区会被复用,必须拷贝)。"""
    __slots__ = ("raw", "wd_addr")
    def __init__(self, raw, wd_addr):
        self.raw = raw                 # bytearray
        self.wd_addr = wd_addr         # deepcopy 的 WinDivertAddress
    def recalculate_checksums(self):
        pass


class WeakNetEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.w = None
        self.running = False
        self._recv_thread = None
        self._flush_thread = None

        # 运行参数 (线程安全读取)
        self.target_name = None     # None = 全局;否则按进程名匹配(多进程软件的所有子进程)
        self.proto = "udp"          # udp / tcp / all
        self.up = DirParams()
        self.down = DirParams()
        self.duration_s = 0.0      # 0 = 永久

        # 内部状态
        self._heap = []             # (release_time, seq, held)
        self._seq = 0
        self._next_free_up = 0.0
        self._next_free_down = 0.0
        # 连丢计数器(按方向)
        self._bc_up = 0
        self._bp_up = 0             # 0=放行阶段 1=丢弃阶段
        self._bc_down = 0
        self._bp_down = 0
        # 连接表:五元组 -> PID(定时用 psutil 刷新)
        self._conn_map = {}
        self._pid_to_name = {}
        self._conn_thread = None

        self.start_time = 0.0
        self.stats_sent = 0
        self.stats_drop = 0
        self.on_stop = None         # 回调:超时/外部停止

    # ---------- 控制 ----------
    def start(self):
        if self.running:
            return
        proto_map = {"udp": "udp", "tcp": "tcp", "all": "tcp or udp"}
        filt = proto_map.get(self.proto, "udp")
        # 重置内部状态
        self._heap = []
        self._seq = 0
        now = time.monotonic()
        self._next_free_up = now
        self._next_free_down = now
        self._bc_up = self._bp_up = 0
        self._bc_down = self._bp_down = 0
        self._pid_map = {}
        self.stats_sent = 0
        self.stats_drop = 0
        self.start_time = now

        try:
            self.w = WinDivert(filt, layer=Layer.NETWORK)
            self.w.open()
            # 按进程模式:额外开 FLOW 句柄建立 五元组->PID 映射
        except Exception as e:
            try:
                if self.w is not None:
                    self.w.close()
            except Exception:
                pass
            self.w = None
            raise RuntimeError(
                "无法启动 WinDivert 过滤驱动:\n%s\n\n"
                "请确认:1) 已用管理员身份运行;2) 未被安全软件拦截。" % e)

        self.running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._recv_thread.start()
        self._flush_thread.start()
        if self.target_name is not None:
            self._conn_thread = threading.Thread(target=self._conn_loop, daemon=True)
            self._conn_thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        # 关闭句柄,唤醒阻塞的 recv
        try:
            if self.w is not None:
                self.w.close()
        except Exception:
            pass
        # 等待线程(跳过当前线程,否则自动停止时 join 自己会报错)
        import threading as _th
        cur = _th.current_thread()
        for t in (self._recv_thread, self._flush_thread, self._conn_thread):
            if t is not None and t is not cur:
                t.join(timeout=2.0)
        # 把堆积的包全部放行,避免网络卡住
        self._drain_all()
        self.w = None

    def _drain_all(self):
        with self.lock:
            items = self._heap
            self._heap = []
        for _, _, pkt in items:
            try:
                self.w.send(pkt)
            except Exception:
                pass

    # ---------- 收包线程 ----------
    def _recv_loop(self):
        try:
            while self.running:
                try:
                    packet = self.w.recv()
                except Exception:
                    if not self.running:
                        break
                    continue
                try:
                    self._handle(packet)
                except Exception:
                    traceback.print_exc()
        except Exception:
            pass

    def _pass_now(self, packet):
        try:
            self.w.send(packet)
            self.stats_sent += 1
        except Exception:
            pass

    # ---------- 连接表:五元组 -> PID(用 psutil 定时刷新) ----------
    def _conn_loop(self):
        while self.running:
            try:
                m = {}
                p2n = {}
                for pr in psutil.process_iter(["pid", "name"]):
                    try:
                        if pr.info["name"]:
                            p2n[pr.info["pid"]] = pr.info["name"]
                    except Exception:
                        pass
                for c in psutil.net_connections(kind="inet"):
                    try:
                        if c.pid is None or not c.laddr or not c.raddr:
                            continue
                        proto = 6 if c.type == 1 else 17
                        key = (proto, c.laddr.ip, c.laddr.port,
                               c.raddr.ip, c.raddr.port)
                        m[key] = c.pid
                    except Exception:
                        continue
                with self.lock:
                    self._conn_map = m
                    self._pid_to_name = p2n
            except Exception:
                pass
            time.sleep(0.5)

    def _lookup_pid(self, packet, is_up):
        try:
            if packet.ipv4 is None:
                return False
            proto = int(packet.protocol[0])
            if is_up:
                lip, lport = str(packet.src_addr), int(packet.src_port)
                rip, rport = str(packet.dst_addr), int(packet.dst_port)
            else:
                lip, lport = str(packet.dst_addr), int(packet.dst_port)
                rip, rport = str(packet.src_addr), int(packet.src_port)
            key = (proto, lip, lport, rip, rport)
            with self.lock:
                pid = self._conn_map.get(key)
                if pid is None:
                    return False
                name = self._pid_to_name.get(pid)
            return name is not None and self.target_name is not None \
                and name.lower() == self.target_name.lower()
        except Exception:
            return False

    def _handle(self, packet):
        addr = packet.wd_addr
        # 回环包直接放行(避免影响本机代理/回环服务)
        if bool(getattr(addr, "Loopback", False)):
            self._pass_now(packet)
            return
        # 进程过滤(查 FLOW 映射;查不到一律放行,不误伤)
        if self.target_name is not None:
            if not self._lookup_pid(packet, bool(getattr(addr, "Outbound", False))):
                self._pass_now(packet)
                return

        is_up = bool(getattr(addr, "Outbound", False))
        p = self.up if is_up else self.down

        # 1) 随机丢包
        if p.loss_pct > 0 and random.uniform(0, 100) < p.loss_pct:
            self.stats_drop += 1
            return

        # 2) 周期连丢 (放行 N 个 -> 丢弃 M 个 -> 循环)
        if p.burst_pass > 0 or p.burst_drop > 0:
            if is_up:
                if self._bp_up == 0:           # 放行阶段
                    self._bc_up += 1
                    if self._bc_up >= p.burst_pass:
                        self._bp_up = 1
                        self._bc_up = 0
                else:                          # 丢弃阶段
                    self._bc_up += 1
                    if self._bc_up >= p.burst_drop:
                        self._bp_up = 0
                        self._bc_up = 0
                    else:
                        self.stats_drop += 1
                        return
            else:
                if self._bp_down == 0:
                    self._bc_down += 1
                    if self._bc_down >= p.burst_pass:
                        self._bp_down = 1
                        self._bc_down = 0
                else:
                    self._bc_down += 1
                    if self._bc_down >= p.burst_drop:
                        self._bp_down = 0
                        self._bc_down = 0
                    else:
                        self.stats_drop += 1
                        return

        # 3) 计算放行时刻:限速令牌桶
        now = time.monotonic()
        release = now
        if p.bandwidth_Bps and p.bandwidth_Bps > 0:
            nxt = self._next_free_up if is_up else self._next_free_down
            earliest = max(now, nxt)
            release = earliest
            new_next = earliest + len(packet.raw) / float(p.bandwidth_Bps)
            if is_up:
                self._next_free_up = new_next
            else:
                self._next_free_down = new_next

        # 4) 叠加延时 + 抖动
        delay_s = p.delay_ms / 1000.0
        jit_s = random.uniform(-p.jitter_ms, p.jitter_ms) / 1000.0
        release += delay_s + jit_s
        if release < now:
            release = now

        # recv 缓冲区会被复用,必须独立拷贝
        held = HeldPacket(bytearray(packet.raw), copy.deepcopy(packet.wd_addr))
        with self.lock:
            heapq.heappush(self._heap, (release, self._seq, held))
            self._seq += 1

    # ---------- 送出线程 ----------
    def _flush_loop(self):
        while self.running:
            # 时长到点
            if self.duration_s > 0 and (time.monotonic() - self.start_time) >= self.duration_s:
                cb = self.on_stop
                self.stop()
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                break
            now = time.monotonic()
            sent_any = False
            with self.lock:
                while self._heap and self._heap[0][0] <= now:
                    _, _, pkt = heapq.heappop(self._heap)
                    try:
                        self.w.send(pkt)
                        self.stats_sent += 1
                        sent_any = True
                    except Exception:
                        pass
            time.sleep(0.001)


# ============================================================
# GUI
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("弱网环境模拟器")
        self.geometry("620x360")
        self.configure(bg=BG)
        self.minsize(580, 330)
        # 锁死窗口大小,禁止鼠标拖拽改变宽高
        self.resizable(False, False)

        self.engine = WeakNetEngine()
        self.engine.on_stop = self._on_engine_auto_stop

        self._proc_list = {}     # display_text -> pid
        self._hotkey_handler = None
        self._binding = False
        self._bound_mouse_vk = None
        self._mouse_watch_thread = None
        self._home_handler = None

        self._build_style()
        self._build_ui()
        self._load_config()
        self._refresh_procs()
        self.after(3000, self._refresh_procs_loop)
        self._register_home()
        self._update_status()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式 ----------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=BG_ENTRY, background=BG_PANEL,
                        foreground=FG, arrowcolor=FG, borderwidth=0,
                        selectbackground=BG_ENTRY, selectforeground=FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_ENTRY), ("disabled", BG_PANEL)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("!focus", BG_ENTRY)],
                  selectforeground=[("!focus", FG)])

    def _mk_entry(self, parent, width=10, var=None):
        e = tk.Entry(parent, bg=BG_ENTRY, fg=FG, insertbackground=FG,
                     relief="flat", width=width, font=("Segoe UI", 9),
                     justify="center", textvariable=var)
        return e

    def _mk_button(self, parent, text, cmd, bg=ACCENT, fg="white", width=12):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=ACCENT_HV, activeforeground="white",
                      relief="flat", font=("Segoe UI", 10, "bold"),
                      width=width, cursor="hand2", bd=0)
        return b

    # ---------- UI 布局 ----------
    def _build_ui(self):
        # 顶部:进程选择
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=8, pady=(6, 2))

        tk.Label(top, text="目标进程:", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")

        row1 = tk.Frame(top, bg=BG)
        row1.pack(fill="x", pady=4)
        self.proc_var = tk.StringVar()
        self.proc_combo = ttk.Combobox(row1, textvariable=self.proc_var,
                                       state="readonly", font=("Segoe UI", 9),
                                       height=18)
        self.proc_combo.pack(side="left", fill="x", expand=True)

        self.pick_btn = tk.Button(row1, text="🎯", bg=BG_PANEL, fg=FG,
                                  relief="flat", font=("Segoe UI", 12),
                                  width=3, cursor="hand2", bd=0,
                                  activebackground=ACCENT)
        self.pick_btn.pack(side="left", padx=(6, 0))
        self.pick_btn.bind("<ButtonPress-1>", self._start_pick)

        # 参数行:协议 / 单位 / 时长
        row2 = tk.Frame(self, bg=BG)
        row2.pack(fill="x", padx=8, pady=2)

        tk.Label(row2, text="协议:", bg=BG, fg=FG_DIM).pack(side="left")
        self.proto_var = tk.StringVar(value="TCP+UDP")
        self.proto_combo = ttk.Combobox(row2, textvariable=self.proto_var, state="readonly", width=9,
                     values=["UDP", "TCP", "TCP+UDP"], font=("Segoe UI", 9))
        self.proto_combo.pack(side="left", padx=(4, 14))
        self.proto_combo.bind("<<ComboboxSelected>>", lambda e: self.focus_set())

        tk.Label(row2, text="单位:", bg=BG, fg=FG_DIM).pack(side="left")
        self.unit_var = tk.StringVar(value="B/s")
        self.unit_combo = ttk.Combobox(row2, textvariable=self.unit_var, state="readonly", width=7,
                     values=["B/s", "KB/s", "MB/s"], font=("Segoe UI", 9))
        self.unit_combo.pack(side="left", padx=(4, 14))
        self.unit_combo.bind("<<ComboboxSelected>>", lambda e: self.focus_set())

        tk.Label(row2, text="几秒后自动恢复网络:", bg=BG, fg=FG_DIM).pack(side="left")
        self.dur_var = tk.StringVar(value="60.0")
        self.dur_minus_btn = tk.Button(row2, text="-", width=3, bg=BG_PANEL, fg=FG, relief="flat",
                  command=lambda: self._bump_dur(-1))
        self.dur_minus_btn.pack(side="left", padx=2)
        self.dur_entry = tk.Entry(row2, textvariable=self.dur_var, width=8, bg=BG_ENTRY,
                                  fg=FG, justify="center", relief="flat", font=("Segoe UI", 9),
                                  disabledbackground=BG_PANEL, disabledforeground=FG_DIM)
        self.dur_entry.pack(side="left")
        self.dur_entry.bind("<Return>", lambda e: self.focus_set())
        self.dur_entry.bind("<Escape>", lambda e: self.focus_set())
        self.dur_plus_btn = tk.Button(row2, text="+", width=3, bg=BG_PANEL, fg=FG, relief="flat",
                  command=lambda: self._bump_dur(1))
        self.dur_plus_btn.pack(side="left", padx=2)
        self.perm_var = tk.IntVar(value=0)
        self.perm_chk = tk.Checkbutton(row2, text="不自动恢复网络", variable=self.perm_var, bg=BG, fg=FG,
                       selectcolor=BG_PANEL, activebackground=BG, activeforeground=FG)
        self.perm_chk.pack(side="left", padx=8)
        self.perm_var.trace_add("write", self._on_perm_toggle)

        # 中部:上行 / 下行
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="x", padx=8, pady=3)

        self.up_vars = self._build_link_panel(mid, "上行配置 (Uplink)", UPLINK_C)
        self.down_vars = self._build_link_panel(mid, "下行配置 (Downlink)", DOWNLINK_C)
        self._apply_default_params()

        # 底部:热键 + 启动
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=8, pady=(2, 0))

        tk.Label(bot, text="启停热键:", bg=BG, fg=FG_DIM).pack(side="left")
        self.hk_label = tk.Label(bot, text="[未绑定]", bg=BG, fg=ACCENT,
                                 font=("Consolas", 10, "bold"))
        self.hk_label.pack(side="left", padx=6)
        self.hk_btn = self._mk_button(bot, "修改绑定", self._start_binding,
                                      bg=BG_PANEL, fg=FG, width=16)
        self.hk_btn.pack(side="left", padx=4)
        self._mk_button(bot, "保存配置", self._save_config, bg=BG_PANEL,
                        fg=FG, width=9).pack(side="left", padx=4)
        self._mk_button(bot, "重置参数", self._reset_params, bg=BG_PANEL,
                        fg=FG, width=9).pack(side="left", padx=4)

        self.start_btn = tk.Button(bot, text="启  动", command=self.toggle_engine,
                                   bg=OK_GREEN, fg="white", relief="flat",
                                   font=("Segoe UI", 11, "bold"), height=1,
                                   cursor="hand2", bd=0)
        self.start_btn.pack(side="left", padx=(12, 0), ipadx=18)

        # 状态栏
        self.bind("<Button-1>", self._click_clear_focus)
        self.status_var = tk.StringVar(value="就绪  |  HOME 键可隐藏/显示窗口")
        tk.Label(self, textvariable=self.status_var, bg="#12131f", fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x", side="bottom")

    def _build_link_panel(self, parent, title, color):
        panel = tk.LabelFrame(parent, text="  " + title + "  ", bg=BG_PANEL, fg=color,
                              font=("Segoe UI", 10, "bold"), bd=1, relief="solid",
                              padx=8, pady=4)
        panel.pack(side="left", fill="both", expand=True, padx=6)
        fields = [
            ("delay",  "延时(ms):",        8, "0"),
            ("jitter", "延时抖动(ms):",    8, "0"),
            ("bw",     "带宽:",           10, "0"),
            ("loss",   "随机丢包(%):",     8, "0"),
            ("bpass",  "连丢(放行)(个):",  8, "0"),
            ("bdrop",  "连丢(丢弃)(个):",  8, "0"),
        ]
        vars_ = {}
        for key, label, w, default in fields:
            row = tk.Frame(panel, bg=BG_PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=BG_PANEL, fg=FG, width=12,
                     anchor="e").pack(side="left")
            v = tk.StringVar(value=default)
            vars_[key] = v
            e = tk.Entry(row, textvariable=v, width=w, bg=BG_ENTRY, fg=FG,
                         justify="center", relief="flat", font=("Segoe UI", 9))
            e.pack(side="left", padx=6)
            if key == "bw":
                tk.Label(row, text="(按上方单位)", bg=BG_PANEL, fg=FG_DIM).pack(side="left")
        return vars_

    # ---------- 进程列表 ----------
    # ---------- 窗口拾取 ----------
    def _start_pick(self, event):
        self._picking = True
        self.pick_btn.configure(text="...")
        self.after(50, self._poll_pick)

    def _poll_pick(self):
        if not getattr(self, "_picking", False):
            return
        pid = 0
        try:
            pos = win32api.GetCursorPos()
            hwnd = win32gui.WindowFromPoint(pos)
            hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            self.status_var.set("正在拾取: pid=%d  %s" % (pid, title[:40]))
        except Exception:
            pass
        if not (win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000):
            self._picking = False
            self.pick_btn.configure(text="🎯")
            if pid:
                self._select_pid(pid)
            return
        self.after(50, self._poll_pick)

    def _select_pid(self, pid):
        for txt, p in self._proc_list.items():
            if p == pid:
                self.proc_var.set(txt)
                self.status_var.set("已选中进程: %s" % txt)
                return
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = "unknown.exe"
        txt = "%s  (pid=%d)" % (name, pid)
        self._proc_list[txt] = pid
        values = list(self.proc_combo["values"])
        if txt not in values:
            values.insert(1, txt)
            self.proc_combo["values"] = values
        self.proc_var.set(txt)
        self.status_var.set("已选中进程: %s (pid=%d)" % (name, pid))

    def _refresh_procs_loop(self):
        self._refresh_procs()
        self.after(3000, self._refresh_procs_loop)

    def _refresh_procs(self):
        procs = []
        seen = {}
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                name = p.info["name"]
                if not name:
                    continue
                mem = p.info["memory_info"].rss / (1024*1024) if p.info["memory_info"] else 0
                txt = "%s  (pid=%d, %d MB)" % (name, p.info["pid"], int(mem))
                procs.append((mem, txt, p.info["pid"]))
            except Exception:
                continue
        procs.sort(key=lambda x: -x[0])
        self._proc_list = {txt: pid for _, txt, pid in procs}
        values = ["(全局模式 - 所有进程)"] + [txt for _, txt, _ in procs[:200]]
        cur = self.proc_var.get()
        # 按 pid 保留当前选择(内存大小会变,不能按整段文本匹配)
        cur_pid = None
        mm = re.search(r"pid=(\d+)", cur)
        if mm:
            cur_pid = int(mm.group(1))
        keep = None
        if cur_pid is not None:
            for txt, pid in self._proc_list.items():
                if pid == cur_pid:
                    keep = txt
                    break
        self.proc_combo["values"] = values
        if keep is not None:
            self.proc_var.set(keep)
        elif cur not in values:
            self.proc_var.set(values[0])

    # ---------- 参数读取 ----------
    def _f(self, var, default=0.0):
        try:
            return float(var.get())
        except Exception:
            return default

    def _read_link(self, vars_):
        d = DirParams()
        d.delay_ms = self._f(vars_["delay"])
        d.jitter_ms = abs(self._f(vars_["jitter"]))
        bw_val = self._f(vars_["bw"])
        factor = UNIT_FACTOR.get(self.unit_var.get(), 1)
        d.bandwidth_Bps = bw_val * factor
        d.loss_pct = self._f(vars_["loss"])
        d.burst_pass = int(self._f(vars_["bpass"]))
        d.burst_drop = int(self._f(vars_["bdrop"]))
        return d

    def _collect_params(self):
        self.engine.up = self._read_link(self.up_vars)
        self.engine.down = self._read_link(self.down_vars)
        self.engine.proto = {"UDP": "udp", "TCP": "tcp",
                             "TCP+UDP": "all"}.get(self.proto_var.get(), "udp")
        sel = self.proc_var.get()
        if sel.startswith("(全局模式"):
            self.engine.target_name = None
        else:
            name = sel.split("  (")[0].strip()
            self.engine.target_name = name or None
        # 时长
        if self.perm_var.get():
            self.engine.duration_s = 0.0
        else:
            self.engine.duration_s = self._f(self.dur_var)

    # ---------- 启动/停止 ----------
    def toggle_engine(self):
        if self.engine.running:
            self._stop_engine()
        else:
            self._start_engine()

    def _start_engine(self):
        self._collect_params()
        try:
            self.engine.start()
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            return
        self.start_btn.configure(text="停  止", bg=DANGER)
        self._set_inputs_state("disabled")
        self._update_status()

    def _stop_engine(self):
        self.engine.stop()
        self.start_btn.configure(text="启  动", bg=OK_GREEN)
        self._set_inputs_state("normal")
        self._update_status()

    def _on_engine_auto_stop(self):
        # 在引擎线程触发,切回主线程
        self.after(0, self._auto_stop_ui)

    def _auto_stop_ui(self):
        self.start_btn.configure(text="启  动", bg=OK_GREEN)
        self._set_inputs_state("normal")
        self.status_var.set("时长到,已自动停止。")

    def _set_inputs_state(self, state):
        # 运行时锁定参数修改(除启动按钮)
        for combo in (self.proc_combo,):
            try:
                combo.configure(state=state if state == "normal" else "disabled")
            except Exception:
                pass

    def _update_status(self):
        if self.engine.running:
            e = self.engine
            tgt = "全局" if e.target_name is None else str(e.target_name)
            self.status_var.set(
                "运行中 [%s | %s]  已送包:%d  已丢包:%d   (HOME 隐藏窗口)" %
                (e.proto, tgt, e.stats_sent, e.stats_drop))
            self.after(800, self._update_status)
        else:
            self.status_var.set("就绪  |  HOME 键隐藏/显示窗口")

    # ---------- 快捷键 ----------
    def _register_home(self):
        if keyboard is None:
            return
        try:
            self._home_handler = keyboard.add_hotkey("home", self._on_home)
        except Exception:
            pass

    def _on_home(self):
        self.after(0, self.toggle_visible)

    def toggle_visible(self):
        if self.state() == "withdrawn" or not self.winfo_viewable():
            self.deiconify()
        else:
            self.withdraw()

    def _start_binding(self):
        if keyboard is None:
            messagebox.showwarning("提示", "keyboard 库不可用,无法绑定热键。")
            return
        if self._binding:
            return
        self._binding = True
        self.hk_btn.configure(text="请按键或鼠标键...")
        self._binding_hook = keyboard.hook(self._on_bind_event)
        threading.Thread(target=self._mouse_bind_poll, daemon=True).start()

    def _on_bind_event(self, event):
        if event.event_type != "down":
            return
        key = event.name
        if key in ("shift", "ctrl", "alt", "windows", "left shift", "right shift"):
            return  # 忽略单独的修饰键
        self.after(0, lambda: self._finish_binding(key))

    MOUSE_NAMES = {0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键",
                   0x05: "鼠标侧键1", 0x06: "鼠标侧键2"}
    MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)

    def _mouse_bind_poll(self):
        # 绑定模式:轮询鼠标键,只检测新按下(避开点"修改绑定"那次点击)
        time.sleep(0.3)
        prev = {vk: bool(win32api.GetAsyncKeyState(vk) & 0x8000) for vk in self.MOUSE_VKS}
        while self._binding:
            for vk in self.MOUSE_VKS:
                cur = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                if cur and not prev[vk]:
                    self.after(0, lambda v=vk: self._finish_binding("mouse:0x%02x" % v))
                    return
                prev[vk] = cur
            time.sleep(0.02)

    def _mouse_watch(self):
        vk = self._bound_mouse_vk
        prev = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
        while self._bound_mouse_vk == vk:
            cur = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
            if cur and not prev:
                self.after(0, self.toggle_engine)
            prev = cur
            time.sleep(0.02)

    def _finish_binding(self, key):
        self._binding = False
        try:
            keyboard.unhook(self._binding_hook)
        except Exception:
            pass
        self.hk_btn.configure(text="修改绑定")
        # 注销旧热键
        if self._hotkey_handler is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handler)
            except Exception:
                pass
        self._bound_mouse_vk = None
        # HOME 不作为启停热键
        if key == "home":
            self.hk_label.configure(text="[HOME 已用作显隐,请换键]")
            return
        try:
            if key.startswith("mouse:"):
                vk = int(key.split(":")[1], 16)
                self._bound_mouse_vk = vk
                self._mouse_watch_thread = threading.Thread(target=self._mouse_watch, daemon=True)
                self._mouse_watch_thread.start()
                self.hk_label.configure(text="[%s]" % self.MOUSE_NAMES.get(vk, "鼠标键"))
                self._hotkey_name = key
            else:
                self._hotkey_handler = keyboard.add_hotkey(key, self._on_hotkey_trigger)
                self.hk_label.configure(text="[%s]" % key.upper())
                self._hotkey_name = key
        except Exception as e:
            self.hk_label.configure(text="[绑定失败]")

    def _on_hotkey_trigger(self):
        self.after(0, self.toggle_engine)

    def _play_sound(self, kind):
        # 启动=上扬两音,停止=下降两音;后台播放不卡 UI
        if winsound is None:
            return
        def run():
            try:
                if kind == "start":
                    winsound.Beep(880, 90)
                    winsound.Beep(1320, 140)
                elif kind == "stop":
                    winsound.Beep(660, 90)
                    winsound.Beep(440, 200)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    # ---------- 工具按钮 ----------
    def _click_clear_focus(self, event=None):
        # 点空白处时收走焦点;点在输入框/下拉上不抢,保证能编辑
        if event is not None:
            w = getattr(event, "widget", None)
            if isinstance(w, (tk.Entry, ttk.Combobox, tk.Button, tk.Checkbutton)):
                return
        try:
            self.focus_set()
        except Exception:
            pass

    def _on_perm_toggle(self, *args):
        # 勾选“不自动恢复网络”时禁用时长输入
        disabled = bool(self.perm_var.get())
        st = "disabled" if disabled else "normal"
        for w in (getattr(self, "dur_entry", None),
                  getattr(self, "dur_minus_btn", None),
                  getattr(self, "dur_plus_btn", None)):
            try:
                w.configure(state=st)
            except Exception:
                pass

    def _bump_dur(self, delta):
        try:
            v = float(self.dur_var.get()) + delta
            if v < 0:
                v = 0
            self.dur_var.set("%.1f" % v)
        except Exception:
            self.dur_var.set("60.0")

    def _apply_default_params(self):
        # 上行/下行所有参数默认全为 0
        for vars_ in (self.up_vars, self.down_vars):
            vars_["delay"].set("0")
            vars_["jitter"].set("0")
            vars_["bw"].set("0")
            vars_["loss"].set("0")
            vars_["bpass"].set("0")
            vars_["bdrop"].set("0")

    def _reset_params(self):
        self._apply_default_params()

    # ---------- 配置存取 ----------
    def _save_config(self):
        cfg = {
            "proto": self.proto_var.get(),
            "unit": self.unit_var.get(),
            "duration": self.dur_var.get(),
            "permanent": bool(self.perm_var.get()),
            "hotkey": getattr(self, "_hotkey_name", ""),
            "up": {k: v.get() for k, v in self.up_vars.items()},
            "down": {k: v.get() for k, v in self.down_vars.items()},
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self.status_var.set("配置已保存到 config.json")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.proto_var.set(cfg.get("proto", "UDP"))
            self.unit_var.set(cfg.get("unit", "B/s"))
            self.dur_var.set(cfg.get("duration", "60.0"))
            self.perm_var.set(1 if cfg.get("permanent") else 0)
            for k, v in cfg.get("up", {}).items():
                if k in self.up_vars:
                    self.up_vars[k].set(v)
            for k, v in cfg.get("down", {}).items():
                if k in self.down_vars:
                    self.down_vars[k].set(v)
            hk = cfg.get("hotkey", "")
            if hk:
                try:
                    if hk.startswith("mouse:"):
                        vk = int(hk.split(":")[1], 16)
                        self._bound_mouse_vk = vk
                        self._mouse_watch_thread = threading.Thread(
                            target=self._mouse_watch, daemon=True)
                        self._mouse_watch_thread.start()
                        self._hotkey_name = hk
                        self.hk_label.configure(
                            text="[%s]" % self.MOUSE_NAMES.get(vk, "鼠标键"))
                    elif keyboard is not None:
                        self._hotkey_handler = keyboard.add_hotkey(
                            hk, self._on_hotkey_trigger)
                        self._hotkey_name = hk
                        self.hk_label.configure(text="[%s]" % hk.upper())
                except Exception:
                    pass
        except Exception:
            pass

    def _on_close(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            if keyboard is not None:
                if self._home_handler:
                    keyboard.remove_hotkey(self._home_handler)
                if self._hotkey_handler:
                    keyboard.remove_hotkey(self._hotkey_handler)
        except Exception:
            pass
        self.destroy()


def main():
    # 必须管理员权限
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            # 提示后仍启动(由用户自行处理)
            pass
    except Exception:
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
