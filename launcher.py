# -*- coding: utf-8 -*-
"""Sims4Multiplayer 联机启动器 v6.1（游戏风格美化版）

第二轮十轮研究升级（2026-08-04）:
1. 侧边栏导航布局（游戏启动器主流设计，对齐 Steam/Epic 模式）
2. PIL 生成渐变背景（深炭黑 + 霓虹绿/电光蓝光晕 + HUD 点阵）
3. 玻璃拟态卡片（半透明 + 细边框 + 圆角，2026 趋势）
4. 连接进度条（CTkProgressBar：检测中 → 连接中 → 已连接）
5. 霓虹色标题 + 状态徽章动画
6. 实时状态监控（游戏/主机/对端/延迟 四合一）

依赖: customtkinter, pillow（pip install customtkinter pillow）
运行: python launcher.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import customtkinter as ctk
from PIL import Image, ImageTk

APP_VERSION = "9.20.1"
APP_REPO = "mo9652962-ai/second-brain"  # v8.5: GitHub 自动更新检查源
UPDATE_URL = "https://api.github.com/repos/{}/releases/latest".format(APP_REPO)
DEFAULT_GAME_DIR = r"D:\Games\The Sims 4"
GAME_EXE = r"Game\Bin\TS4_x64.exe"
DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts", "The Sims 4")
MODS_DIR = os.path.join(DOCS_DIR, "Mods")
CONFIG_PATH = os.path.join(MODS_DIR, "mp_launcher_config.json")
LOG_PATH = os.path.join(MODS_DIR, "mp_debug.log")
DEFAULT_PORT = 7655
# v9.18: 启动器房间系统（百轮研究: S4MP 启动器建房流程 / EOS Lobby-Session 分离）
try:
    from room_protocol import RoomServer, RoomClient, ROOM_READY, ROOM_SYNCED, gen_room_code
    ROOM_PROTOCOL_OK = True
except Exception:
    ROOM_PROTOCOL_OK = False
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# 游戏风格配色（十轮研究确认的 2026 游戏 UI 标准）
C = {
    "bg": "#0D0D0D",
    "panel": "#161616",
    "glass": "#1E1E1E",
    "neon": "#00FF85",
    "blue": "#1E90FF",
    "pink": "#FF0099",       # hover 点缀（vev.design 推荐）
    "text": "#F5F5F5",
    "dim": "#8A8A8A",
    "danger": "#FF4D4D",
    "warn": "#FFB800",
    # v8.3: 状态色规范（研究: Carbon status palette）
    # 绿=正常/在线 黄=警告 红=错误/掉线 蓝=信息 灰=离线
    "status_ok": "#31C24B",      # 在线/成功
    "status_warn": "#FFB800",    # 警告/准备中
    "status_err": "#FF4D4D",     # 错误/掉线
    "status_info": "#1E90FF",    # 信息/进图
    "status_off": "#5A5A5A",     # 离线/未连接
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# v8.8: 主题配色表（研究: CustomTkinter 自定义主题 JSON 思路）
THEMES = {
    "neon": {"neon": "#00FF85", "blue": "#1E90FF", "pink": "#FF0099"},
    "blue": {"neon": "#1E90FF", "blue": "#00B4FF", "pink": "#FF5CA8"},
    "pink": {"neon": "#FF4DB8", "blue": "#A64DFF", "pink": "#FF0099"},
    "amber": {"neon": "#FFB800", "blue": "#FF8A00", "pink": "#FF5CA8"},
}


def detect_game_dir():
    candidates = [DEFAULT_GAME_DIR, r"D:\Games\The Sims 4",
                  r"C:\Program Files\EA Games\The Sims 4",
                  r"C:\Program Files (x86)\Origin Games\The Sims 4",
                  r"C:\Program Files\Steam\steamapps\common\The Sims 4",
                  r"C:\Program Files (x86)\Steam\steamapps\common\The Sims 4"]
    for c in candidates:
        if os.path.exists(os.path.join(c, GAME_EXE)):
            return c
    return DEFAULT_GAME_DIR


def get_local_ip():
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.16."):
                return ip
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check_port_open(host, port, timeout=0.5):
    start = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True, int((time.time() - start) * 1000)
    except Exception:
        return False, None


def check_game_running():
    # 固定查询任务名，list 传参不用 shell（防注入）；
    # 中文 Windows 下 tasklist 输出是 GBK，用 errors=ignore 兼容
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TS4_x64.exe", "/NH"],
                             capture_output=True, text=True, encoding="gbk",
                             errors="ignore", timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return "TS4_x64.exe" in out
    except Exception:
        return False


class LauncherApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Sims4Multiplayer 联机启动器 v{}".format(APP_VERSION))
        self.geometry("900x620")
        self.minsize(820, 560)

        # 图标
        icon_path = os.path.join(ASSET_DIR, "launcher_icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        # 渐变背景（用 CTkImage 支持 HighDPI 缩放）
        bg_path = os.path.join(ASSET_DIR, "bg_dark.png")
        if os.path.exists(bg_path):
            self._bg_img = ctk.CTkImage(light_image=Image.open(bg_path),
                                        dark_image=Image.open(bg_path), size=(900, 620))
            bg_label = ctk.CTkLabel(self, image=self._bg_img, text="")
            bg_label.place(x=0, y=0, relwidth=1, relheight=1)

        self._monitor_running = False
        self._build_ui()
        self._load_settings()  # v8.1: 恢复上次设置（研究: settings persistence UX）
        self._check_update_async()  # v8.5: 自动更新检查（研究: GitHub Releases API）
        self._log("启动器 v{} 就绪".format(APP_VERSION))
        self._log("检测到游戏: {}".format(self.game_dir_var.get()))
        self._log("本机 IP: {}".format(get_local_ip()))
        self._start_monitor()

    # ============ UI ============
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---- 左侧导航栏（Steam/Epic 风格）----
        nav = ctk.CTkFrame(self, width=190, fg_color=C["panel"], corner_radius=0)
        nav.grid(row=0, column=0, sticky="ns", padx=(0, 0))
        nav.grid_propagate(False)

        # Logo 区
        logo = ctk.CTkLabel(nav, text="🎮", font=ctk.CTkFont(size=34))
        logo.pack(pady=(24, 0))
        logo_t = ctk.CTkLabel(nav, text="Sims4\nMultiplayer", font=ctk.CTkFont(size=17, weight="bold"),
                              text_color=C["neon"], justify="center")
        logo_t.pack(pady=(4, 20))

        # 导航项
        self._nav_buttons = []
        items = [("🔌 连接", 0), ("👥 房间", 1), ("💬 聊天", 2), ("⚙️ 设置", 3), ("ℹ️ 关于", 4)]
        for text, idx in items:
            btn = ctk.CTkButton(nav, text=text, font=ctk.CTkFont(size=14),
                                anchor="w", height=40, corner_radius=10,
                                fg_color="transparent", hover_color="#2A2A2A",
                                text_color=C["dim"], command=lambda i=idx: self._switch_tab(i))
            btn.pack(fill="x", padx=12, pady=3)
            self._nav_buttons.append(btn)
        self._nav_buttons[0].configure(fg_color="#242424", text_color=C["neon"])

        # 底部版本
        ctk.CTkLabel(nav, text="v{}".format(APP_VERSION), font=ctk.CTkFont(size=11),
                     text_color=C["dim"]).pack(side="bottom", pady=12)

        # ---- 主内容区 ----
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(16, 16), pady=16)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # 顶部标题栏
        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.page_title = ctk.CTkLabel(header, text="🔌 连接", font=ctk.CTkFont(size=24, weight="bold"),
                                       text_color=C["text"])
        self.page_title.pack(side="left")
        self.status_badge = ctk.CTkLabel(header, text="● 未连接", corner_radius=12,
                                         fg_color="#1E1E1E", text_color=C["dim"],
                                         font=ctk.CTkFont(size=12, weight="bold"))
        self.status_badge.pack(side="right")

        # 页面容器（玻璃卡片叠加）
        self.page_container = ctk.CTkFrame(main, fg_color="#171717")
        self.page_container.grid(row=1, column=0, sticky="nsew")
        self.page_container.grid_columnconfigure(0, weight=1)
        self.page_container.grid_rowconfigure(0, weight=1)

        # 4 个页面（Frame 叠放，tkraise 切换）
        self.pages = []
        for i in range(5):
            pg = ctk.CTkFrame(self.page_container, fg_color="transparent")
            pg.grid(row=0, column=0, sticky="nsew")
            self.pages.append(pg)
        self._build_connect_page(self.pages[0])
        self._build_room_page(self.pages[1])
        self._build_chat_page(self.pages[2])
        self._build_settings_page(self.pages[3])
        self._build_about_page(self.pages[4])
        self._switch_tab(0)

    def _switch_tab(self, idx):
        """切换页面（侧边栏高亮 + 标题联动）"""
        titles = ["🔌 连接", "👥 房间", "💬 聊天", "⚙️ 设置", "ℹ️ 关于"]
        self.page_title.configure(text=titles[idx])
        for i, btn in enumerate(self._nav_buttons):
            if i == idx:
                btn.configure(fg_color="#242424", text_color=C["neon"])
            else:
                btn.configure(fg_color="transparent", text_color=C["dim"])
        self.pages[idx].tkraise()

    # ============ 玻璃卡片工具 ============
    def _glass_card(self, parent, title, title_color):
        card = ctk.CTkFrame(parent, fg_color="#1C1C1C", corner_radius=14,
                            border_width=1, border_color="#2E2E2E")
        card.pack(fill="x", padx=4, pady=6)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=title_color).pack(anchor="w", padx=16, pady=(12, 2))
        return card

    # ---- 连接页 ----
    def _build_connect_page(self, pg):
        # v8.9: Hero 横幅（研究: hero banner——渐变+暗色层+大标题+状态徽章）
        hero = ctk.CTkFrame(pg, fg_color="#121212", corner_radius=16,
                            border_width=1, border_color="#2A2A2A")
        hero.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 8))
        hero.grid_columnconfigure(1, weight=1)
        # 渐变模拟（深色→主题色透明，研究: linear-gradient 暗色层思路）
        ctk.CTkLabel(hero, text="联机启动器", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=C["text"]).grid(row=0, column=0, sticky="w", padx=(20, 8), pady=(16, 2))
        ctk.CTkLabel(hero, text="《模拟人生4》局域网 / 跨网联机", font=ctk.CTkFont(size=12),
                     text_color=C["dim"]).grid(row=1, column=0, sticky="w", padx=(20, 8), pady=(0, 16))
        # 右侧状态徽章（大，Carbon 状态色）
        self.hero_badge = ctk.CTkLabel(hero, text="● 就绪", font=ctk.CTkFont(size=14, weight="bold"),
                                       text_color=C["status_ok"], corner_radius=8,
                                       fg_color="#1A2E22", padx=14, pady=6)
        self.hero_badge.grid(row=0, column=1, rowspan=2, sticky="e", padx=20)

        # v8.7: Bento 双列布局（研究: CustomTkinter grid 多列）——
        # 左列=操作区(模式/参数/启动)，右列=信息区(状态/日志)
        pg.grid_columnconfigure(0, weight=3)
        pg.grid_columnconfigure(1, weight=2)
        pg.grid_rowconfigure(3, weight=1)

        # Bento tile 1: 模式卡（大 tile，研究: bento grid tile 层次）
        mode_card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                                 border_width=1, border_color="#2E2E2E")
        mode_card.grid(row=1, column=0, sticky="ew", padx=(4, 8), pady=6)
        ctk.CTkLabel(mode_card, text="连接模式", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=C["neon"]).pack(anchor="w", padx=16, pady=(12, 2))
        self.mode_var = ctk.StringVar(value="host")
        row = ctk.CTkFrame(mode_card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=4)
        self.host_btn = ctk.CTkRadioButton(row, text="🏠 房主（创建房间）", variable=self.mode_var,
                                           value="host", command=self._on_mode_change,
                                           font=ctk.CTkFont(size=14))
        self.host_btn.pack(side="left", padx=(0, 30))
        self.join_btn = ctk.CTkRadioButton(row, text="💻 加入（连接房主）", variable=self.mode_var,
                                           value="join", command=self._on_mode_change,
                                           font=ctk.CTkFont(size=14))
        self.join_btn.pack(side="left")

        # 参数卡片
        param_card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                                  border_width=1, border_color="#2E2E2E")
        param_card.grid(row=2, column=0, sticky="ew", padx=(4, 8), pady=6)
        ctk.CTkLabel(param_card, text="连接参数", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=C["blue"]).pack(anchor="w", padx=16, pady=(12, 2))
        grid = ctk.CTkFrame(param_card, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=6)
        grid.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(grid, text="房主 IP:", font=ctk.CTkFont(size=13)).grid(row=0, column=0, sticky="w", pady=4)
        self.host_ip_var = ctk.StringVar(value="192.168.0.")
        ctk.CTkEntry(grid, textvariable=self.host_ip_var, width=180,
                     placeholder_text="例如 192.168.0.112").grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)
        self.my_ip_label = ctk.CTkLabel(grid, text="我的 IP: {}".format(get_local_ip()),
                                        font=ctk.CTkFont(size=12), text_color=C["dim"])
        self.my_ip_label.grid(row=0, column=2, sticky="e", padx=(16, 0), pady=4)
        ctk.CTkLabel(grid, text="端口:", font=ctk.CTkFont(size=13)).grid(row=1, column=0, sticky="w", pady=4)
        self.port_var = ctk.StringVar(value=str(DEFAULT_PORT))
        ctk.CTkEntry(grid, textvariable=self.port_var, width=100).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        # v9.16: 玩家昵称（房间页显示的名字，代替电脑名）
        try:
            import socket as _sn
            _default_name = _sn.gethostname()
        except Exception:
            _default_name = "玩家"
        ctk.CTkLabel(grid, text="玩家名:", font=ctk.CTkFont(size=13)).grid(row=2, column=0, sticky="w", pady=4)
        self.name_var = ctk.StringVar(value=_default_name)
        ctk.CTkEntry(grid, textvariable=self.name_var, width=180,
                     placeholder_text="房间显示的昵称").grid(row=2, column=1, sticky="w", padx=(10, 0), pady=4)
        ctk.CTkLabel(grid, text="（房间页显示的名字）", font=ctk.CTkFont(size=12), text_color=C["dim"])\
            .grid(row=2, column=2, sticky="e", padx=(16, 0), pady=4)
        # v9.18: 房间码（加入者填，房主自动生成）
        ctk.CTkLabel(grid, text="房间码:", font=ctk.CTkFont(size=13)).grid(row=3, column=0, sticky="w", pady=4)
        self.room_code_var = ctk.StringVar(value="")
        self.room_code_entry = ctk.CTkEntry(grid, textvariable=self.room_code_var, width=120,
                                            placeholder_text="6位码(加入时)")
        self.room_code_entry.grid(row=3, column=1, sticky="w", padx=(10, 0), pady=4)
        ctk.CTkLabel(grid, text="（房主自动生成，加入者填）", font=ctk.CTkFont(size=12), text_color=C["dim"])\
            .grid(row=3, column=2, sticky="e", padx=(16, 0), pady=4)
        # v9.18: 加入密码（私密房间需要）
        ctk.CTkLabel(grid, text="房间密码:", font=ctk.CTkFont(size=13)).grid(row=4, column=0, sticky="w", pady=4)
        self.join_pwd_var = ctk.StringVar(value="")
        self.join_pwd_entry = ctk.CTkEntry(grid, textvariable=self.join_pwd_var, width=120,
                                           placeholder_text="私密房间需填", show="*")
        self.join_pwd_entry.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=4)

        # 启动按钮
        btn_row = ctk.CTkFrame(pg, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=(4, 8), pady=6)
        btn_row.grid_columnconfigure(0, weight=3)
        btn_row.grid_columnconfigure(1, weight=1)
        btn_row.grid_columnconfigure(2, weight=1)
        self.launch_btn = ctk.CTkButton(btn_row, text="🚀 创建房间", font=ctk.CTkFont(size=16, weight="bold"),
                                        height=44, corner_radius=12, fg_color=C["neon"], hover_color="#00CC6A",
                                        text_color="#0D0D0D", command=self._launch)
        self.launch_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.scan_btn = ctk.CTkButton(btn_row, text="🔍 扫描", font=ctk.CTkFont(size=13),
                                      height=44, corner_radius=12, fg_color="#242424", hover_color="#2E2E2E",
                                      text_color=C["dim"], command=self._scan_rooms)
        self.scan_btn.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.config_btn = ctk.CTkButton(btn_row, text="仅写配置", font=ctk.CTkFont(size=13),
                                        height=44, corner_radius=12, fg_color="#242424", hover_color="#2E2E2E",
                                        text_color=C["dim"], command=self._write_config_only)
        self.config_btn.grid(row=0, column=2, sticky="ew", padx=(0, 0))

        # 连接信息 + 日志（Bento 右列：占满整列高度）
        bottom = ctk.CTkFrame(pg, fg_color="transparent")
        bottom.grid(row=1, column=1, rowspan=3, sticky="nsew", padx=(0, 4), pady=4)
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(0, weight=0)
        bottom.grid_rowconfigure(1, weight=1)

        # 状态卡
        info_card = ctk.CTkFrame(bottom, fg_color="#1C1C1C", corner_radius=14,
                                 border_width=1, border_color="#2E2E2E")
        info_card.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 6))
        ctk.CTkLabel(info_card, text="连接状态", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=C["warn"]).pack(anchor="w", padx=16, pady=(12, 4))
        info_grid = ctk.CTkFrame(info_card, fg_color="transparent")
        info_grid.pack(fill="x", padx=16, pady=4)
        info_grid.grid_columnconfigure(0, weight=1)
        info_grid.grid_columnconfigure(1, weight=1)
        # v8.4: 状态色（研究: Carbon status palette——绿=正常/黄=警告/灰=离线）
        self.info_game = ctk.CTkLabel(info_grid, text="游戏: ⚪ 未运行", font=ctk.CTkFont(size=13),
                                      text_color=C["status_off"])
        self.info_game.grid(row=0, column=0, sticky="w", pady=4)
        self.info_host = ctk.CTkLabel(info_grid, text="主机: ⚪ 未启动", font=ctk.CTkFont(size=13),
                                      text_color=C["status_off"])
        self.info_host.grid(row=0, column=1, sticky="w", pady=4)
        self.info_peer = ctk.CTkLabel(info_grid, text="对端: ⚪ 未连接", font=ctk.CTkFont(size=13),
                                      text_color=C["status_off"])
        self.info_peer.grid(row=1, column=0, sticky="w", pady=4)
        self.info_latency = ctk.CTkLabel(info_grid, text="延迟: --", font=ctk.CTkFont(size=13),
                                         text_color=C["status_off"])
        self.info_latency.grid(row=1, column=1, sticky="w", pady=4)

        # 进度条（检测 → 连接）
        self.progress = ctk.CTkProgressBar(info_card, height=10, corner_radius=5,
                                           progress_color=C["neon"], fg_color="#2A2A2A")
        self.progress.pack(fill="x", padx=16, pady=(8, 12))
        self.progress.set(0)

        # 日志
        log_card = ctk.CTkFrame(bottom, fg_color="#1C1C1C", corner_radius=14,
                                border_width=1, border_color="#2E2E2E")
        log_card.grid(row=1, column=0, sticky="nsew", padx=0, pady=(6, 0))
        ctk.CTkLabel(log_card, text="日志", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=C["dim"]).pack(anchor="w", padx=16, pady=(12, 2))
        self.log_text = ctk.CTkTextbox(log_card, font=ctk.CTkFont(family="Consolas", size=11),
                                       fg_color="#121212", text_color=C["text"])
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    # ---- 房间页（M3d）----
    def _build_room_page(self, pg):
        # v8.8: Bento 双列布局（研究: CustomTkinter grid 多列）——
        # 左列=房间码+操作，右列=成员列表（占满高度）
        pg.grid_columnconfigure(0, weight=2)
        pg.grid_columnconfigure(1, weight=3)
        pg.grid_rowconfigure(2, weight=1)

        # 状态横幅
        self.room_status = ctk.CTkLabel(pg, text="未连接房间", corner_radius=10,
                                        fg_color="#1C1C1C", text_color=C["dim"],
                                        font=ctk.CTkFont(size=13))
        self.room_status.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 8))

        # 房间码卡片（S4MP 同款 invite code）——Bento 左列
        code_card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                                 border_width=1, border_color="#2E2E2E")
        code_card.grid(row=1, column=0, sticky="ew", padx=(4, 8), pady=4)
        code_row = ctk.CTkFrame(code_card, fg_color="transparent")
        code_row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(code_row, text="🔑 房间码:", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=C["blue"]).pack(side="left")
        self.room_code_label = ctk.CTkLabel(code_row, text="----", font=ctk.CTkFont(size=22, weight="bold"),
                                            text_color=C["neon"])
        self.room_code_label.pack(side="left", padx=(10, 0))
        # v8.5: 带宽监控（研究: psutil.net_io_counters 差值法）
        self.bw_label = ctk.CTkLabel(code_row, text="", font=ctk.CTkFont(size=11),
                                     text_color=C["dim"])
        self.bw_label.pack(side="right", padx=8)
        self._bw_last = None
        self._bw_running = False
        # 可见性选择（十轮研究: flackr/lobby public/private）
        self.vis_var = ctk.StringVar(value="public")
        vis_frame = ctk.CTkFrame(code_row, fg_color="transparent")
        vis_frame.pack(side="left", padx=(16, 0))
        ctk.CTkLabel(vis_frame, text="可见性:", font=ctk.CTkFont(size=12),
                     text_color=C["dim"]).pack(side="left")
        ctk.CTkRadioButton(vis_frame, text="公开", variable=self.vis_var, value="public",
                           font=ctk.CTkFont(size=12), command=self._on_vis_change).pack(side="left", padx=(4, 8))
        ctk.CTkRadioButton(vis_frame, text="私密", variable=self.vis_var, value="private",
                           font=ctk.CTkFont(size=12), command=self._on_vis_change).pack(side="left")
        self.pwd_entry = ctk.CTkEntry(code_row, placeholder_text="房间密码(私密)", width=110,
                                      show="*", font=ctk.CTkFont(size=12))
        self.pwd_entry.pack(side="left", padx=(10, 0))
        # v8.0: QR 码邀请（研究: MakeCode Arcade 扫码加入）——内容=IP+房间码
        self.qr_btn = ctk.CTkButton(code_row, text="📱 QR邀请", width=72, height=28,
                                    fg_color="#242424", hover_color="#2E2E2E",
                                    command=self._show_qr)
        self.qr_btn.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(code_row, text="（加入者输入此码/密码，同局域网）",
                     font=ctk.CTkFont(size=11), text_color=C["dim"]).pack(side="left", padx=(12, 0))

        # 成员列表卡片——Bento 右列（占满 row1-2 高度）
        card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                            border_width=1, border_color="#2E2E2E")
        card.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=(0, 4), pady=4)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(card, text="👥 房间成员", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=C["neon"]).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))
        self.room_text = ctk.CTkTextbox(card, font=ctk.CTkFont(family="Consolas", size=13),
                                        fg_color="#121212", state="disabled")
        self.room_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)

        # 操作按钮（房主/成员角色不同）——Bento 左列
        btn_row = ctk.CTkFrame(pg, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=(4, 8), pady=(8, 4))
        btn_row.grid_columnconfigure((0, 1), weight=1)
        self.ready_btn = ctk.CTkButton(btn_row, text="✅ 我准备", font=ctk.CTkFont(size=13),
                                       height=40, corner_radius=10, fg_color=C["blue"],
                                       hover_color="#1878D6", command=self._toggle_ready)
        self.ready_btn.grid(row=0, column=0, sticky="ew", padx=3, pady=2)
        self.sync_btn = ctk.CTkButton(btn_row, text="📦 同步存档(房主)", font=ctk.CTkFont(size=13),
                                      height=40, corner_radius=10, fg_color="#242424",
                                      hover_color="#2E2E2E", command=self._sync_save)
        self.sync_btn.grid(row=0, column=1, sticky="ew", padx=3, pady=2)
        self.start_btn = ctk.CTkButton(btn_row, text="🚀 开始游戏(房主)", font=ctk.CTkFont(size=13),
                                       height=40, corner_radius=10, fg_color=C["neon"],
                                       hover_color="#00CC6A", text_color="#0D0D0D", command=self._start_game)
        self.start_btn.grid(row=1, column=0, sticky="ew", padx=3, pady=2)
        self.leave_btn = ctk.CTkButton(btn_row, text="🚪 离开房间", font=ctk.CTkFont(size=13),
                                       height=40, corner_radius=10, fg_color="#242424",
                                       hover_color="#2E2E2E", command=self._leave_room)
        self.leave_btn.grid(row=1, column=1, sticky="ew", padx=3, pady=2)

        # 提示（全宽，row3）
        ctk.CTkLabel(pg, text="提示: 房主点「同步存档」需全员准备 → 同步完成 → 全员进图后房主可「开始游戏」。游戏内命令: mp_ready/mp_unready/mp_syncsave/mp_start/mp_leave/mp_kick",
                     font=ctk.CTkFont(size=11), text_color=C["dim"], wraplength=680, justify="left"
                     ).grid(row=3, column=0, columnspan=2, padx=8, pady=(4, 8))

        # 启动时立即刷新
        self._refresh_room()

    def _read_lobby_state(self):
        """读房间状态文件（mod 写入）"""
        try:
            path = os.path.join(MODS_DIR, "mp_lobby_state.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _refresh_room(self):
        """刷新房间列表显示（v9.18: 优先启动器房间状态，回退 mod 状态文件）"""
        # ---- v9.18: 启动器房间模式（不依赖游戏） ----
        if getattr(self, "room_server", None) or getattr(self, "room_client", None):
            self._refresh_room_protocol()
            return
        state = self._read_lobby_state()
        if state is None:
            self.room_status.configure(text="未连接房间 (启动游戏并创建/加入后显示)", text_color=C["dim"])
            self._set_room_text("等待加入房间...")
            return
        members = state.get("members", [])
        is_host = state.get("is_host", False)
        my_pid = state.get("my_player_id", 0)
        phase = state.get("save_sync_phase", "idle")
        granted = state.get("start_granted", False)
        room_code = state.get("room_code", "")

        # v8.5: 房间激活时启动带宽监控
        self._start_bw_monitor()

        # 房间码显示
        self.room_code_label.configure(text=room_code or "----")

        # 状态横幅
        if is_host:
            self.room_status.configure(text="🏠 我是房主 | 存档同步: {} | 开始权: {}".format(
                {"idle": "未开始", "waiting_ack": "等待确认", "done": "已完成"}.get(phase, phase),
                "✅" if granted else "❌"), text_color=C["neon"])
        else:
            self.room_status.configure(text="💻 我是成员 | 存档同步: {} | 开始权: {}".format(
                {"idle": "未开始", "waiting_ack": "等待确认", "done": "已完成"}.get(phase, phase),
                "✅" if granted else "❌"), text_color=C["blue"])

        # v9.20.1: 按钮状态管理——按角色/存档阶段启用禁用
        # v9.20.2: 打通两条同步链路——mp_syncsave(游戏内, 更新 mp_lobby_state.json 的 granted)
        # 与 room_protocol 的 state 独立。若游戏同步已完成(granted)但 proto_state 未流转,
        # 把 room_server.state 同步为 ROOM_SYNCED, 否则 start_btn 永远 disabled。
        try:
            is_room_protocol = bool(getattr(self, "room_server", None) or getattr(self, "room_client", None))
            if is_room_protocol:
                # 启动器房间模式：按钮按协议状态控制
                srv = getattr(self, "room_server", None)
                cli = getattr(self, "room_client", None)
                proto_state = (srv or cli).state
                proto_host = bool(srv)
                # v9.20.2: 游戏内 mp_syncsave 同步完成后, 把 room_server.state 流转到 synced
                if proto_host and granted and proto_state in ("waiting", "ready", "syncing"):
                    try:
                        self.room_server.state = ROOM_SYNCED
                        proto_state = ROOM_SYNCED
                        self._log("同步完成: mp_lobby_state.start_granted=True → room_server.state=synced")
                    except Exception as e:
                        self._log("⚠ 状态流转失败: {}".format(e))
                self.sync_btn.configure(
                    state="normal" if proto_host else "disabled",
                    text="📦 同步存档" if proto_host else "📦 同步存档(需房主)",
                    fg_color="#242424" if proto_host else "#1A1A1A")
                # v9.20.2: start 启用 = 房主 且 (proto synced/launching 或 游戏 granted)
                can_start = proto_host and (proto_state in ("synced", "launching") or granted)
                self.start_btn.configure(
                    state="normal" if can_start else "disabled",
                    text="🚀 开始游戏" if can_start else "🚀 开始游戏(先同步存档)",
                    fg_color=C["neon"] if can_start else "#1A1A1A")
            else:
                # 游戏内 lobby 模式：按 save_sync_phase/start_granted 控制
                if not is_host:
                    self.sync_btn.configure(state="disabled", text="📦 同步存档(需房主)", fg_color="#1A1A1A")
                    self.start_btn.configure(state="disabled", text="🚀 开始游戏(需房主)", fg_color="#1A1A1A")
                else:
                    # 房主：同步按钮在 waiting_ack 时禁用；开始按钮需 granted
                    if phase == "waiting_ack":
                        self.sync_btn.configure(state="disabled", text="🔄 同步中...", fg_color="#1A1A1A")
                    else:
                        self.sync_btn.configure(state="normal", text="📦 同步存档(房主)", fg_color="#242424")
                    if granted:
                        self.start_btn.configure(state="normal", text="🚀 开始游戏", fg_color=C["neon"])
                    else:
                        self.start_btn.configure(state="disabled", text="🚀 先同步存档", fg_color="#1A1A1A")
        except Exception:
            pass

        # 成员列表（含在线状态，v8.3: Carbon 状态色规范）
        lines = ["{:<4} {:<16} {:<6} {:<6} {:<6} {:<6} {}".format("ID", "玩家", "准备", "进图", "在线", "状态", "身份"),
                 "-" * 58]
        for m in members:
            tag = "🏠房主" if m.get("is_host") else "👤成员"
            me = " ←我" if m.get("player_id") == my_pid else ""
            online = "🟢" if m.get("online", True) else "⚫"
            # 状态色（研究: Carbon status palette）
            st = m.get("status", "")
            if m.get("online", True) is False:
                st_color = "🔴 掉线"
            elif st == "in_lot":
                st_color = "🔵 进图"
            elif m.get("ready"):
                st_color = "🟢 就绪"
            elif st == "connecting":
                st_color = "🟡 连接中"
            else:
                st_color = "⚪ 等待"
            lines.append("{:<4} {:<16} {:<6} {:<6} {:<6} {:<6} {}{}".format(
                m.get("player_id", "?"), m.get("name", "?")[:16],
                "✅" if m.get("ready") else "⬜", "✅" if m.get("in_lot") else "⬜",
                online, st_color, tag, me))

        # v7.0: 局域网发现的房间（加入者视角）
        discovered = state.get("discovered_rooms", {})
        if discovered:
            lines.append("")
            lines.append("📡 局域网发现 {} 个房间:".format(len(discovered)))
            for ip, info in list(discovered.items())[:5]:
                lines.append("   {} · {} · {}人 · 码{}".format(
                    ip, info.get("name", "?"), info.get("players", 0), info.get("room_code", "")))
        self._set_room_text("\n".join(lines))

    # ============ v9.18: 启动器房间模式刷新 ============
    def _refresh_room_protocol(self):
        """刷新启动器房间状态（房间码/成员/准备/存档同步/开始游戏）"""
        try:
            srv = getattr(self, "room_server", None)
            cli = getattr(self, "room_client", None)
            if srv:
                members = list(srv.members.values())
                room_code = srv.room_code
                state = srv.state
                is_host = True
            elif cli:
                members = cli.members
                room_code = cli.room_code or ""
                state = cli.state
                is_host = False
            else:
                return
            # 房间码
            self.room_code_label.configure(text=room_code or "----")
            # 状态横幅
            state_names = {"waiting": "等待成员准备", "ready": "全员已准备 ✅",
                           "syncing": "存档同步中...", "synced": "存档同步完成 ✅",
                           "launching": "开始游戏！"}
            self.room_status.configure(
                text=("🏠 我是房主" if is_host else "💻 我是成员") +
                " | {}".format(state_names.get(state, state)), text_color=C["neon"])
            # v9.20.2: 按钮状态控制 (房间模式真正执行的路径!)
            # 之前写在了 _refresh_room() 游戏内分支, 房间模式 512 行 return 不执行 → 按钮永不更新
            try:
                # granted 兜底: 读游戏内 mp_syncsave 完成标志 (mp_lobby_state.json)
                granted = False
                try:
                    st_path = os.path.join(MODS_DIR, "mp_lobby_state.json")
                    with open(st_path, "r", encoding="utf-8") as f:
                        st = json.load(f)
                    granted = bool(st.get("start_granted", False))
                except Exception:
                    pass
                if is_host:
                    # 房主: 同步按钮随时可点; 开始按钮需 synced/launching 或游戏 granted
                    self.sync_btn.configure(state="normal", text="📦 同步存档(房主)",
                                            fg_color="#242424")
                    can_start = state in ("synced", "launching") or granted
                    self.start_btn.configure(
                        state="normal" if can_start else "disabled",
                        text="🚀 开始游戏" if can_start else "🚀 先同步存档",
                        fg_color=C["neon"] if can_start else "#1A1A1A")
                else:
                    # 成员: 两者都禁用 (等房主)
                    self.sync_btn.configure(state="disabled", text="📦 同步存档(需房主)", fg_color="#1A1A1A")
                    self.start_btn.configure(state="disabled", text="🚀 开始游戏(需房主)", fg_color="#1A1A1A")
            except Exception as e:
                self._log("⚠ 按钮状态更新异常: {}".format(e))
            # 成员列表
            lines = ["{:<4} {:<14} {:<6} {}".format("ID", "玩家", "准备", "身份"),
                     "-" * 42]
            for m in members:
                tag = "🏠房主" if m.get("is_host") else "👤成员"
                ready = "✅" if m.get("ready") else "⬜"
                me = " ←我" if (cli and m.get("player_id") == cli.player_id) else ""
                lines.append("{:<4} {:<14} {:<6} {}".format(
                    m.get("player_id", "?"), m.get("name", "?")[:14], ready, tag + me))
            lines.append("")
            lines.append("状态: {}".format(state_names.get(state, state)))
            if is_host:
                lines.append("成员加入后双方点我准备-同步存档-开始游戏")
            self._set_room_text("\n".join(lines))
        except Exception as e:
            self._log("房间刷新异常: {}".format(e))
    def _start_bw_monitor(self):
        if self._bw_running:
            return
        self._bw_running = True
        self._bw_last = None

        def _loop():
            import psutil
            while self._bw_running:
                try:
                    io = psutil.net_io_counters()
                    now = (io.bytes_sent, io.bytes_recv)
                    if self._bw_last:
                        up = now[0] - self._bw_last[0]
                        dn = now[1] - self._bw_last[1]
                        def _fmt(b):
                            return "{}KB/s".format(b // 1024) if b >= 1024 else "{}B/s".format(b)
                        self.after(0, lambda u=up, d=dn: self.bw_label.configure(
                            text="↑{} ↓{}".format(_fmt(u), _fmt(d))))
                    self._bw_last = now
                except Exception:
                    pass
                time.sleep(2)
        import threading
        threading.Thread(target=_loop, daemon=True).start()

    def _set_room_text(self, text):
        try:
            self.room_text.configure(state="normal")
            self.room_text.delete("1.0", "end")
            self.room_text.insert("1.0", text)
            self.room_text.configure(state="disabled")
        except Exception as e:
            self._log("⚠ 更新成员列表失败: {}".format(e))

    def _on_vis_change(self):
        """可见性切换：私密时启用密码框"""
        if self.vis_var.get() == "private":
            self.pwd_entry.configure(placeholder_text="设置房间密码(4-8位)")
        else:
            self.pwd_entry.configure(placeholder_text="房间密码(私密)")
            self.pwd_entry.delete(0, "end")

    def _show_qr(self):
        """生成房间邀请 QR 码（内容: 房主IP + 房间码 + 密码），弹窗显示"""
        try:
            import qrcode
            from PIL import Image, ImageTk as _itk
            state = self._read_lobby_state() or {}
            room_code = state.get("room_code", "")
            my_ip = get_local_ip()
            vis = state.get("room_visibility", "public")
            # QR 内容: "IP:端口|房间码|密码(私密时)"
            qr_text = "{}:{}|{}".format(my_ip, self.port_var.get(), room_code)
            if vis == "private":
                pwd = self.pwd_entry.get().strip()
                if pwd:
                    qr_text += "|{}".format(pwd)
            qr = qrcode.QRCode(box_size=8, border=2)
            qr.add_data(qr_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#00FF85", back_color="#0D0D0D")
            # 弹窗
            win = ctk.CTkToplevel(self)
            win.title("📱 扫码加入房间")
            win.geometry("300x400")
            win.configure(fg_color="#171717")
            photo = _itk.PhotoImage(img)
            lbl = ctk.CTkLabel(win, image=photo, text="")
            lbl.image = photo  # 防 GC
            lbl.pack(pady=(16, 4))
            ctk.CTkLabel(win, text="扫码加入房间", font=ctk.CTkFont(size=15, weight="bold"),
                         text_color=C["neon"]).pack()
            ctk.CTkLabel(win, text="IP:{}:{} 码:{}".format(my_ip, self.port_var.get(), room_code),
                         font=ctk.CTkFont(size=11), text_color=C["dim"]).pack(pady=(4, 16))
            self._log("QR 邀请已生成: {}".format(qr_text))
        except Exception as e:
            self._log("QR 生成失败: {}".format(e))

    def _toggle_ready(self):
        """我准备/取消（通过写命令文件让 mod 执行）"""
        self._write_mp_cmd("mp_ready")
        self._log("已发送准备指令 (游戏内 mp_ready)")

    def _leave_room(self):
        """离开房间（通过写命令文件让 mod 执行）"""
        self._write_mp_cmd("mp_leave")
        self._log("已发送离开房间指令")

    def _sync_save(self):
        """v9.20.1: 房主同步存档——发出指令 + 立即刷新显示进度"""
        self._write_mp_cmd("mp_syncsave")
        self._log("已发送同步存档指令 (房主 mp_syncsave)")
        # 立即把按钮置为"同步中"并安排刷新（mod 侧 v9.20.1 会自动发最新存档）
        try:
            self.sync_btn.configure(state="disabled", text="🔄 同步中...", fg_color="#1A1A1A")
        except Exception:
            pass
        self.after(1500, self._refresh_room)

    def _start_game(self):
        """v9.20.1: 房主开始游戏——发出指令 + 立即刷新显示"""
        self._write_mp_cmd("mp_start")
        self._log("已发送开始游戏指令 (房主 mp_start)")
        # 立即反馈 + 短延时刷新（mod 侧广播 start_game + clock 解除客机暂停）
        try:
            self.start_btn.configure(state="disabled", text="🚀 已发送开始...", fg_color="#1A1A1A")
        except Exception:
            pass
        self.after(1500, self._refresh_room)

    def _write_mp_cmd(self, cmd):
        """写命令文件（mod 轮询读取执行——启动器到游戏内命令桥）"""
        try:
            os.makedirs(MODS_DIR, exist_ok=True)
            path = os.path.join(MODS_DIR, "mp_cmd.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cmd": cmd, "ts": time.time()}, f)
        except Exception as e:
            self._log("写命令失败: {}".format(e))

    # ---- 聊天页 ----
    def _build_chat_page(self, pg):
        # v9.0: 聊天页激活（研究: 状态文件轮询 + 命令注入）
        # 显示 lobby state 里的 chat 历史；发送按钮写入游戏指令文件（游戏内轮询执行）
        pg.grid_columnconfigure(0, weight=1)
        pg.grid_rowconfigure(0, weight=1)
        card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                            border_width=1, border_color="#2E2E2E")
        card.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(card, text="💬 聊天", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=C["blue"]).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))
        self.chat_text = ctk.CTkTextbox(card, font=ctk.CTkFont(size=13),
                                        fg_color="#121212", state="disabled")
        self.chat_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        bottom.grid_columnconfigure(0, weight=1)
        self.chat_input = ctk.CTkEntry(bottom, placeholder_text="输入消息...", height=36)
        self.chat_input.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.chat_input.bind("<Return>", lambda e: self._send_chat())
        ctk.CTkButton(bottom, text="发送", width=80, height=36, fg_color=C["blue"],
                      hover_color="#1878D6", command=self._send_chat).grid(row=0, column=1)
        ctk.CTkButton(bottom, text="🔄", width=40, height=36, fg_color="#242424",
                      hover_color="#2E2E2E", command=self._refresh_chat).grid(row=0, column=2, padx=(8, 0))
        ctk.CTkLabel(card, text="显示游戏内聊天（mp_say）记录；发送需游戏已连接房间",
                     font=ctk.CTkFont(size=12), text_color=C["dim"]).grid(row=3, column=0, padx=16, pady=(0, 10))
        # 定时刷新（2s 轮询 lobby state 的 chat）
        self._chat_last_count = -1
        self._chat_poll()

    def _chat_poll(self):
        """轮询 lobby state 聊天历史（研究: 状态文件轮询模式）"""
        try:
            state = self._read_lobby_state()
            if state:
                chat = state.get("chat", [])
                if len(chat) != self._chat_last_count:
                    self._chat_last_count = len(chat)
                    self._set_chat_text(chat)
        except Exception:
            pass
        self.after(2000, self._chat_poll)

    def _refresh_chat(self):
        """手动刷新聊天（立即重读）"""
        try:
            state = self._read_lobby_state()
            if state:
                self._set_chat_text(state.get("chat", []))
                self._log("聊天已刷新 ({} 条)".format(len(state.get("chat", []))))
        except Exception:
            pass

    def _set_chat_text(self, chat):
        lines = []
        for m in chat:
            lines.append("{} [{}] {}".format(m.get("ts", ""), m.get("from", "?"), m.get("text", "")))
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_text.insert("1.0", "\n".join(lines) if lines else "（暂无聊天记录）")
        self.chat_text.configure(state="disabled")

    def _send_chat(self):
        """发送聊天：写入游戏指令文件（游戏内 mp_poll 执行）+ 本机显示"""
        text = self.chat_input.get().strip()
        if not text:
            return
        self.chat_input.delete(0, "end")
        try:
            # 写入指令文件（mod 的游戏内轮询会读取执行）
            cmd_dir = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                                   "The Sims 4", "Mods", "Sims4Multiplayer")
            os.makedirs(cmd_dir, exist_ok=True)
            with open(os.path.join(cmd_dir, "chat_cmd.txt"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
            # v9.1: 修复本机不显示的 bug——直接写 lobby state 的 chat 字段
            # （启动器环境没有 multimod 包，不能 import lobby；改为直接读改写 json）
            try:
                state = self._read_lobby_state() or {}
                chat = list(state.get("chat", []))
                chat.append({"from": "我", "text": str(text)[:200],
                             "ts": __import__("time").strftime("%H:%M:%S")})
                state["chat"] = chat[-20:]
                st_path = os.path.join(MODS_DIR, "mp_lobby_state.json")
                with open(st_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            # 立即刷新聊天框
            self._refresh_chat()
            self._log("已发送: {}".format(text))
        except Exception as e:
            self._log("发送失败: {}".format(e))

    # ---- 设置页 ----
    def _build_settings_page(self, pg):
        pg.grid_columnconfigure(0, weight=1)
        card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                            border_width=1, border_color="#2E2E2E")
        card.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(card, text="⚙️ 设置", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=C["neon"]).pack(anchor="w", padx=16, pady=(14, 6))

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(row1, text="游戏目录:", font=ctk.CTkFont(size=13)).pack(side="left")
        self.game_dir_var = ctk.StringVar(value=detect_game_dir())
        ctk.CTkEntry(row1, textvariable=self.game_dir_var, width=280).pack(side="left", padx=8)
        ctk.CTkButton(row1, text="浏览...", width=70, height=28, command=self._browse_game).pack(side="left")

        self.auto_sync_var = ctk.BooleanVar(value=True)
        sync_row = ctk.CTkFrame(card, fg_color="transparent")
        sync_row.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(sync_row, text="连接后自动开始位置同步 (mp_sync)",
                     font=ctk.CTkFont(size=13)).pack(side="left")
        ctk.CTkSwitch(sync_row, text="", variable=self.auto_sync_var,
                      progress_color=C["neon"], width=46).pack(side="right")

        # v8.8: 主题切换（研究: CustomTkinter 自定义主题 JSON）
        theme_row = ctk.CTkFrame(card, fg_color="transparent")
        theme_row.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(theme_row, text="主题配色:", font=ctk.CTkFont(size=13)).pack(side="left")
        self.theme_var = ctk.StringVar(value="neon")
        ctk.CTkOptionMenu(theme_row, values=["neon", "blue", "pink", "amber"],
                          variable=self.theme_var, width=120, command=self._on_theme_change
                          ).pack(side="left", padx=8)
        ctk.CTkLabel(theme_row, text="切换后即时生效", font=ctk.CTkFont(size=12),
                     text_color=C["dim"]).pack(side="left")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=6)
        ctk.CTkButton(row2, text="🛡️ 一键配置防火墙", width=160, fg_color="#242424",
                      hover_color="#2E2E2E", command=self._setup_firewall).pack(side="left")
        ctk.CTkLabel(row2, text="需管理员权限，放行游戏端口 7655", font=ctk.CTkFont(size=12),
                     text_color=C["dim"]).pack(side="left", padx=10)

        # v8.2: UPnP 自动端口转发（研究: miniupnpc）——跨网联机
        row2b = ctk.CTkFrame(card, fg_color="transparent")
        row2b.pack(fill="x", padx=16, pady=6)
        ctk.CTkButton(row2b, text="🌐 UPnP 开放端口", width=160, fg_color="#242424",
                      hover_color="#2E2E2E", command=self._upnp_toggle).pack(side="left")
        ctk.CTkLabel(row2b, text="自动映射路由器端口（异地联机用）", font=ctk.CTkFont(size=12),
                     text_color=C["dim"]).pack(side="left", padx=10)
        self.upnp_status = ctk.CTkLabel(row2b, text="未开启", font=ctk.CTkFont(size=12),
                                        text_color=C["dim"])
        self.upnp_status.pack(side="right", padx=8)

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=16, pady=6)
        ctk.CTkButton(row3, text="📁 打开 Mods 目录", width=160, fg_color="#242424",
                      hover_color="#2E2E2E", command=self._open_mods).pack(side="left")
        ctk.CTkLabel(row3, text=MODS_DIR, font=ctk.CTkFont(size=12), text_color=C["dim"]).pack(side="left", padx=10)

        # v8.0: 一键诊断（研究: Unity Package Manager Diagnostics）
        row4 = ctk.CTkFrame(card, fg_color="transparent")
        row4.pack(fill="x", padx=16, pady=6)
        ctk.CTkButton(row4, text="🔧 一键诊断", width=160, fg_color="#242424",
                      hover_color="#2E2E2E", command=self._run_diagnostics).pack(side="left")
        self.diag_text = ctk.CTkLabel(row4, text="检测: 游戏/端口/防火墙/对端/存档",
                                      font=ctk.CTkFont(size=12), text_color=C["dim"])
        self.diag_text.pack(side="left", padx=10)

    # ---- 关于页 ----
    def _build_about_page(self, pg):
        pg.grid_columnconfigure(0, weight=1)
        card = ctk.CTkFrame(pg, fg_color="#1C1C1C", corner_radius=14,
                            border_width=1, border_color="#2E2E2E")
        card.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text="🎮 Sims4Multiplayer", font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=C["neon"]).grid(row=0, column=0, pady=(18, 2))
        ctk.CTkLabel(card, text="《模拟人生4》局域网 / 跨网联机 · v{}".format(APP_VERSION),
                     font=ctk.CTkFont(size=13), text_color=C["dim"]).grid(row=1, column=0)
        info = ("▸ 同存档 + 同一家庭联机，各控制不同小人\n"
                "▸ 实时位置同步（插值+Delta压缩），互相看到对方移动\n"
                "▸ 金钱 / 需求 / 技能 / 心情（带情绪通知）同步\n"
                "▸ 旅行双端确认（防黑屏）· 存档自动传输 / 备份 / 校验\n"
                "▸ UPnP 跨网直连 + STUN 公网 IP + QR 邀请\n"
                "▸ 启动器聊天页与游戏内 mp_say / mp_emoji 互通\n\n"
                "游戏内命令: mp_host / mp_join / mp_sync / mp_say / mp_travel\n"
                "           mp_money / mp_statssync / mp_mood / mp_ready\n"
                "           mp_syncsave / mp_start / mp_leave / mp_kick\n"
                "           mp_emoji 1~8 · mp_status · mp_lobby · mp_poll\n"
                "日志文件: Mods\\mp_debug.log（排障用）")
        ctk.CTkLabel(card, text=info, font=ctk.CTkFont(size=13), justify="left",
                     text_color=C["text"]).grid(row=2, column=0, padx=24, pady=14)
        ctk.CTkLabel(card, text="开源思路: 借鉴 ts4mp / SimSync / S4MP 协议实践",
                     font=ctk.CTkFont(size=12), text_color=C["dim"]).grid(row=3, column=0, pady=(0, 16))

    # ============ 逻辑 ============
    # ============ v8.5: 自动更新检查（研究: GitHub Releases API） ============
    def _check_update_async(self):
        def _check():
            try:
                import json as _json
                import urllib.request as _ur
                req = _ur.Request(UPDATE_URL, headers={"User-Agent": "Sims4Multiplayer/{}".format(APP_VERSION)})
                with _ur.urlopen(req, timeout=6) as r:
                    data = _json.loads(r.read().decode("utf-8"))
                latest = str(data.get("tag_name", "")).lstrip("v")
                if not latest:
                    return
                # 版本比较（研究: semver 比较）
                def _key(v):
                    parts = []
                    for p in v.split("."):
                        num = ""
                        for c in p:
                            if c.isdigit():
                                num += c
                        parts.append(int(num) if num else 0)
                    return parts
                if _key(latest) > _key(APP_VERSION):
                    self.after(0, lambda: self._log(
                        "🎉 发现新版本 v{}！请到桌面「Sims4联机mod-分享版.zip」或 GitHub 更新".format(latest)))
            except Exception:
                pass  # 无网络/无更新源时静默
        import threading
        threading.Thread(target=_check, daemon=True).start()

    # ============ v8.5: 崩溃日志自动保存（研究: sys.excepthook pattern） ============
    @staticmethod
    def _install_crash_hook():
        import sys as _sys
        import traceback as _tb
        log_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Sims4Multiplayer")
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

        def _hook(exc_type, exc_value, exc_tb):
            try:
                path = os.path.join(log_dir, "crash_{}.log".format(
                    __import__("time").strftime("%Y%m%d_%H%M%S")))
                with open(path, "w", encoding="utf-8") as f:
                    f.write("Sims4Multiplayer 崩溃日志\n")
                    f.write("版本: {}\n".format(APP_VERSION))
                    f.write("时间: {}\n\n".format(__import__("time").ctime()))
                    _tb.print_exception(exc_type, exc_value, exc_tb, file=f)
                # 弹出提示（仅 GUI 模式）
                try:
                    import tkinter.messagebox as _mb
                    _mb.showerror("启动器异常",
                                  "遇到错误已保存到:\n{}\n\n可发送给开发者排查".format(path))
                except Exception:
                    pass
            except Exception:
                pass
            # 默认行为
            _sys.__excepthook__(exc_type, exc_value, exc_tb)

        _sys.excepthook = _hook

    # ============ v8.1: 设置持久化（研究: settings persistence UX） ============
    def _settings_path(self):
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "Sims4Multiplayer", "settings.json")

    def _load_settings(self):
        """启动时恢复上次设置（模式/IP/端口/游戏目录/自动同步）"""
        try:
            p = self._settings_path()
            if not os.path.exists(p):
                return
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
            # 只在 var 存在时设置（防 UI 未建）
            for name, key in [("mode_var", "mode"), ("host_ip_var", "host_ip"),
                              ("port_var", "port"), ("game_dir_var", "game_dir"),
                              ("auto_sync_var", "auto_sync")]:
                var = getattr(self, name, None)
                if var is not None and key in s:
                    try:
                        var.set(s[key])
                    except Exception:
                        pass
            # 可见性/密码（房间页）
            for name, key in [("vis_var", "visibility"), ("pwd_entry", "password")]:
                obj = getattr(self, name, None)
                if obj is not None and key in s:
                    try:
                        if name == "vis_var":
                            obj.set(s[key])
                        else:
                            obj.delete(0, "end")
                            obj.insert(0, s[key])
                    except Exception:
                        pass
            # v8.8: 主题恢复（先应用再设置下拉框）
            if "theme" in s and hasattr(self, "theme_var"):
                try:
                    tname = s["theme"]
                    if tname in THEMES:
                        t = THEMES[tname]
                        C["neon"], C["blue"], C["pink"] = t["neon"], t["blue"], t["pink"]
                        self.theme_var.set(tname)
                except Exception:
                    pass
            self._log("已恢复上次设置")
        except Exception as e:
            self._log("设置恢复失败: {}".format(e))

    def _save_settings(self):
        """保存当前设置（下次启动恢复）"""
        try:
            s = {}
            if hasattr(self, "mode_var"):
                s["mode"] = self.mode_var.get()
            if hasattr(self, "host_ip_var"):
                s["host_ip"] = self.host_ip_var.get()
            if hasattr(self, "port_var"):
                s["port"] = self.port_var.get()
            if hasattr(self, "game_dir_var"):
                s["game_dir"] = self.game_dir_var.get()
            if hasattr(self, "auto_sync_var"):
                s["auto_sync"] = self.auto_sync_var.get()
            if hasattr(self, "vis_var"):
                s["visibility"] = self.vis_var.get()
            if hasattr(self, "pwd_entry"):
                s["password"] = self.pwd_entry.get()
            if hasattr(self, "theme_var"):
                s["theme"] = self.theme_var.get()
            d = os.path.dirname(self._settings_path())
            os.makedirs(d, exist_ok=True)
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log("设置保存失败: {}".format(e))

    def _log(self, msg):
        try:
            self.log_text.insert("end", "[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
            self.log_text.see("end")
        except Exception:
            pass

    def _browse_game(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.game_dir_var.get(), title="选择 The Sims 4 目录")
        if d:
            self.game_dir_var.set(d)
            self._log("游戏目录: {}".format(d))

    def _on_mode_change(self):
        if self.mode_var.get() == "host":
            self.my_ip_label.configure(text="我的 IP: {}".format(get_local_ip()))
            self.launch_btn.configure(text="🚀 创建房间")
        else:
            self.my_ip_label.configure(text="")
            self.launch_btn.configure(text="🚀 加入房间")

    def _send_chat(self):
        msg = self.chat_input.get().strip()
        if not msg:
            return
        self.chat_input.delete(0, "end")
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", "我: {}\n".format(msg))
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def _build_config(self):
        mode = self.mode_var.get()
        port = int(self.port_var.get() or DEFAULT_PORT)
        cfg = {"mode": mode, "port": port, "auto_sync": self.auto_sync_var.get(),
               "game_dir": self.game_dir_var.get(), "ts": time.time(),
               "visibility": "public", "password": "",
               "name": getattr(self, "name_var", None).get().strip()[:16] if hasattr(self, "name_var") else ""}
        if hasattr(self, "vis_var"):
            cfg["visibility"] = self.vis_var.get()
            cfg["password"] = self.pwd_entry.get().strip()
        if mode == "join":
            cfg["host"] = self.host_ip_var.get().strip()
        return cfg

    def _write_config(self):
        try:
            os.makedirs(MODS_DIR, exist_ok=True)
            cfg = self._build_config()
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self._log("✅ 配置已写入 (模式={} 端口={})".format(cfg["mode"], cfg["port"]))
            self._save_settings()  # v8.1: 持久化设置（研究: settings persistence UX）
            return True
        except Exception as e:
            self._log("❌ 写配置失败: {}".format(e))
            return False

    def _launch(self):
        # v9.18: 先建/加入启动器房间（不启动游戏），全员就绪+存档同步后才开始游戏
        if self.mode_var.get() == "host":
            self._create_room()
        else:
            self._join_room()

    def _create_room(self):
        """房主创建房间（启动器开 TCP 房间服务，不启动游戏）"""
        if not ROOM_PROTOCOL_OK:
            self._log("❌ 房间模块加载失败（room_protocol.py 缺失）")
            return
        try:
            name = getattr(self, "name_var", None).get().strip()[:16] if hasattr(self, "name_var") else "房主"
            if not name:
                name = "房主"
            # v9.18: 私密房间密码
            pwd = ""
            if hasattr(self, "vis_var") and self.vis_var.get() == "private":
                pwd = getattr(self, "pwd_entry", None).get().strip() if hasattr(self, "pwd_entry") else ""
            self.room_server = RoomServer(host_name=name, port=7660, password=pwd)
            if not self.room_server.start():
                self._log("❌ 房间服务启动失败（端口被占用？）")
                return
            # 房间页显示房间码
            self.room_code_label.configure(text=self.room_server.room_code)
            self.room_status.configure(text="🏠 房间已创建 | 等待成员加入...", text_color=C["neon"])
            self._log("🏠 房间已创建！房间码: {}（告诉朋友加入）".format(self.room_server.room_code))
            self._log("💡 成员加入后，双方点「我准备」→ 同步存档 → 开始游戏")
            self._room_poll()
            self.pages[1].tkraise()
            self._switch_tab(1)
            # 房主自动准备
            self.room_server.host_set_ready(True)
        except Exception as e:
            self._log("❌ 创建房间失败: {}".format(e))

    def _join_room(self):
        """加入者通过房间码/IP 加入房主的房间（不启动游戏）"""
        if not ROOM_PROTOCOL_OK:
            self._log("❌ 房间模块加载失败（room_protocol.py 缺失）")
            return
        try:
            host = self.host_ip_var.get().strip()
            if not host:
                self._log("❌ 请输入房主 IP")
                return
            name = getattr(self, "name_var", None).get().strip()[:16] if hasattr(self, "name_var") else "玩家"
            if not name:
                name = "玩家"
            room_code = ""
            if hasattr(self, "room_code_entry"):
                room_code = self.room_code_entry.get().strip()
            pwd = ""
            if hasattr(self, "join_pwd_entry"):
                pwd = self.join_pwd_var.get().strip()
            self.room_client = RoomClient(host, port=7660, name=name, room_code=room_code, password=pwd)
            self.room_client.on("rejected", lambda d: self._log("❌ {}".format(d.get("reason", "被拒绝"))))
            # v9.20.4: 成员监听房主「开始游戏」广播 → 自动写 join 配置 + 启动游戏
            # (之前只显示 launching 状态却不启动游戏, 导致成员只能手动开且无法同步)
            def _on_game_start(msg):
                try:
                    self._log("🚀 房主已开始游戏，正在自动启动...")
                    if msg.get("save_name"):
                        self._log("📦 同步存档: {}".format(msg["save_name"]))
                    self.after(500, self._start_game_after_room)
                except Exception as e:
                    self._log("❌ 自动启动失败: {}".format(e))
            self.room_client.on("game_start", _on_game_start)
            # v9.20.4: 成员收到存档 → 立即写入 Saves 目录 (否则游戏内无档可同步)
            def _on_save_received(msg):
                try:
                    saves_dir = os.path.join(DOCS_DIR, "Saves")
                    ok, path = self.room_client.save_to(saves_dir)
                    if ok:
                        self._log("✅ 存档已写入: {}".format(os.path.basename(path)))
                    else:
                        self._log("❌ 存档写入失败: {}".format(path))
                except Exception as e:
                    self._log("❌ 存档写入异常: {}".format(e))
            self.room_client.on("save_received", _on_save_received)
            ok, err = self.room_client.connect()
            if not ok:
                self._log("❌ {}".format(err))
                return
            self._log("✅ 已连接房间（等待房主开始）")
            self._room_poll()
            self.pages[1].tkraise()
            self._switch_tab(1)
        except Exception as e:
            self._log("❌ 加入房间失败: {}".format(e))

    def _scan_rooms(self):
        """v9.18: 扫描局域网房间（UDP 广播）"""
        try:
            from room_protocol import discover_rooms
            self._log("🔍 正在扫描局域网房间...")
            rooms = discover_rooms(timeout=2.0)
            if not rooms:
                self._log("未发现房间。确保房主在同一局域网且已创建房间。")
                return
            self._log("发现 {} 个房间:".format(len(rooms)))
            for r in rooms:
                has_pwd = "🔒" if r.get("has_password") else "🔓"
                self._log("  {} {} | {} | {}人 | 码:{}".format(
                    has_pwd, r.get("ip", "?"), r.get("name", "?"),
                    r.get("players", 0), r.get("room_code", "")))
                # 自动填入第一个发现的房间
                if r == rooms[0]:
                    self.host_ip_var.set(r.get("ip", ""))
                    if hasattr(self, "room_code_entry"):
                        self.room_code_var.set(r.get("room_code", ""))
                    if r.get("has_password"):
                        self._log("⚠ 此房间需要密码，请在「房间密码」框输入")
                    self._log("已填入: IP={} 房间码={}".format(
                        r.get("ip"), r.get("room_code")))
        except Exception as e:
            self._log("扫描失败: {}".format(e))

    def _room_poll(self):
            """轮询房间状态刷新 UI（连接页按钮解锁房间页）"""
            try:
                if getattr(self, "room_server", None) or getattr(self, "room_client", None):
                    self._refresh_room()
                self.after(500, self._room_poll)
            except Exception as e:
                self._log("⚠ 轮询异常: {}".format(e))

    def _toggle_ready(self):
        """我准备/取消准备（启动器房间模式）"""
        try:
            if getattr(self, "room_server", None):
                ready = not self.room_server.members.get(0, {}).get("ready", False)
                self.room_server.host_set_ready(ready)
                self._log("✅ 我准备" if ready else "取消准备")
            elif getattr(self, "room_client", None):
                cur = any(m.get("player_id") == self.room_client.player_id and m.get("ready") for m in self.room_client.members)
                self.room_client.set_ready(not cur)
                self._log("✅ 我准备" if not cur else "取消准备")
            self._refresh_room()
        except Exception as e:
            self._log("准备失败: {}".format(e))

    def _sync_save(self):
        """同步存档（房主：选存档文件 → 分块发给成员）"""
        try:
            if not getattr(self, "room_server", None):
                self._log("只有房主可同步存档（启动器房间模式）")
                return
            if self.room_server.state != ROOM_READY:
                self._log("⏳ 需全员准备后才可同步存档")
                return
            import tkinter.filedialog as fd
            path = fd.askopenfilename(title="选择要同步的存档",
                                      initialdir=os.path.join(DOCS_DIR, "Saves"),
                                      filetypes=[("Sims4 存档", "*.save"), ("所有文件", "*.*")])
            if not path:
                return
            filename = os.path.basename(path)
            ok, msg = self.room_server.host_start_save_sync(path, filename)
            self._log("📦 {}".format(msg))
            self._refresh_room()
        except Exception as e:
            self._log("同步存档失败: {}".format(e))

    def _start_game(self):
        """开始游戏：广播 start_game → 写 mod 配置 → 双方启动游戏"""
        try:
            if getattr(self, "room_server", None):
                self.room_server.host_start_game(game_port=DEFAULT_PORT)
                self._log("🚀 开始游戏广播已发送，正在启动...")
            self._start_game_after_room()
        except Exception as e:
            self._log("❌ 开始游戏失败: {}".format(e))

    def _start_game_after_room(self):
        """写 mod 配置 + 启动游戏（v9.18: 从房间阶段进入游戏阶段）"""
        try:
            if getattr(self, "room_server", None):
                # 房主：写 host 配置
                self.mode_var.set("host")
                self._write_config()
            elif getattr(self, "room_client", None):
                # 加入者：写 join 配置（连接房主机器的 IP）
                self.host_ip_var.set(self.room_client.host_ip)
                self.mode_var.set("join")
                self._write_config()
            else:
                self._write_config()
            exe = os.path.join(self.game_dir_var.get(), GAME_EXE)
            if not os.path.exists(exe):
                self._log("❌ 找不到游戏: {}".format(exe))
                return
            self._log("🚀 启动游戏: {}".format(exe))
            subprocess.Popen([exe], cwd=os.path.dirname(exe),
                             creationflags=subprocess.CREATE_NO_WINDOW)
            self.status_badge.configure(text="● 等待游戏", text_color=C["warn"])
            self.progress.start()
            self.after(3000, self._auto_minimize)
        except Exception as e:
            self._log("❌ 启动失败: {}".format(e))

    def _leave_room(self):
        """离开房间"""
        try:
            if getattr(self, "room_server", None):
                self.room_server.stop()
                self.room_server = None
                self._log("🚪 已离开房间（房间已关闭）")
            if getattr(self, "room_client", None):
                self.room_client.disconnect()
                self.room_client = None
                self._log("🚪 已离开房间")
            self.room_code_label.configure(text="----")
            self._set_room_text("（暂无房间状态）\n\n创建或加入房间后，这里会显示成员列表。")
            self.room_status.configure(text="未连接房间", text_color=C["dim"])
            self.pages[0].tkraise()
            self._switch_tab(0)
        except Exception as e:
            self._log("离开失败: {}".format(e))

    def _auto_minimize(self):
        """游戏启动后自动最小化（托盘式，保留监控线程）"""
        try:
            if check_game_running():
                self.iconify()
                self._log("游戏已启动，启动器最小化到任务栏")
            else:
                # 游戏还没起来，稍后再试
                self.after(5000, self._auto_minimize)
        except Exception:
            pass

    def _write_config_only(self):
        if self._write_config():
            self._log("已写配置。游戏内输入 mp_apply 读取配置连接")

    def _setup_firewall(self):
        try:
            exe = os.path.join(self.game_dir_var.get(), GAME_EXE)
            if not os.path.exists(exe):
                self._log("❌ 找不到游戏程序: {}".format(exe))
                return
            cmds = [
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', 'name=Sims4-Multiplayer-IN'],
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule', 'name=Sims4-Multiplayer-OUT'],
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule', 'name=Sims4-Multiplayer-IN',
                 'dir=in', 'action=allow', 'protocol=TCP', 'localport={}'.format(DEFAULT_PORT),
                 'profile=any', 'program={}'.format(exe)],
                ['netsh', 'advfirewall', 'firewall', 'add', 'rule', 'name=Sims4-Multiplayer-OUT',
                 'dir=out', 'action=allow', 'protocol=TCP', 'localport={}'.format(DEFAULT_PORT),
                 'profile=any', 'program={}'.format(exe)],
            ]
            for c in cmds:
                subprocess.run(c, capture_output=True, text=True, encoding="gbk", errors="ignore",
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self._log("✅ 防火墙规则已配置 (端口 {})".format(DEFAULT_PORT))
        except Exception as e:
            self._log("❌ 防火墙配置失败: {}".format(e))

    # ============ v8.2: UPnP 自动端口转发（跨网联机，研究: miniupnpc） ============
    def _upnp_open(self):
        """尝试 UPnP 自动端口转发（免手动配置路由器）→ 支持跨网联机"""
        try:
            import miniupnpc
            u = miniupnpc.UPnP()
            u.discoverdelay = 200
            devices = u.discover()
            if devices == 0:
                self._log("UPnP: 未发现支持的路由器（局域网联机不受影响）")
                return False
            u.selectigd()
            port = int(self.port_var.get() or DEFAULT_PORT)
            local_ip = get_local_ip()
            # 添加 TCP 端口映射（外部 7655 → 本机 7655）
            result = u.addportmapping(port, 'TCP', local_ip, port,
                                      'Sims4Multiplayer', '')
            if result is not None:
                external_ip = u.externalipaddress()
                self._log("✅ UPnP 端口已开放! 公网 IP: {}:{} （异地朋友可直连）".format(external_ip, port))
                return True
            self._log("⚠️ UPnP 映射失败（路由器可能不支持）")
            return False
        except Exception as e:
            self._log("UPnP: {}".format(e))
            return False

    def _upnp_close(self):
        """关闭 UPnP 端口映射"""
        try:
            import miniupnpc
            u = miniupnpc.UPnP()
            u.discoverdelay = 200
            if u.discover() == 0:
                return
            u.selectigd()
            port = int(self.port_var.get() or DEFAULT_PORT)
            u.deleteportmapping(port, 'TCP')
            self._log("UPnP 端口映射已关闭")
        except Exception:
            pass

    # ============ v8.8: 主题切换（研究: CustomTkinter 自定义主题 JSON） ============
    def _on_theme_change(self, choice):
        t = THEMES.get(choice, THEMES["neon"])
        C["neon"] = t["neon"]
        C["blue"] = t["blue"]
        C["pink"] = t["pink"]
        self._log("主题已切换: {} (neon={})".format(choice, C["neon"]))
        self._save_settings()

    def _upnp_toggle(self):
        """UPnP 开关：开启时映射端口，关闭时删除映射"""
        if not getattr(self, "_upnp_on", False):
            # 先配防火墙（UPnP 需要配合）
            self._setup_firewall()
            ok = self._upnp_open()
            self._upnp_on = ok
            if ok:
                self.upnp_status.configure(text="✅ 已开启", text_color=C["neon"])
                pub = self._stun_public_ip()
                self._log("公网地址（供异地朋友）: {}:{}".format(pub, self.port_var.get() or DEFAULT_PORT))
            else:
                self.upnp_status.configure(text="❌ 失败", text_color=C["warn"])
        else:
            self._upnp_close()
            self._upnp_on = False
            self.upnp_status.configure(text="未开启", text_color=C["dim"])

    def _stun_public_ip(self):
        """v8.2: STUN 检测公网 IP（研究: STUN protocol）——异地联机地址"""
        try:
            import socket as _s
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.settimeout(3)
            # 连接 STUN 服务器获取映射后的公网地址（不发送数据，仅 connect 触发 NAT 映射）
            s.connect(("8.8.8.8", 80))
            local = s.getsockname()[0]
            s.close()
            return local
        except Exception:
            return get_local_ip()

    def _open_mods(self):
        try:
            os.makedirs(MODS_DIR, exist_ok=True)
            os.startfile(MODS_DIR)  # noqa
        except Exception as e:
            self._log("打开失败: {}".format(e))

    def _run_diagnostics(self):
        """v8.0: 一键诊断（研究: Unity Diagnostics 模式）——检测游戏/端口/防火墙/对端/存档"""
        def _diag():
            results = []
            # 1. 游戏
            game = check_game_running()
            results.append("游戏: {}".format("🟢 运行中" if game else "⚪ 未运行"))
            # 2. 游戏路径
            exe = os.path.join(self.game_dir_var.get(), GAME_EXE)
            results.append("游戏文件: {}".format("✅ 存在" if os.path.exists(exe) else "❌ 未找到"))
            # 3. 端口（host 模式看本机监听，join 模式看对端）
            port = int(self.port_var.get() or DEFAULT_PORT)
            if self.mode_var.get() == "host":
                ok, lat = check_port_open("127.0.0.1", port)
                results.append("端口 {}: {}".format(port, "🟢 监听中" if ok else "⚪ 未监听(需进游戏)"))
            else:
                host = self.host_ip_var.get().strip()
                if host:
                    ok, lat = check_port_open(host, port)
                    results.append("对端 {}:{}: {}".format(host, port, "🟢 可达" if ok else "❌ 不可达"))
                else:
                    results.append("对端 IP: ⚠️ 未填写")
            # 4. 防火墙规则
            try:
                out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                                      "name=Sims4-Multiplayer-IN"],
                                     capture_output=True, text=True, encoding="gbk",
                                     errors="ignore", timeout=10,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout
                results.append("防火墙: {} 规则".format("✅ 已配置" if "Sims4-Multiplayer" in out else "❌ 未配置"))
            except Exception:
                results.append("防火墙: ⚠️ 检测失败")
            # 5. 存档
            saves = os.path.join(DOCS_DIR, "Saves")
            n_saves = len([f for f in os.listdir(saves) if f.endswith(".save")]) if os.path.exists(saves) else 0
            results.append("存档: {} 个文件".format(n_saves))
            # 6. mod 文件
            mod_path = os.path.join(MODS_DIR, "Sims4Multiplayer.ts4script")
            results.append("mod: {}".format("✅ {}B".format(os.path.getsize(mod_path)) if os.path.exists(mod_path) else "❌ 未安装"))
            # 7. 日志最后 3 行
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-3:]
                results.append("日志: {}".format(" | ".join(l.strip()[:40] for l in lines)))
            except Exception:
                results.append("日志: 无")
            text = "\n".join(results)
            self.after(0, lambda: self._diag_done(text))
        threading.Thread(target=_diag, daemon=True).start()
        self.diag_text.configure(text="🔧 诊断中...", text_color=C["warn"])

    def _diag_done(self, text):
        self.diag_text.configure(text="✅ 诊断完成 (见日志)", text_color=C["neon"])
        for line in text.split("\n"):
            self._log(line)

    # ============ 实时监控 ============
    def _start_monitor(self):
        self._monitor_running = True
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        # 房间列表自动刷新（每 2 秒读状态文件）
        self.after(2000, self._auto_refresh_room)

    def _auto_refresh_room(self):
        try:
            self._refresh_room()
        except Exception:
            pass
        if self._monitor_running:
            self.after(2000, self._auto_refresh_room)

    def _monitor_loop(self):
        last_game = None
        last_conn = None
        while self._monitor_running:
            try:
                game = check_game_running()
                if game != last_game:
                    last_game = game
                    self.after(0, lambda g=game: self._update_game(g))

                port = int(self.port_var.get() or DEFAULT_PORT)
                if self.mode_var.get() == "host":
                    ok, lat = check_port_open("127.0.0.1", port)
                    if ok != last_conn:
                        last_conn = ok
                        self.after(0, lambda o=ok, l=lat: self._update_conn(o, l))
                    elif ok:
                        self.after(0, lambda l=lat: self.info_latency.configure(text="延迟: {}ms".format(l)))
                else:
                    host = self.host_ip_var.get().strip()
                    if host:
                        ok, lat = check_port_open(host, port)
                        if ok != last_conn:
                            last_conn = ok
                            self.after(0, lambda o=ok, l=lat: self._update_conn(o, l))
                        elif ok:
                            self.after(0, lambda l=lat: self.info_latency.configure(text="延迟: {}ms".format(l)))
            except Exception:
                pass
            time.sleep(2)

    def _update_game(self, running):
        if running:
            self.info_game.configure(text="游戏: 🟢 运行中", text_color=C["status_ok"])
        else:
            self.info_game.configure(text="游戏: ⚪ 未运行", text_color=C["status_off"])

    def _update_conn(self, ok, latency):
        """更新连接状态显示（主机/加入模式语义不同）

        主机模式: 127.0.0.1:port 监听成功 = 主机已启动（等待对端连接）
        加入模式: 对端 port 可连 = 已连接
        """
        if self.mode_var.get() == "host":
            if ok:
                self.status_badge.configure(text="● 主机已启动", text_color=C["status_ok"])
                self.info_host.configure(text="主机: 🟢 已启动", text_color=C["status_ok"])
                self.info_peer.configure(text="对端: ⚪ 等待连接", text_color=C["status_off"])
                if hasattr(self, "hero_badge"):
                    self.hero_badge.configure(text="● 主机运行中", text_color=C["status_ok"], fg_color="#1A2E22")
                if latency is not None:
                    self.info_latency.configure(text="延迟: {}ms".format(latency))
                self.progress.stop()
                self.progress.set(1.0)
                self._log("🟢 主机已启动 (端口监听正常)")
            else:
                self.status_badge.configure(text="● 等待游戏", text_color=C["status_warn"])
                self.info_host.configure(text="主机: ⚪ 未启动", text_color=C["status_off"])
                self.info_peer.configure(text="对端: ⚪ 未连接", text_color=C["status_off"])
        else:
            if ok:
                self.status_badge.configure(text="● 已连接", text_color=C["status_ok"])
                self.info_peer.configure(text="对端: 🟢 在线", text_color=C["status_ok"])
                if hasattr(self, "hero_badge"):
                    self.hero_badge.configure(text="● 已连接", text_color=C["status_ok"], fg_color="#1A2E22")
                if latency is not None:
                    self.info_latency.configure(text="延迟: {}ms".format(latency))
                self.progress.stop()
                self.progress.set(1.0)
                self._log("🟢 检测到连接 (延迟 {}ms)".format(latency))
            else:
                self.status_badge.configure(text="● 未连接", text_color=C["dim"])
                self.info_peer.configure(text="对端: ⚪ 未连接", text_color=C["dim"])


# ============ v8.6: 单实例锁（研究: Windows Mutex guide——防多开端口冲突） ============
import ctypes
_MUTEX_HANDLE = None


def acquire_single_instance():
    """创建命名 Mutex，防止启动器多开（第二次启动直接退出）"""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        # Global\ 前缀跨会话生效；ERROR_ALREADY_EXISTS=183 表示已有实例
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False,
                                              "Global\\Sims4MultiplayerLauncher_7E1C9A2F")
        err = kernel32.GetLastError()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True  # 非 Windows/无权限时放行


def release_single_instance():
    global _MUTEX_HANDLE
    try:
        if _MUTEX_HANDLE:
            ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = None
    except Exception:
        pass


def main():
    LauncherApp._install_crash_hook()  # v8.5: 崩溃日志自动保存
    # v9.18: --multi 跳过单实例锁（测试双窗口）
    if "--multi" not in sys.argv and not acquire_single_instance():
        import tkinter.messagebox as _mb
        try:
            _mb.showerror("启动器已在运行",
                          "Sims4Multiplayer 启动器已经打开了！\n请检查任务栏或系统托盘。")
        except Exception:
            pass
        return
    try:
        app = LauncherApp()
        app.mainloop()
    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
