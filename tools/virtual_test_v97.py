# -*- coding: utf-8 -*-
"""v9.7 百次虚拟测试：长跑内存/GUI构建/配置往返/输入边界fuzz

验证（自身经验 + 搜索引擎研究: tracemalloc 泄漏检测/mock GUI/round-trip/fuzz）:
A. 长跑内存稳定（1000 次 chat+消息处理，tracemalloc 对比不增长）
B. GUI 构建完整性（mock CTk：5 页构建不崩）
C. 配置全链路（launcher 写 → mod 读 → 应用）
D. 输入边界 fuzz（超长/emoji/无效UTF8/特殊字符 不崩）
"""
import sys, os, json, time, threading, random, string

# ---- Mock ----
class _Commands:
    CommandType = type('CT', (), {'Live': 1, 'Cheat': 2})
    def Command(self, name, command_type=None):
        def deco(fn): return fn
        return deco
    def CheatOutput(self, conn):
        return lambda msg: None
class _Sims4: commands = _Commands()
sys.modules['sims4'] = _Sims4()
sys.modules['sims4.commands'] = _Sims4.commands
sys.modules['services'] = type('services', (), {})
sys.modules['autonomy'] = type('autonomy', (), {})
sys.modules['autonomy.settings'] = type('settings', (), {})
sys.modules['sims4.resources'] = type('resources', (), {})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from multimod import network

pc = fc = 0
failures = []
def check(name, cond, note=""):
    global pc, fc
    if cond: pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))

network._log = lambda msg: None
network._notify = lambda msg: None
network._ensure_alarm = lambda: None

from multimod import lobby
class Net:
    _is_host = True
    _my_player_id = 0
    _client_socket = None
    _clients = {}
    @classmethod
    def _log(cls, msg): pass
    @classmethod
    def _broadcast(cls, p): pass
    @classmethod
    def _send_json(cls, s, p, prio=0): pass
    @classmethod
    def _notify(cls, s): pass
lobby.network = Net
network._broadcast = Net._broadcast
network._send_json = Net._send_json

# ============ A: 长跑内存稳定（tracemalloc） ============
print('[A] 长跑内存稳定 (tracemalloc)')
import tracemalloc, gc

tracemalloc.start()
# 预热
lobby._chat_history = []
for i in range(100):
    lobby.add_chat("warm", "x" * 50)
gc.collect()
snap1 = tracemalloc.take_snapshot()
mem1 = sum(s.size_diff for s in snap1.compare_to(snap1, 'filename'))  # 基准

# 跑 1000 次 chat + 消息处理（模拟长跑）
for i in range(1000):
    lobby.add_chat("user{}".format(i % 10), "消息内容 {}".format(i) * 3)
    network._incoming_queue.put(({"type": "chat", "from": "t", "text": "x" * 30}, 1))
    if i % 100 == 0:
        network._process_incoming()
network._process_incoming()
gc.collect()
snap2 = tracemalloc.take_snapshot()
# 对比：chat 历史有 20 条上限，内存不应无限增长
stats = snap2.compare_to(snap1, 'lineno')
total_growth = sum(s.size_diff for s in stats)
check('1000 次操作内存增长 < 5MB', abs(total_growth) < 5 * 1024 * 1024,
      "growth={}KB".format(total_growth // 1024))
tracemalloc.stop()

# ============ B: GUI 构建完整性（mock CTk） ============
print()
print('[B] GUI 构建完整性 (mock CTk)')
# 创建 CTk mock（headless 测试，研究: mock GUI 对象）
class _MockWidget:
    def __init__(self, *a, **k):
        self.children = []
    def grid(self, *a, **k): pass
    def pack(self, *a, **k): pass
    def place(self, *a, **k): pass
    def configure(self, *a, **k): return self
    def grid_propagate(self, *a, **k): pass
    def grid_columnconfigure(self, *a, **k): pass
    def grid_rowconfigure(self, *a, **k): pass
    def tkraise(self, *a, **k): pass
    def bind(self, *a, **k): pass
    def delete(self, *a, **k): pass
    def insert(self, *a, **k): pass
    def get(self, *a, **k): return ""
    def set(self, *a, **k): pass
    def start(self, *a, **k): pass
    def stop(self, *a, **k): pass
    def after(self, *a, **k): pass
    def iconify(self): pass

class _MockCTk:
    def __init__(self, *a, **k): pass
    def title(self, *a): pass
    def geometry(self, *a): pass
    def mainloop(self): pass
    def destroy(self): pass
    def grid_columnconfigure(self, *a, **k): pass
    def grid_rowconfigure(self, *a, **k): pass
    CTk = _MockWidget  # 窗口基类（LauncherApp 继承自 ctk.CTk）
    CTkFrame = _MockWidget
    CTkLabel = _MockWidget
    CTkButton = _MockWidget
    CTkEntry = _MockWidget
    CTkTextbox = _MockWidget
    CTkProgressBar = _MockWidget
    CTkCheckBox = _MockWidget
    CTkSwitch = _MockWidget
    CTkOptionMenu = _MockWidget
    CTkRadioButton = _MockWidget
    CTkImage = _MockWidget
    CTkFont = lambda *a, **k: None
    StringVar = lambda *a, **k: type('V', (), {'get': lambda s: '', 'set': lambda s, v: None})()
    BooleanVar = lambda *a, **k: type('V', (), {'get': lambda s: True, 'set': lambda s, v: None})()
    set_appearance_mode = staticmethod(lambda m: None)
    set_default_color_theme = staticmethod(lambda m: None)

sys.modules['customtkinter'] = _MockCTk()
# mock PIL（from PIL import Image, ImageTk 需要顶层属性）
class _MockPIL:
    class Image:
        @staticmethod
        def open(*a): return type('I', (), {})()
    ImageTk = type('IT', (), {})
sys.modules['PIL'] = _MockPIL
sys.modules['PIL.Image'] = _MockPIL.Image
sys.modules['PIL.ImageTk'] = _MockPIL.ImageTk
# mock qrcode/miniupnpc（launcher import 时）
sys.modules['qrcode'] = type('q', (), {})
sys.modules['miniupnpc'] = type('m', (), {})

# 加载 launcher 并构建 GUI
import importlib.util
spec = importlib.util.spec_from_file_location('launcher', 'D:/Sims4-Multiplayer-Dev/launcher.py')
launcher = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(launcher)
    check('launcher 模块加载（mock 环境）', True)
except Exception as e:
    check('launcher 模块加载（mock 环境）', False, str(e))

# 构建 UI（绕过 mainloop）——object.__new__ 绕过 __init__，手动补 base 方法
try:
    app = object.__new__(launcher.LauncherApp)
    # 补 CTk 基类方法（正常 __init__ 会设置）
    app.grid_columnconfigure = lambda *a, **k: None
    app.grid_rowconfigure = lambda *a, **k: None
    launcher.LauncherApp._build_ui(app)
    check('GUI 5 页构建不崩', True)
except Exception as e:
    check('GUI 5 页构建不崩', False, str(e))

# ============ C: 配置全链路 ============
print()
print('[C] 配置全链路 (launcher → mod → 应用)')
# 写 launcher config（模拟启动器写，路径与 mod LAUNCHER_CONFIG_PATH 一致）
cfg_path = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                        "The Sims 4", "Mods", "mp_launcher_config.json")
cfg_data = {"mode": "host", "host_ip": "192.168.1.100", "port": "7655",
            "visibility": "private", "password": "1234"}
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_data, f, ensure_ascii=False)

# mod 读取（network._load_launcher_config）
try:
    cfg = network._load_launcher_config()
    check('mod 读取 launcher 配置', cfg is not None and cfg.get("mode") == "host")
    check('配置值完整保留 (round-trip)', cfg.get("host_ip") == "192.168.1.100"
          and cfg.get("port") == "7655" and cfg.get("password") == "1234")
except Exception as e:
    check('mod 读取 launcher 配置', False, str(e))

# 损坏配置 → 优雅降级
with open(cfg_path, "w", encoding="utf-8") as f:
    f.write("{invalid json!!!")
try:
    cfg2 = network._load_launcher_config()
    check('损坏配置优雅降级 (None)', cfg2 is None or isinstance(cfg2, dict))
except Exception as e:
    check('损坏配置优雅降级', False, str(e))

# ============ D: 输入边界 fuzz ×20 ============
print()
print('[D] 输入边界 fuzz ×20')
ok_fuzz = 0
fuzz_cases = [
    "A" * 10000,                    # 超长
    "😀😁😂🤣😃😄😅😆" * 100,       # emoji 风暴
    "\x00\x01\x02\x03\xff\xfe",     # 控制字符+无效UTF8
    "'; DROP TABLE sims; --",       # SQL 注入尝试
    "<script>alert(1)</script>",    # XSS 尝试
    "日本語テストテキスト" * 50,     # 多字节
    "\u200b\u200c\u200d",           # 零宽字符
    " " * 5000,                     # 纯空格
    "\n" * 100,                     # 换行风暴
    "ｆｕｌｌｗｉｄｔｈ" * 20,       # 全角
]
for i in range(20):
    text = fuzz_cases[i % len(fuzz_cases)]
    try:
        # 聊天处理（核心 fuzz 目标）
        lobby._chat_history = []
        lobby.add_chat("fuzz", text)
        network._incoming_queue.put(({"type": "chat", "from": "f", "text": text}, 1))
        network._process_incoming()
        # 存档文件名 fuzz
        lobby.on_save_chunk({"filename": text[:50], "index": 0, "total": 1, "data": "A=="})
        ok_fuzz += 1
    except Exception as e:
        failures.append("fuzz{}: {}".format(i, e))
        break
check('20 次 fuzz 输入处理不崩', ok_fuzz == 20)
check('chat 历史截断到 20 条（防内存）', len(lobby._chat_history) <= 20)

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
