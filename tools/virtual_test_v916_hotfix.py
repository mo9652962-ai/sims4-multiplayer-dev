# -*- coding: utf-8 -*-
"""v9.16 热修复测试：mp_host/mp_apply 的 NameError（network 未定义）→ 房主不显示

验证:
A. mp_host 核心流程（add_host_self 执行 → 房主进 members + 生成房间码）
B. mp_apply host 分支（auto-apply 建房同样正常）
C. mp_apply join 分支（加入流程无 NameError）
"""
import sys, os, json, time

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
sys.modules['sims4'].services = sys.modules['services']
sys.modules['autonomy'] = type('autonomy', (), {})
sys.modules['autonomy.settings'] = type('settings', (), {})
sys.modules['sims4.resources'] = type('resources', (), {})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import network

pc = fc = 0
failures = []
def check(name, cond, note=""):
    global pc, fc
    if cond: pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))

# mock 网络侧（不发真实网络）
network._log = lambda msg: None
network._notify = lambda msg: None
network._ensure_alarm = lambda: None
network._check_launcher_cmd = lambda: None
network._process_alarm_callback = lambda *a: None
network._start_host_discovery = lambda: None
network._ensure_alarm_retry = lambda: None
# mock 时钟同步
sys.modules['multimod.clock_sync'] = type('clock_sync', (), {
    'set_host_flag': lambda *a: None, '_install_clock_hook': lambda: None})
from multimod import lobby
lobby.LOBBY_STATE_PATH = os.path.join(os.path.dirname(__file__), "mp_state_hotfix.json")
if os.path.exists(lobby.LOBBY_STATE_PATH):
    os.remove(lobby.LOBBY_STATE_PATH)

# ============ A: mp_host 核心流程（真实调用，不 mock add_host_self） ============
print('[A] mp_host 建房流程')
network._network_thread = None
network._server_thread = lambda *a: None  # 不真启动 server
network.mp_host(port=None, visibility="public")
check('mp_host 无 NameError（房主进列表）', 0 in lobby._members,
      "members={}".format(list(lobby._members.keys())))
check('房间码已生成', len(lobby.ROOM_CODE) == 6, "code={}".format(lobby.ROOM_CODE))
check('状态文件有房主', os.path.exists(lobby.LOBBY_STATE_PATH))
if os.path.exists(lobby.LOBBY_STATE_PATH):
    st = json.load(open(lobby.LOBBY_STATE_PATH, encoding="utf-8"))
    check('状态 members 含房主', len(st.get("members", [])) == 1,
          "members={}".format([m.get("name") for m in st.get("members", [])]))
    check('状态 room_code 非空', st.get("room_code") == lobby.ROOM_CODE)

# ============ B: mp_apply host 分支 ============
print()
print('[B] mp_apply auto-apply 建房')
lobby._members = {}
lobby.ROOM_CODE = ""
lobby._write_state_file()
network._network_thread = None
cfg_path = os.path.join(os.path.dirname(__file__), "..", "src", "multimod", "..", "..", "mp_launcher_config.json")
# 写临时配置
import tempfile
tmp_cfg = os.path.join(os.path.dirname(__file__), "mp_launcher_config_test.json")
with open(tmp_cfg, "w", encoding="utf-8") as f:
    json.dump({"mode": "host", "port": 7655, "visibility": "public",
               "password": "", "auto_sync": False}, f)
network._load_launcher_config = lambda: json.load(open(tmp_cfg, encoding="utf-8"))
ok, msg = network._apply_launcher_config()
check('mp_apply host 无 NameError', ok is not False or "NameError" not in str(msg), "msg={}".format(msg))
check('mp_apply 后房主进列表', 0 in lobby._members,
      "members={}".format(list(lobby._members.keys())))
check('mp_apply 后房间码生成', len(lobby.ROOM_CODE) == 6)

# ============ C: mp_apply join 分支 ============
print()
print('[C] mp_apply 加入流程')
with open(tmp_cfg, "w", encoding="utf-8") as f:
    json.dump({"mode": "join", "host": "192.168.1.5", "port": 7655,
               "password": "", "auto_sync": False}, f)
network._network_thread = None
network._client_thread = lambda *a: None
try:
    network._apply_launcher_config()
    check('mp_apply join 无异常', True)
except Exception as e:
    check('mp_apply join 无异常', False, str(e))

os.remove(tmp_cfg)
if os.path.exists(lobby.LOBBY_STATE_PATH):
    os.remove(lobby.LOBBY_STATE_PATH)

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
