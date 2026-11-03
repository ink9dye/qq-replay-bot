"""Tkinter 桌面界面：配置、状态、任务表、日志。"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk

from .client import WatchClient, format_bytes
from .config import (AccountConfig, AppConfig, ConfigStore, MAX_ACCOUNTS, MAX_CAPACITY,
                     normalize_server, validate)
from .paths import APP_TITLE, APP_VERSION

BG = "#F3F6FB"
CARD = "#FFFFFF"
BORDER = "#E4EAF2"
TEXT = "#122033"
MUTED = "#667085"
FAINT = "#98A2B3"
PRIMARY = "#2563EB"
PRIMARY_DARK = "#1D4ED8"
PRIMARY_SOFT = "#EFF4FF"
SUCCESS = "#12B76A"
SUCCESS_SOFT = "#ECFDF3"
WARNING = "#F79009"
WARNING_SOFT = "#FFFAEB"
DANGER = "#F04438"
DANGER_SOFT = "#FEF3F2"
HEADER = "#0B1220"
HEADER_SOFT = "#111C2E"
HEADER_LINE = "#243147"

FONT = "Microsoft YaHei UI"
MONO = "Cascadia Mono"

STATE_STYLE = {
    "stopped": ("已停止", FAINT),
    "starting": ("正在连接", "#FDB022"),
    "authenticating": ("正在登录萌卡", "#FDB022"),
    "running": ("在线待命", "#32D583"),
    "reconnecting": ("网络重连中", "#FDB022"),
    "stopping": ("正在停止", "#FDB022"),
    "error": ("启动失败", "#FDA29B"),
}

LEVEL_STYLE = {
    "info": ("#CBD5E1", "log_info"),
    "success": ("#6EE7B7", "log_success"),
    "warn": ("#FCD34D", "log_warn"),
    "error": ("#FCA5A5", "log_error"),
}


def _check_font(name: str, fallback: str) -> str:
    try:
        families = set(tkfont.families())
    except Exception:
        return fallback
    return name if name in families else fallback


class StatCard(tk.Frame):
    def __init__(self, master, title: str, value: str = "—", sub: str = ""):
        super().__init__(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.columnconfigure(0, weight=1)
        self._title = tk.Label(self, text=title, bg=CARD, fg=MUTED, font=(FONT, 8),
                               anchor="w")
        self._title.grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 0))
        self._value = tk.Label(self, text=value, bg=CARD, fg=TEXT, font=(FONT, 18, "bold"),
                               anchor="w")
        self._value.grid(row=1, column=0, sticky="ew", padx=12)
        self._sub = tk.Label(self, text=sub, bg=CARD, fg=FAINT, font=(FONT, 8),
                             anchor="w")
        self._sub.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

    def set(self, value: str, sub: str | None = None, color: str | None = None) -> None:
        self._value.configure(text=value)
        if color:
            self._value.configure(fg=color)
        if sub is not None:
            self._sub.configure(text=sub)


class App:
    def __init__(self, root: tk.Tk, store: ConfigStore, config: AppConfig,
                 data_dir: Path, warnings: list[str] | None = None, smoke: bool = False):
        self.root = root
        self.store = store
        self.config = config
        self.data_dir = Path(data_dir)
        self.smoke = smoke
        self.bus: queue.Queue[dict] = queue.Queue()
        self.client: WatchClient | None = None
        self._jobs: list[dict] = []
        self._stats: dict = {}
        self._state = "stopped"
        self._state_detail = ""
        self._closing = False
        self._destroyed = False
        self._testing = False
        self._verifying = False
        self._error_shown = False
        self._ttk_fields: list = []
        self._tk_checks: list = []
        self._buttons: list = []
        self._setup_fonts()
        self._setup_style()
        self._build()
        self._load(config)
        self._append_log("界面已就绪。填写配置后点击“开始观战”。", "info")
        self.root.after(100, self._poll)
        if warnings:
            for text in warnings:
                self._append_log(text, "warn")
            if not smoke:
                self.root.after(500, lambda: messagebox.showwarning("配置提示", "\n".join(warnings)))
        if config.auto_start:
            self.root.after(800, self._auto_start)

    # ------------------------------------------------------------------ 外观
    def _setup_fonts(self) -> None:
        global FONT, MONO
        FONT = _check_font("Microsoft YaHei UI", "Segoe UI")
        MONO = _check_font("Cascadia Mono", "Consolas")

    def _setup_style(self) -> None:
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(FONT, 9))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 8))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=(FONT, 10, "bold"))
        style.configure("TEntry", fieldbackground="#F8FAFC", foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        insertcolor=TEXT, padding=(7, 3))
        style.map("TEntry",
                  bordercolor=[("focus", PRIMARY)],
                  lightcolor=[("focus", PRIMARY)],
                  darkcolor=[("focus", PRIMARY)])
        style.configure("TSpinbox", fieldbackground="#F8FAFC", foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=MUTED, padding=(6, 4))
        style.map("TSpinbox", bordercolor=[("focus", PRIMARY)])
        style.configure("TButton", font=(FONT, 9), padding=(10, 6), borderwidth=0,
                        focusthickness=0, background="#EEF2F7", foreground=TEXT)
        style.map("TButton", background=[("active", "#E3E9F2"), ("disabled", "#F1F3F7")],
                  foreground=[("disabled", "#AAB4C3")])
        style.configure("Primary.TButton", background=PRIMARY, foreground="#FFFFFF",
                        font=(FONT, 9, "bold"), padding=(12, 8))
        style.map("Primary.TButton", background=[("active", PRIMARY_DARK), ("disabled", "#A9C2F5")],
                  foreground=[("disabled", "#F8FAFF")])
        style.configure("Secondary.TButton", background=PRIMARY_SOFT, foreground=PRIMARY_DARK,
                        padding=(10, 7))
        style.map("Secondary.TButton", background=[("active", "#E0EAFF"), ("disabled", "#F2F5FA")],
                  foreground=[("disabled", "#AAB4C3")])
        style.configure("Ghost.TButton", background=CARD, foreground=MUTED, padding=(10, 7))
        style.map("Ghost.TButton", background=[("active", "#F1F5F9")])
        style.configure("Danger.TButton", background=DANGER_SOFT, foreground=DANGER,
                        padding=(10, 7))
        style.map("Danger.TButton", background=[("active", "#FEE4E2"), ("disabled", "#F8F2F1")],
                  foreground=[("disabled", "#C9B4B0")])
        style.configure("Header.TButton", background=HEADER, foreground="#CBD5E1",
                        padding=(10, 6))
        style.map("Header.TButton", background=[("active", HEADER_SOFT), ("pressed", HEADER_SOFT)])
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 8))
        style.map("Card.TCheckbutton", background=[("active", CARD)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=27, borderwidth=0, font=(FONT, 8))
        style.configure("Treeview.Heading", background="#F8FAFC", foreground=MUTED,
                        font=(FONT, 8, "bold"), relief="flat", padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", "#F1F5F9")])
        style.map("Treeview", background=[("selected", "#E8F0FF")],
                  foreground=[("selected", PRIMARY_DARK)])
        style.configure("TScrollbar", background="#DCE3ED", troughcolor=CARD,
                        bordercolor=CARD, arrowcolor=MUTED)
        style.configure("TSeparator", background=BORDER)

    # ------------------------------------------------------------------ 构建界面
    def _build(self) -> None:
        self.root.title(APP_TITLE)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self._build_header()
        body = tk.Frame(self.root, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(12, 0))
        body.grid_propagate(False)
        body.grid_rowconfigure(0, weight=1)
        scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        self.sidebar_width = max(344, min(480, int(344 * scale)))
        body.grid_columnconfigure(0, weight=0, minsize=self.sidebar_width + 12)
        body.grid_columnconfigure(1, weight=1)
        self._build_sidebar(body)
        self._build_main(body)
        self._build_footer()
        self._set_form_enabled(True)

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=HEADER)
        header.grid(row=0, column=0, sticky="ew")
        inner = tk.Frame(header, bg=HEADER)
        inner.pack(fill="x", padx=18, pady=11)

        logo = tk.Canvas(inner, width=44, height=44, bg=HEADER, highlightthickness=0)
        logo.pack(side="left", padx=(0, 12))
        self._round_rect(logo, 0, 0, 44, 44, 12, PRIMARY)
        logo.create_text(22, 22, text="观", fill="#FFFFFF", font=(FONT, 18, "bold"))

        titles = tk.Frame(inner, bg=HEADER)
        titles.pack(side="left", fill="y")
        tk.Label(titles, text=APP_TITLE, bg=HEADER, fg="#FFFFFF",
                 font=(FONT, 15, "bold"), anchor="w").pack(anchor="w")
        tk.Label(titles, text="远程观战客户端 · 自动领任务、进房录制并上传",
                 bg=HEADER, fg="#94A3B8", font=(FONT, 8), anchor="w").pack(anchor="w", pady=(2, 0))

        right = tk.Frame(inner, bg=HEADER)
        right.pack(side="right")
        pill = tk.Frame(right, bg=HEADER_SOFT, highlightbackground=HEADER_LINE,
                        highlightthickness=1)
        pill.pack(side="left", padx=(0, 10))
        self.status_dot = tk.Canvas(pill, width=8, height=8, bg=HEADER_SOFT,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(10, 6), pady=6)
        self._dot = self.status_dot.create_oval(1, 1, 7, 7, fill=FAINT, outline="")
        self.status_label = tk.Label(pill, text="已停止", bg=HEADER_SOFT, fg="#98A2B3",
                                     font=(FONT, 8, "bold"))
        self.status_label.pack(side="left", padx=(0, 10), pady=4)
        ttk.Button(right, text="使用说明", style="Header.TButton",
                   command=self._show_help).pack(side="left")

    def _build_sidebar(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=CARD, width=self.sidebar_width,
                        highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        card.grid_propagate(False)
        card.pack_propagate(False)
        self._sidebar_card = card

        # 底部操作区固定，不随字段一起滚动；窗口再矮也能点到“开始观战”。
        bottom = tk.Frame(card, bg=CARD)
        bottom.pack(side="bottom", fill="x", padx=15, pady=(0, 10))

        canvas = tk.Canvas(card, bg=CARD, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(card, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=CARD)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _event: (canvas.configure(scrollregion=canvas.bbox("all")),
                                   self.root.after_idle(self._update_sidebar_scroll)))
        canvas.bind("<Configure>",
                    lambda event: (canvas.itemconfigure(window, width=event.width),
                                   self.root.after_idle(self._update_sidebar_scroll)))
        self._sidebar_canvas = canvas
        self._sidebar_scroll = scroll
        inner.pack_propagate(True)
        content = tk.Frame(inner, bg=CARD)
        content.pack(fill="both", expand=True, padx=15, pady=11)
        self.root.after(250, self._update_sidebar_scroll)

        title_row = tk.Frame(content, bg=CARD)
        title_row.pack(fill="x")
        tk.Label(title_row, text="连接设置", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(title_row, text="仅保存在本机", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="right")

        self.server_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.show_token_var = tk.BooleanVar(value=False)
        self.show_password_var = tk.BooleanVar(value=False)
        self.remember_var = tk.BooleanVar(value=True)
        self.keep_local_var = tk.BooleanVar(value=True)
        self.auto_start_var = tk.BooleanVar(value=False)
        self._account_rows: list[dict] = []

        self.server_entry = self._field(content, "服务器地址", self.server_var)
        self.token_entry = self._field(content, "接口令牌", self.token_var, show="•",
                                       toggle=(self.show_token_var, "显示"))
        self._name_capacity_row(content)
        self._separator(content)
        self._accounts_section(content)

        # 选项跟着账号一起放在可滚动区，账号多时才不会把按钮挤出屏幕。
        self._separator(content)
        checks = tk.Frame(content, bg=CARD)
        checks.pack(fill="x", pady=(0, 4))
        for text, var in (("记住密码（DPAPI 加密）", self.remember_var),
                          ("在本机保存一份录像（可用 YGOPro 打开）", self.keep_local_var),
                          ("启动后自动开始观战", self.auto_start_var)):
            cb = ttk.Checkbutton(checks, text=text, variable=var, style="Card.TCheckbutton")
            cb.pack(anchor="w", pady=0)
            self._tk_checks.append(cb)

        buttons = tk.Frame(bottom, bg=CARD)
        buttons.pack(fill="x", pady=(2, 0))
        buttons.grid_columnconfigure(0, weight=1)
        buttons.grid_columnconfigure(1, weight=1)
        buttons.grid_columnconfigure(2, weight=1)
        self.btn_test = ttk.Button(buttons, text="测试连接", style="Secondary.TButton",
                                   command=self._test_connection)
        self.btn_test.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_verify = ttk.Button(buttons, text="验证账号", style="Secondary.TButton",
                                     command=self._verify_account)
        self.btn_verify.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_save = ttk.Button(buttons, text="保存", style="Ghost.TButton",
                                   command=self._save_settings)
        self.btn_save.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self._buttons.extend([self.btn_test, self.btn_verify, self.btn_save])

        actions = tk.Frame(bottom, bg=CARD)
        actions.pack(fill="x", pady=(6, 0))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        self.btn_start = ttk.Button(actions, text="开始观战", style="Primary.TButton",
                                    command=self._start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_stop = ttk.Button(actions, text="停止", style="Danger.TButton",
                                   command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._buttons.extend([self.btn_start, self.btn_stop])

        self.feedback = tk.Label(bottom, text="接口地址和令牌由服务器管理员提供。",
                                 bg=CARD, fg=FAINT, font=(FONT, 8), justify="left",
                                 anchor="w", wraplength=max(240, self.sidebar_width - 48))
        self.feedback.pack(fill="x", pady=(6, 0))
        self._bind_sidebar_wheel(inner)
        self._bind_sidebar_wheel(bottom)

    def _bind_sidebar_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._sidebar_wheel)
        for child in widget.winfo_children():
            self._bind_sidebar_wheel(child)

    def _sidebar_wheel(self, event):
        try:
            self._sidebar_canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass
        return "break"

    def _update_sidebar_scroll(self) -> None:
        """内容装得下时隐藏滚动条，窗口变矮时再显示。"""
        try:
            canvas = self._sidebar_canvas
            scroll = self._sidebar_scroll
            if not canvas.winfo_exists() or not scroll.winfo_exists():
                return
            bbox = canvas.bbox("all")
            needed = bool(bbox and bbox[3] > canvas.winfo_height() + 2)
            shown = bool(scroll.winfo_ismapped())
            if needed == shown:
                return
            if needed:
                scroll.pack_forget()
                scroll.pack(side="right", fill="y", before=canvas)
            else:
                scroll.pack_forget()
        except tk.TclError:
            pass

    def _build_main(self, parent: tk.Frame) -> None:
        main = tk.Frame(parent, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=3)
        main.grid_rowconfigure(2, weight=4)

        stats = tk.Frame(main, bg=BG)
        stats.grid(row=0, column=0, sticky="ew")
        for index in range(4):
            stats.grid_columnconfigure(index, weight=1, uniform="stats")
        self.stat_service = StatCard(stats, "服务状态", "离线", "尚未启动")
        self.stat_active = StatCard(stats, "进行中", "0 / 1", "并发上限")
        self.stat_saved = StatCard(stats, "已保存小局", "0", "本次运行")
        self.stat_uploaded = StatCard(stats, "已上传", "0", "0 B")
        for index, card in enumerate((self.stat_service, self.stat_active,
                                      self.stat_saved, self.stat_uploaded)):
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0 if index == 0 else 5, 0 if index == 3 else 5))

        self._build_jobs(main)
        self._build_log(main)

    def _build_jobs(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=9)
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="观战任务", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        self.jobs_count = tk.Label(head, text="0 个", bg=CARD, fg=FAINT, font=(FONT, 8))
        self.jobs_count.pack(side="left", padx=(8, 0))
        tk.Label(head, text="双击查看详情", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="right")

        table = tk.Frame(inner, bg=CARD)
        table.pack(fill="both", expand=True, pady=(6, 0))
        columns = ("room", "players", "account", "status", "saved", "uploaded")
        self.tree = ttk.Treeview(table, columns=columns, show="headings",
                                 selectmode="browse")
        headings = {"room": "房间", "players": "玩家", "account": "账号", "status": "状态",
                    "saved": "录像", "uploaded": "上传"}
        widths = {"room": 138, "players": 116, "account": 92, "status": 72,
                  "saved": 40, "uploaded": 40}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=36,
                             anchor="center" if col in ("saved", "uploaded") else "w")
        self.tree.tag_configure("active", foreground="#1570EF")
        self.tree.tag_configure("ok", foreground="#067647")
        self.tree.tag_configure("warn", foreground="#B54708")
        self.tree.tag_configure("err", foreground="#B42318")
        self.tree.tag_configure("muted", foreground=FAINT)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._show_job_details)
        self.jobs_empty = tk.Label(table, text="还没有任务\n点击左侧“开始观战”，客户端会自动领取服务器派发的房间",
                                   bg=CARD, fg=FAINT, font=(FONT, 9), justify="center")
        self.jobs_empty.place(relx=0.5, rely=0.55, anchor="center")

    def _build_log(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=9)
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="运行日志", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(head, text="令牌与密码不会写入日志", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="left", padx=(8, 0))
        ttk.Button(head, text="清空", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")
        ttk.Button(head, text="复制", style="Ghost.TButton",
                   command=self._copy_log).pack(side="right", padx=(0, 4))
        ttk.Button(head, text="目录", style="Ghost.TButton",
                   command=self._open_log_dir).pack(side="right", padx=(0, 4))

        text_frame = tk.Frame(inner, bg="#0B1220")
        text_frame.pack(fill="both", expand=True, pady=(7, 0))
        self.log_text = tk.Text(text_frame, bg="#0B1220", fg="#CBD5E1", relief="flat",
                                bd=0, highlightthickness=0, wrap="word",
                                font=(MONO, 9), padx=10, pady=8, state="disabled",
                                insertbackground="#CBD5E1")
        log_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.log_text.tag_configure("log_info", foreground="#CBD5E1")
        self.log_text.tag_configure("log_success", foreground="#6EE7B7")
        self.log_text.tag_configure("log_warn", foreground="#FCD34D")
        self.log_text.tag_configure("log_error", foreground="#FCA5A5")

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=BG)
        footer.grid(row=2, column=0, sticky="ew", padx=18, pady=(6, 7))
        path_label = tk.Label(footer, text=f"数据目录：{self.data_dir}", bg=BG, fg=FAINT,
                              font=(FONT, 7), cursor="hand2")
        path_label.pack(side="left")
        path_label.bind("<Button-1>", lambda _event: self._open_data_dir())
        replays_label = tk.Label(footer, text="录像目录", bg=BG, fg=PRIMARY,
                                 font=(FONT, 7), cursor="hand2")
        replays_label.pack(side="left", padx=(10, 0))
        replays_label.bind("<Button-1>", lambda _event: self._open_replays_dir())
        tk.Label(footer, text=f"v{APP_VERSION}", bg=BG, fg=FAINT,
                 font=(FONT, 7)).pack(side="right")

    # ------------------------------------------------------------------ 小部件
    @staticmethod
    def _round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, color) -> None:
        points = [x1 + radius, y1, x1 + radius, y1, x2 - radius, y1, x2 - radius, y1,
                  x2, y1, x2, y1 + radius, x2, y1 + radius, x2, y2 - radius, x2, y2 - radius,
                  x2, y2, x2 - radius, y2, x2 - radius, y2, x1 + radius, y2, x1 + radius, y2,
                  x1, y2, x1, y2 - radius, x1, y2 - radius, x1, y1 + radius, x1, y1 + radius,
                  x1, y1]
        canvas.create_polygon(points, smooth=True, fill=color, outline="")

    def _field(self, parent: tk.Frame, label: str, variable: tk.StringVar,
               show: str | None = None, toggle: tuple | None = None) -> ttk.Entry:
        wrapper = tk.Frame(parent, bg=CARD)
        wrapper.pack(fill="x", pady=(0, 5))
        head = tk.Frame(wrapper, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=label, bg=CARD, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(side="left")
        if toggle:
            var, text = toggle
            check = tk.Checkbutton(head, text=text, variable=var, bg=CARD,
                                   activebackground=CARD, fg=FAINT, activeforeground=MUTED,
                                   selectcolor=CARD, font=(FONT, 7), bd=0,
                                   highlightthickness=0, cursor="hand2",
                                   command=lambda: self._toggle_show(entry, var))
            check.pack(side="right")
            self._tk_checks.append(check)
        entry = ttk.Entry(wrapper, textvariable=variable, show=show or "")
        entry.pack(fill="x", pady=(3, 0))
        self._ttk_fields.append(entry)
        return entry

    def _name_capacity_row(self, parent: tk.Frame) -> None:
        wrapper = tk.Frame(parent, bg=CARD)
        wrapper.pack(fill="x", pady=(0, 5))
        left = tk.Frame(wrapper, bg=CARD)
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(left, text="客户端名称", bg=CARD, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(fill="x")
        self.name_entry = ttk.Entry(left, textvariable=self.name_var)
        self.name_entry.pack(fill="x", pady=(3, 0))
        self._ttk_fields.append(self.name_entry)

        right = tk.Frame(wrapper, bg=CARD)
        right.pack(side="right")
        tk.Label(right, text="同时观战", bg=CARD, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(fill="x")
        self.capacity_label = tk.Label(right, text="1 间", bg=CARD, fg=TEXT,
                                       font=(FONT, 11, "bold"), anchor="w")
        self.capacity_label.pack(fill="x", pady=(7, 0))

    def _accounts_section(self, parent: tk.Frame) -> None:
        """多账号列表：一行一个账号，账号数就是并发路数。"""
        wrapper = tk.Frame(parent, bg=CARD)
        wrapper.pack(fill="x", pady=(0, 4))
        head = tk.Frame(wrapper, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="萌卡账号", bg=CARD, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(side="left")
        self.btn_add_account = ttk.Button(head, text="＋账号", style="Ghost.TButton", width=6,
                                          command=self._add_account_row)
        self.btn_add_account.pack(side="right")
        self._ttk_fields.append(self.btn_add_account)
        self.account_count_label = tk.Label(head, text="0 个", bg=CARD, fg=FAINT,
                                            font=(FONT, 8))
        self.account_count_label.pack(side="right", padx=(0, 4))
        check = tk.Checkbutton(head, text="显示密码", variable=self.show_password_var,
                               bg=CARD, activebackground=CARD, fg=FAINT,
                               activeforeground=MUTED, selectcolor=CARD,
                               font=(FONT, 7), bd=0, highlightthickness=0, cursor="hand2",
                               command=self._toggle_all_passwords)
        check.pack(side="right", padx=(0, 4))
        self._tk_checks.append(check)

        self._account_box = tk.Frame(wrapper, bg=CARD)
        self._account_box.pack(fill="x", pady=(4, 0))

        tk.Label(wrapper, text="左边账号 / 右边密码，一个账号看一间房",
                 bg=CARD, fg=FAINT, font=(FONT, 7), anchor="w", justify="left",
                 wraplength=max(220, self.sidebar_width - 48)).pack(fill="x", pady=(4, 0))

    def _add_account_row(self, account: str = "", password: str = "",
                         refresh: bool = True) -> None:
        rows = [item for item in self._account_rows if item["frame"].winfo_exists()]
        self._account_rows = rows
        if len(rows) >= MAX_ACCOUNTS:
            self._feedback_text(f"最多 {MAX_ACCOUNTS} 个账号（一个账号一间房）。", WARNING)
            return
        row = tk.Frame(self._account_box, bg=CARD)
        row.pack(fill="x", pady=(0, 4))
        account_var = tk.StringVar(value=account)
        account_entry = ttk.Entry(row, textvariable=account_var)
        account_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        password_var = tk.StringVar(value=password)
        password_entry = ttk.Entry(row, textvariable=password_var, width=9,
                                   show="" if self.show_password_var.get() else "•")
        password_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        delete_btn = ttk.Button(row, text="×", style="Ghost.TButton", width=3,
                               command=lambda: self._remove_account_row(item))
        delete_btn.pack(side="right")
        item = {"frame": row, "account_var": account_var, "password_var": password_var,
                "account_entry": account_entry, "password_entry": password_entry,
                "delete_btn": delete_btn}
        self._account_rows.append(item)
        self._ttk_fields.extend([account_entry, password_entry, delete_btn])
        if refresh:
            self._refresh_account_summary()

    def _remove_account_row(self, item: dict) -> None:
        if item in self._account_rows:
            self._account_rows.remove(item)
        for key in ("account_entry", "password_entry", "delete_btn"):
            widget = item.get(key)
            if widget in self._ttk_fields:
                self._ttk_fields.remove(widget)
        try:
            item["frame"].destroy()
        except (KeyError, tk.TclError):
            pass
        if not self._account_rows:
            self._add_account_row(refresh=False)
        self._refresh_account_summary()

    def _clear_account_rows(self) -> None:
        for item in list(self._account_rows):
            for key in ("account_entry", "password_entry", "delete_btn"):
                widget = item.get(key)
                if widget in self._ttk_fields:
                    self._ttk_fields.remove(widget)
            try:
                item["frame"].destroy()
            except (KeyError, tk.TclError):
                pass
        self._account_rows = []

    def _toggle_all_passwords(self) -> None:
        show = "" if self.show_password_var.get() else "•"
        for item in self._account_rows:
            try:
                item["password_entry"].configure(show=show)
            except tk.TclError:
                pass

    def _refresh_account_summary(self) -> None:
        count = len([item for item in self._account_rows
                     if item["account_var"].get().strip()])
        try:
            self.account_count_label.configure(text=f"{count} 个")
            self.capacity_label.configure(text=f"{max(1, min(MAX_CAPACITY, count))} 间")
        except tk.TclError:
            pass

    @staticmethod
    def _separator(parent: tk.Frame) -> None:
        line = tk.Frame(parent, bg=BORDER, height=1)
        line.pack(fill="x", pady=(1, 8))

    def _toggle_show(self, entry: ttk.Entry, variable: tk.BooleanVar) -> None:
        try:
            entry.configure(show="" if variable.get() else "•")
        except tk.TclError:
            pass

    def _load(self, config: AppConfig) -> None:
        self.server_var.set(config.server)
        self.token_var.set(config.token)
        self.name_var.set(config.name)
        self.remember_var.set(config.remember_password)
        self.keep_local_var.set(config.keep_local)
        self.auto_start_var.set(config.auto_start)
        self._clear_account_rows()
        for item in (config.accounts or [AccountConfig()]):
            self._add_account_row(item.account, item.password, refresh=False)
        self._refresh_account_summary()
        self._render_status()
        self._on_stats({"active": 0, "capacity": config.capacity,
                        "worker": config.name, "saved": 0, "uploaded": 0,
                        "bytes": 0, "jobs": 0, "failed": 0, "nickname": "",
                        "accounts": []})

    def _collect(self) -> AppConfig:
        accounts: list[AccountConfig] = []
        for item in self._account_rows:
            try:
                account = item["account_var"].get().strip()
                password = item["password_var"].get()
            except (KeyError, tk.TclError):
                continue
            if account or password:
                accounts.append(AccountConfig(account=account, password=password))
        return AppConfig(
            server=self.server_var.get().strip(),
            token=self.token_var.get().strip(),
            name=self.name_var.get().strip(),
            accounts=accounts,
            remember_password=bool(self.remember_var.get()),
            keep_local=bool(self.keep_local_var.get()),
            auto_start=bool(self.auto_start_var.get()),
        )

    # ------------------------------------------------------------------ 表单状态
    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in self._ttk_fields:
            try:
                widget.state(["!disabled"] if enabled else ["disabled"])
            except tk.TclError:
                pass
        for widget in self._tk_checks:
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        for button in self._buttons:
            try:
                button.state(["!disabled"] if enabled else ["disabled"])
            except tk.TclError:
                pass
        if enabled:
            self.btn_stop.state(["disabled"])
        else:
            self.btn_stop.state(["!disabled"])

    def _feedback_text(self, text: str, color: str = FAINT) -> None:
        self.feedback.configure(text=text, fg=color)

    def _set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        self._state_detail = detail
        self._render_status()
        if state in ("stopped", "error"):
            self._set_form_enabled(True)
            self.btn_stop.state(["disabled"])
            if state == "error":
                self._feedback_text(detail or "启动失败", DANGER)
                if not self._error_shown and not self.smoke and not self._closing:
                    self._error_shown = True
                    messagebox.showerror("观战启动失败", detail or "请查看运行日志")
            else:
                self._feedback_text(detail or "已停止", FAINT)
            if self._closing:
                self._finish_close()
        elif state == "stopping":
            self._feedback_text(detail or "正在停止…", WARNING)
        elif state in ("starting", "authenticating"):
            self._feedback_text(detail or "正在启动…", WARNING)
        elif state == "reconnecting":
            self._feedback_text(detail or "网络异常，正在重试…", WARNING)

    def _render_status(self) -> None:
        text, color = STATE_STYLE.get(self._state, ("未知", FAINT))
        active = sum(1 for job in self._jobs if job.get("stage") not in
                     ("done", "error", "skipped", "stopped"))
        if self._state == "running" and active:
            text, color = f"观战中 · {active}", "#60A5FA"
        self.status_label.configure(text=text, fg=color)
        self.status_dot.itemconfigure(self._dot, fill=color)

    # ------------------------------------------------------------------ 动作
    def _auto_start(self) -> None:
        if self.smoke:
            return
        if self._validate_and_collect(show_message=False):
            self._start()

    def _validate_and_collect(self, show_message: bool = True) -> AppConfig | None:
        cfg = self._collect()
        errors = validate(cfg, need_account=True)
        if errors:
            if show_message and not self.smoke:
                messagebox.showerror("请检查配置", "\n".join("• " + item for item in errors))
            self._feedback_text(errors[0], DANGER)
            return None
        try:
            cfg.server = normalize_server(cfg.server)
        except ValueError as exc:
            if show_message and not self.smoke:
                messagebox.showerror("服务器地址不正确", str(exc))
            return None
        return cfg

    def _save(self, silent: bool = False) -> bool:
        cfg = self._collect()
        try:
            cfg.server = normalize_server(cfg.server)
        except ValueError as exc:
            if not silent and not self.smoke:
                messagebox.showerror("服务器地址不正确", str(exc))
            return False
        self.server_var.set(cfg.server)
        warnings = self.store.save(cfg)
        self.config = cfg
        if warnings:
            for text in warnings:
                self._append_log(text, "warn")
            if not silent and not self.smoke:
                messagebox.showwarning("配置保存提示", "\n".join(warnings))
        if not silent:
            self._feedback_text("设置已保存到本机。", SUCCESS)
            self._append_log("设置已保存。", "success")
        return True

    def _save_settings(self) -> None:
        if self._save():
            self._append_log("设置已保存（敏感字段使用 DPAPI 加密）。", "success")

    def _test_connection(self) -> None:
        if self._testing:
            return
        cfg = self._collect()
        try:
            cfg.server = normalize_server(cfg.server)
        except ValueError as exc:
            messagebox.showerror("服务器地址不正确", str(exc))
            return
        if not cfg.token:
            messagebox.showerror("缺少接口令牌", "请填写服务器管理员提供的接口令牌。")
            return
        self._testing = True
        self.btn_test.state(["disabled"])
        self._feedback_text("正在测试服务器连接…", WARNING)

        def run() -> None:
            result = WatchClient.test_connection(cfg.server, cfg.token)
            self.bus.put({"type": "test_result", "result": result})

        threading.Thread(target=run, name="test-connection", daemon=True).start()

    def _verify_account(self) -> None:
        if self._verifying:
            return
        cfg = self._collect()
        pairs = [(item.account.strip(), item.password) for item in cfg.real_accounts()
                 if item.account.strip() and item.password]
        if not pairs:
            messagebox.showerror("缺少账号信息", "请至少填写一个萌卡账号和密码。")
            return
        self._verifying = True
        self.btn_verify.state(["disabled"])
        self._feedback_text(f"正在逐个登录验证 {len(pairs)} 个账号…", WARNING)

        def run() -> None:
            results = WatchClient.verify_accounts(pairs)
            self.bus.put({"type": "verify_result", "results": results})

        threading.Thread(target=run, name="verify-account", daemon=True).start()

    def _start(self) -> None:
        if self.client and self.client.is_running():
            return
        cfg = self._validate_and_collect()
        if not cfg:
            return
        self._save(silent=True)
        self.server_var.set(cfg.server)
        self._jobs = []
        self._refresh_jobs()
        self._error_shown = False
        self.client = WatchClient(cfg, self.data_dir, emit=self._client_emit)
        if not self.client.start():
            self._feedback_text("客户端已经在运行。", WARNING)
            return
        self._state = "starting"
        self._set_form_enabled(False)
        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self._feedback_text("正在连接服务器并登录萌卡，请稍候…", WARNING)
        self._render_status()

    def _stop(self) -> None:
        if self.client and self.client.is_running():
            self.btn_stop.state(["disabled"])
            self._feedback_text("正在停止，最多等待当前网络请求完成…", WARNING)
            self.client.stop()
        else:
            self._set_form_enabled(True)
            self._feedback_text("当前没有运行中的观战。", FAINT)

    def _client_emit(self, kind: str, data: dict) -> None:
        payload = {"type": kind}
        payload.update(data)
        self.bus.put(payload)

    # ------------------------------------------------------------------ 事件循环
    def _poll(self) -> None:
        if self._destroyed:
            return
        try:
            while True:
                self._handle(self.bus.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "state":
            self._set_state(str(event.get("state") or "stopped"),
                            str(event.get("detail") or ""))
        elif kind == "log":
            self._append_log(str(event.get("text") or ""), str(event.get("level") or "info"))
        elif kind == "jobs":
            jobs = event.get("jobs")
            if isinstance(jobs, list):
                self._jobs = jobs
                self._refresh_jobs()
                self._render_status()
        elif kind == "stats":
            stats = event.get("stats")
            if isinstance(stats, dict):
                self._on_stats(stats)
        elif kind == "account":
            username = str(event.get("username") or "")
            if username:
                self._feedback_text(f"萌卡账号已登录：{username}", SUCCESS)
        elif kind == "connection":
            info = event.get("info") if isinstance(event.get("info"), dict) else {}
            if info:
                self._feedback_text(
                    f"已连接服务器 · 在线 {info.get('online_workers', 0)} 个客户端", SUCCESS)
        elif kind == "test_result":
            self._handle_test(event.get("result") or {})
        elif kind == "verify_result":
            results = event.get("results")
            if isinstance(results, list):
                self._handle_verify(results)

    def _handle_test(self, result: dict) -> None:
        self._testing = False
        if not (self.client and self.client.is_running()):
            self.btn_test.state(["!disabled"])
        if result.get("ok"):
            detail = (f"连接成功。\n在线客户端：{result.get('online_workers', 0)} 个\n"
                      f"待认领任务：{result.get('pending', 0)} 个\n"
                      f"服务器单 IP 上限：{result.get('ip_cap') or '未知'}")
            self._feedback_text("服务器连接成功。", SUCCESS)
            self._append_log(f"连接测试成功：在线 {result.get('online_workers', 0)} 个客户端。", "success")
            if not self.smoke:
                messagebox.showinfo("连接测试", detail)
        else:
            error = str(result.get("error") or "未知错误")
            self._feedback_text(error, DANGER)
            self._append_log(f"连接测试失败：{error}", "error")
            if not self.smoke:
                messagebox.showerror("连接测试失败", error)

    def _handle_verify(self, results: list[dict]) -> None:
        self._verifying = False
        if not (self.client and self.client.is_running()):
            self.btn_verify.state(["!disabled"])
        if not results:
            return
        ok = [item for item in results if item.get("ok")]
        bad = [item for item in results if not item.get("ok")]
        if not bad:
            names = "、".join(str(item.get("username") or item.get("account")) for item in ok)
            self._feedback_text(f"{len(ok)} 个账号全部有效", SUCCESS)
            self._append_log(f"萌卡账号验证成功：{names}", "success")
            if not self.smoke:
                messagebox.showinfo("账号验证", f"{len(ok)} 个萌卡账号全部有效。\n{names}")
            return
        lines = [f"• {item.get('account') or '—'}：{item.get('error') or '登录失败'}" for item in bad]
        self._feedback_text(f"{len(bad)} 个账号验证失败", DANGER)
        for item in bad:
            self._append_log(f"萌卡账号验证失败：{item.get('account')} · {item.get('error')}", "error")
        if not self.smoke:
            head = f"{len(ok)} 个有效，{len(bad)} 个失败。\n" if ok else ""
            messagebox.showerror("账号验证失败", head + "\n".join(lines))

    # ------------------------------------------------------------------ 列表/日志
    def _refresh_jobs(self) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        jobs = sorted(self._jobs, key=lambda item: item.get("started_at", 0), reverse=True)[:60]
        for job in jobs:
            job_id = str(job.get("id") or "")
            room = str(job.get("title") or job.get("room_key") or "未知房间")
            players = " vs ".join(str(item) for item in (job.get("players") or [])) or "—"
            account = str(job.get("account") or "") or "—"
            status = str(job.get("status") or "")
            saved = str(job.get("saved") or 0) if job.get("saved") else "—"
            uploaded = str(job.get("uploaded") or 0) if job.get("uploaded") else "—"
            stage = str(job.get("stage") or "")
            tag = ("active" if stage in ("queued", "waiting", "connecting", "watching",
                                         "waiting_replay")
                   else "ok" if stage == "done" else "err" if stage == "error"
                   else "muted")
            self.tree.insert("", "end", iid=job_id,
                             values=(room[:26], players[:26], account[:18], status,
                                     saved, uploaded),
                             tags=(tag,))
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        count = len(self._jobs)
        self.jobs_count.configure(text=f"{count} 个")
        if jobs:
            self.jobs_empty.place_forget()
        else:
            self.jobs_empty.place(relx=0.5, rely=0.55, anchor="center")

    def _on_stats(self, stats: dict) -> None:
        self._stats = stats
        state = self._state
        if state in ("running", "reconnecting", "starting", "authenticating"):
            service = "在线" if state != "reconnecting" else "重连中"
            color = SUCCESS if state == "running" else WARNING
        elif state == "error":
            service, color = "异常", DANGER
        else:
            service, color = "离线", FAINT
        worker = str(stats.get("worker") or self.name_var.get() or "—")
        accounts = stats.get("accounts") if isinstance(stats.get("accounts"), list) else []
        if accounts:
            ready = sum(1 for item in accounts if item.get("state") in ("idle", "busy"))
            sub = f"{ready}/{len(accounts)} 个账号可用"
        else:
            sub = worker[:28]
        self.stat_service.set(service, sub, color)
        active = int(stats.get("active") or 0)
        capacity = int(stats.get("capacity") or 1)
        self.stat_active.set(f"{active} / {capacity}", f"{capacity} 个账号并行")
        self.stat_saved.set(str(int(stats.get("saved") or 0)), "本次运行")
        self.stat_uploaded.set(str(int(stats.get("uploaded") or 0)),
                               format_bytes(int(stats.get("bytes") or 0)))

    def _append_log(self, text: str, level: str = "info") -> None:
        if not text:
            return
        try:
            at_bottom = self.log_text.yview()[1] >= 0.985
        except tk.TclError:
            at_bottom = True
        color, tag = LEVEL_STYLE.get(level, LEVEL_STYLE["info"])
        line = f"{time.strftime('%H:%M:%S')}  {text}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, (tag,))
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 2500:
                self.log_text.delete("1.0", "500.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.configure(state="disabled")
        if at_bottom:
            self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._append_log("日志显示已清空（磁盘日志仍保留）。", "info")

    def _copy_log(self) -> None:
        try:
            text = self.log_text.get("1.0", "end-1c")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._feedback_text("当前日志已复制到剪贴板。", SUCCESS)
        except tk.TclError:
            pass

    def _open_log_dir(self) -> None:
        self._open_path(self.data_dir / "logs")

    def _open_replays_dir(self) -> None:
        folder = self.data_dir / "local-replays"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("打开录像目录失败", str(exc))
            return
        self._open_path(folder)

    def _open_data_dir(self) -> None:
        self._open_path(self.data_dir)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开目录失败", str(exc))

    def _show_job_details(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        job_id = selection[0]
        job = next((item for item in self._jobs if str(item.get("id")) == job_id), None)
        if not job:
            return
        started = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(float(job.get("started_at") or 0)))
        players = " vs ".join(str(item) for item in (job.get("players") or [])) or "—"
        text = (f"房间：{job.get('title') or '—'}\n"
                f"玩家：{players}\n"
                f"账号：{job.get('account') or '—'}\n"
                f"状态：{job.get('status') or '—'}\n"
                f"已保存：{job.get('saved') or 0} 个小局\n"
                f"已上传：{job.get('uploaded') or 0} 个（{format_bytes(int(job.get('uploaded_bytes') or 0))}）\n"
                f"已收包：{job.get('packets') or 0}\n"
                f"开始时间：{started}\n"
                f"任务编号：{job.get('id') or '—'}\n"
                f"详情：{job.get('detail') or '—'}")
        messagebox.showinfo("观战任务详情", text)

    # ------------------------------------------------------------------ 帮助/关闭
    def _show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.configure(bg=CARD)
        win.transient(self.root)
        win.grab_set()
        width, height = 560, 520
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.minsize(480, 400)

        title = tk.Label(win, text="使用说明", bg=CARD, fg=TEXT,
                         font=(FONT, 14, "bold"), anchor="w")
        title.pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(win, text="本程序是远程观战客户端：领取任务、进入房间、录制并上传录像。",
                 bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w", justify="left",
                 wraplength=510).pack(fill="x", padx=20, pady=(0, 8))

        text = tk.Text(win, bg="#F8FAFC", fg=TEXT, relief="flat", bd=0,
                       highlightbackground=BORDER, highlightthickness=1,
                       font=(FONT, 9), wrap="word", padx=14, pady=12)
        text.pack(fill="both", expand=True, padx=20)
        content = (
            "第一次使用\n"
            "1. 向服务器管理员获取“服务器地址”和“接口令牌”。\n"
            "2. 在“萌卡账号”里填 1 个或多个账号和密码；账号只在本机登录，不上传服务器。\n"
            "3. 客户端名称在同一令牌下必须唯一，例如 pc-你的名字。\n"
            "4. 一个账号同时只进一间房：填 3 个账号，就并行观战 3 间。\n"
            "5. 点击“开始观战”，程序会自动领任务、进房录制并上传。\n\n"
            "界面说明\n"
            "• 服务状态：服务器接口是否可连；在线待命表示正在等任务。\n"
            "• 观战任务：当前和历史任务，双击可看房间、玩家、用的哪个账号。\n"
            "• 运行日志：出问题时先看这里，日志里不会出现令牌和密码。\n"
            "• 已上传：本次运行成功上传的录像数量；小局数按录像包统计。\n\n"
            "数据放在哪\n"
            "• 全部数据都在 exe 同级的 data 目录里：config.json（配置）、logs（日志）、\n"
            "  local-replays（录像，按日期分文件夹）、decks（临时卡组）。\n"
            "• 界面底部“数据目录”“录像目录”可以直接点开。\n"
            "• 录像直接复制给别人用 YGOPro 打开即可；换电脑时整个 data 目录一起拷走，\n"
            "  但密码是 DPAPI 密文，新电脑上需要重新填写。\n\n"
            "注意事项\n"
            "• 同一个账号不要同时进多个房间，平台可能拒发回放；多开请用多个账号。\n"
            "• 两次进场至少间隔约 20–25 秒，这是客户端主动限速，避免被平台限制。\n"
            "• 停止或关闭窗口会断开当前观战；已上传的录像不受影响。\n"
            "• 令牌、密码使用 Windows DPAPI 加密保存，换 Windows 用户或换电脑后需要重新填写。\n"
            "• 服务器地址当前为 HTTP；请只使用管理员提供的地址，不要在公共网络泄露令牌。\n\n"
            "常见错误\n"
            "• 401：令牌不正确或已被轮换，向管理员要新令牌。\n"
            "• 404/502：服务器没有启用远程观战接口，或服务未启动。\n"
            "• 萌卡登录失败：账号密码错误，或网络无法访问萌卡。\n"
            "• 一直显示“无录像”：平台没有下发回放，常见于同账号多房间。\n"
            "• 409：任务在服务器重启后过期；已上传文件通常仍会入库。\n"
        )
        text.insert("1.0", content)
        text.configure(state="disabled")
        footer = tk.Frame(win, bg=CARD)
        footer.pack(fill="x", padx=20, pady=12)
        ttk.Button(footer, text="打开数据目录", style="Ghost.TButton",
                   command=self._open_data_dir).pack(side="left")
        ttk.Button(footer, text="知道了", style="Primary.TButton",
                   command=win.destroy).pack(side="right")

    def on_close(self) -> None:
        if self._closing:
            return
        if self.client and self.client.is_running():
            if self.smoke:
                self._closing = True
                self.client.stop()
                self._finish_close()
                return
            if not messagebox.askyesno("退出观战助手",
                                       "观战仍在进行。\n退出会停止当前观战并断开房间，确定退出吗？"):
                return
            self._closing = True
            self._feedback_text("正在停止观战并退出…", WARNING)
            self.client.stop()
            # 兜底：网络请求最多让用户等 8 秒，之后直接关闭窗口。
            self.root.after(8000, self._finish_close)
            return
        self._save(silent=True)
        self._finish_close()

    def _finish_close(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def smoke_close(self) -> None:
        self._closing = True
        if self.client:
            self.client.stop()
        self._finish_close()
