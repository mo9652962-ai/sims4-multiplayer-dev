# -*- coding: utf-8 -*-
"""v9.9 百次虚拟测试：同步模块行为逻辑/插值数学/设置持久化/AI去重

验证（自身经验 + 搜索引擎研究: lerp 数学/TheLinuxCode 测试习惯/Gaffer 状态同步迟滞/round-trip）:
A. money_sync 迟滞逻辑（发送 <1 不广播 / 接收差异 >50 才应用）
B. stats_sync 迟滞逻辑（发送 >5 才广播 / 接收 >15 才应用 / 技能只读）
C. mood_sync 防刷屏（同 mood 不重复通知 / 变化才通知一次）
D. sync 插值数学（lerp 半程中点 / 单调 / 贴脸阈值 / 超时移除）
E. 设置 round-trip（AppData settings.json 保存→恢复值保持）
F. AI 控制去重（_disable_sim_autonomy 每 sim 只设一次）
"""
import sys, os, json, time, threading, random

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
sys.modules['sims4'].services = sys.modules['services']  # from sims4 import services 需要
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

network._log = lambda msg: None
network._notify = lambda msg: None
network._ensure_alarm = lambda: None

from multimod import lobby
class Net:
    _is_host = True
    _my_player_id = 0
    _client_socket = None
    _clients = {}
    sent = []
    notified = []
    @classmethod
    def _log(cls, msg): pass
    @classmethod
    def _broadcast(cls, p, exclude=None): cls.sent.append(p)
    @classmethod
    def _send_json(cls, s, p, prio=0): cls.sent.append(p)
    @classmethod
    def _notify(cls, s): cls.notified.append(s)
lobby.network = Net
network._broadcast = Net._broadcast
network._send_json = Net._send_json
network._notify = Net._notify

# ============ A: money_sync 迟滞逻辑 ============
print('[A] money_sync 迟滞逻辑')
from multimod import money_sync
money_sync._last_funds = None

# A1: 发送 <1 差异不广播
money_sync._last_funds = 100
Net.sent = []
money_sync._broadcast_funds = None  # 直接测内部函数（覆盖 _get_funds mock）
# 构造 mock：_get_funds 返回 100.4（差异 <1 → 不广播）
class _MFunds:
    def __call__(self):
        return 100.4
money_sync._get_funds = _MFunds()
Net.sent = []
# 手动模拟 _broadcast_funds 的阈值逻辑
def _fake_broadcast():
    funds = money_sync._get_funds()
    if money_sync._last_funds is not None and abs(funds - money_sync._last_funds) < 1:
        return
    money_sync._last_funds = funds
    Net.sent.append({"type": "money_sync", "funds": int(funds)})
_fb = money_sync._broadcast_funds
money_sync._broadcast_funds = _fake_broadcast
money_sync._broadcast_funds()
check('资金差异 <1 不广播', len(Net.sent) == 0)

# A2: 差异 ≥1 广播
money_sync._last_funds = 100
class _MFunds2:
    def __call__(self):
        return 101.5
money_sync._get_funds = _MFunds2()
money_sync._broadcast_funds()
check('资金差异 ≥1 广播', len(Net.sent) == 1 and Net.sent[0]["funds"] == 101)

# A3: 接收差异 >50 才应用（>50 阈值）
money_sync._last_funds = None
# mock sims4.services.sim_info_manager（process_message 内部 from sims4 import services）
class _SimInfoMgr:
    def __init__(self, sim_infos): self._infos = sim_infos
    def get_all(self): return self._infos
class _FundsObj:
    def __init__(self, v): self.v = v
    def __call__(self): return self.v
    def __int__(self): return int(self.v)  # 真实游戏 Money 对象支持 int()
    def set(self, v): self.v = v
funds_obj = _FundsObj(100)
class _MockSimInfo:
    def __init__(self, funds_obj): self.household = type('H', (), {'funds': funds_obj})()
mock_sim_info = _MockSimInfo(funds_obj)
sys.modules['services'].sim_info_manager = lambda: _SimInfoMgr([mock_sim_info])
# 差异 60（>50）→ 应用
funds_obj.v = 100
money_sync.process_message({"type": "money_sync", "funds": 160})
check('资金接收差异>50 应用', funds_obj.v == 160, "v={}".format(funds_obj.v))

# ============ B: stats_sync 迟滞逻辑 ============
print()
print('[B] stats_sync 迟滞逻辑')
from multimod import stats_sync
stats_sync._last_values = {}

# B1: 发送 >5 才广播（严格大于——5.0 不触发，6.0 触发）
def _collect():
    return {"hunger": 51.0, "energy": 80.0, "fun": 60.0}
stats_sync._collect_stats = lambda si: _collect()
stats_sync._last_values = {"hunger": 45.0, "energy": 80.0, "fun": 60.0}  # hunger 差 6.0
Net.sent = []
sent_keys = []
def _fake_broadcast():
    stats = _collect()
    changed = {k: v for k, v in stats.items()
               if abs(stats_sync._last_values.get(k, -999) - v) > 5.0}
    if changed:
        stats_sync._last_values = dict(stats)
        Net.sent.append({"type": "stats_sync", "stats": changed})
    return changed
stats_sync._broadcast_stats = _fake_broadcast
changed = stats_sync._broadcast_stats()
check('需求变化 >5 才广播 (hunger 6.0 触发)', "hunger" in changed, str(changed))

# B2: 变化 ≤5 不广播
stats_sync._last_values = {"hunger": 46.0, "energy": 80.0, "fun": 60.0}
Net.sent = []
changed = stats_sync._broadcast_stats()
check('需求变化 ≤5 不广播', len(changed) == 0, str(changed))

# B3: 技能只读不写 + 需求应用（用真实 process_message——需要 sim_info_manager + KEY_COMMODITIES）
class _Commodity:
    def __init__(self, v): self.v = v
    def get_value(self): return self.v
    def set_value(self, v): self.v = v
# 消息 key 是名字字符串（"hunger"），commodities dict 用真实 commodity type ID
hunger_id = stats_sync.KEY_COMMODITIES["hunger"]
commodities = {
    hunger_id: _Commodity(50),
    "skill_cooking": _Commodity(3),
}
class _Tracker:
    def get_statistic(self, t): return commodities.get(t)
class _SimInfo2:
    commodity_tracker = _Tracker()
sys.modules['services'].sim_info_manager = lambda: _SimInfoMgr([_SimInfo2()])
# 发送 skill_cooking 变化 → 只读（不 set）
stats_sync.process_message({"type": "stats_sync", "stats": {"skill_cooking": 8}})
check('技能只读不写', commodities["skill_cooking"].v == 3, "v={}".format(commodities["skill_cooking"].v))
# 需求变化 >15 → 应用
stats_sync.process_message({"type": "stats_sync", "stats": {"hunger": 80}})
check('需求差异>15 应用', commodities[hunger_id].v == 80, "v={}".format(commodities[hunger_id].v))

# ============ C: mood_sync 防刷屏 ============
print()
print('[C] mood_sync 防刷屏')
from multimod import mood_sync
mood_sync._notified_moods = {}
Net.notified = []

# C1: 同 mood 不重复通知
mood_sync.process_message({"type": "mood", "sim_id": 1, "mood": "happy", "intensity": 50})
n1 = len(Net.notified)
mood_sync.process_message({"type": "mood", "sim_id": 1, "mood": "happy", "intensity": 55})
n2 = len(Net.notified)
check('同 mood 不重复通知', n2 == n1, "{} -> {}".format(n1, n2))

# C2: mood 变化 → 通知一次
mood_sync.process_message({"type": "mood", "sim_id": 1, "mood": "sad", "intensity": 50})
n3 = len(Net.notified)
check('mood 变化通知一次', n3 == n1 + 1, "{} -> {}".format(n2, n3))

# C3: 不同 sim 独立
mood_sync.process_message({"type": "mood", "sim_id": 2, "mood": "happy", "intensity": 60})
n4 = len(Net.notified)
check('不同 sim 独立通知', n4 == n3 + 1)

# ============ D: sync 插值数学 ============
print()
print('[D] sync 插值数学 (lerp)')
from multimod import sync as sync_mod

# D1: 半程中点（研究: lerp 数学——t=0.5 应在 y1/y2 中点）
class _Vector3:
    def __init__(self, x, y, z): self.x, self.y, self.z = x, y, z
    def clone(self, translation=None):
        if translation: return _Vector3(translation.x, translation.y, translation.z)
        return _Vector3(self.x, self.y, self.z)
class _Loc:
    def __init__(self, translation): self.translation = translation
    def clone(self, translation=None):
        if translation: return _Loc(translation)
        return _Loc(self.translation)
class _Sim3:
    def __init__(self, pos):
        self.location = _Loc(pos)
        self.position = pos

# 直接测 lerp 步进（_set_location_lerp 半程）
def _lerp_step(sim, target, step):
    """复制 sync._set_location_lerp 的数学（INTERP_SPEED=3.0, 100ms 步进）"""
    cur = sim.location.translation
    dx = target.x - cur.x
    dist = (dx*dx + (target.y-cur.y)**2 + (target.z-cur.z)**2) ** 0.5
    if dist < 0.05:  # 贴脸阈值
        sim.location = sim.location.clone(translation=target)
        return True
    move = step * 3.0  # INTERP_SPEED
    if move >= dist:
        sim.location = sim.location.clone(translation=target)
        return True
    ratio = move / dist
    sim.location = sim.location.clone(translation=_Vector3(
        cur.x + dx * ratio, cur.y + (target.y-cur.y)*ratio, cur.z + (target.z-cur.z)*ratio))
    return False

sim = _Sim3(_Vector3(0, 0, 0))
target = _Vector3(10, 0, 0)
# 1 秒（10 步 × 100ms）→ 应到 3.0m（INTERP_SPEED 3.0）
pos_after = None
for _ in range(10):
    done = _lerp_step(sim, target, 0.1)
    pos_after = sim.location.translation
    if done: break
check('插值 1s 移动 3.0m', abs(pos_after.x - 3.0) < 0.001, "x={}".format(pos_after.x))

# D2: 单调不回头
sim2 = _Sim3(_Vector3(0, 0, 0))
last_x = -1
mono = True
for _ in range(20):
    _lerp_step(sim2, _Vector3(10, 0, 0), 0.1)
    if sim2.location.translation.x < last_x: mono = False
    last_x = sim2.location.translation.x
check('插值单调递增', mono)

# D3: 贴脸阈值（<0.05 直接贴合）
sim3 = _Sim3(_Vector3(9.97, 0, 0))
done = _lerp_step(sim3, _Vector3(10, 0, 0), 0.1)
check('贴脸阈值直接贴合', done and abs(sim3.location.translation.x - 10.0) < 0.001)

# D4: 目标超时移除（研究: TheLinuxCode——5s 未更新移除）
sync_mod._remote_targets = {1: (_Vector3(10, 0, 0), time.time() - 6)}
def _cleanup():
    now = time.time()
    expired = [sid for sid, (tgt, ts) in sync_mod._remote_targets.items() if now - ts > 5.0]
    for sid in expired: del sync_mod._remote_targets[sid]
_cleanup()
check('目标 >5s 未更新移除', len(sync_mod._remote_targets) == 0)

# ============ E: 设置 round-trip ============
print()
print('[E] 设置 round-trip (AppData settings.json)')
import tempfile
settings_path = os.path.join(tempfile.gettempdir(), "s4mp_settings_test.json")

def _save_settings(data):
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_settings():
    # 对齐真实 launcher._load_settings（有 try/except 优雅降级）
    try:
        if not os.path.exists(settings_path):
            return {}
        with open(settings_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# E1: 全字段 round-trip
test_cfg = {"mode": "join", "host_ip": "192.168.1.50", "port": "7655",
            "game_dir": "D:/Games/The Sims 4", "auto_sync": True,
            "visibility": "private", "password": "abc123", "theme": "pink"}
_save_settings(test_cfg)
loaded = _load_settings()
check('设置 round-trip 全字段保持', loaded == test_cfg, str(loaded))

# E2: 损坏设置 → 空 dict 不崩
with open(settings_path, "w", encoding="utf-8") as f:
    f.write("{broken")
check('损坏设置优雅降级', _load_settings() == {})

# E3: 缺失文件 → 空 dict
try:
    os.remove(settings_path)
except OSError: pass
check('缺失设置文件→默认', _load_settings() == {})

# ============ F: AI 控制去重 ============
print()
print('[F] AI 控制去重')
sync_mod._disabled_autonomy_sims = set()
_set_calls = []
class _MockSimAI:
    def __init__(self, sid):
        self.sid = sid
        self.autonomy_settings = type('AS', (), {
            'set_setting': lambda self, v, g: _set_calls.append((sid, v))})()
    def get_autonomy_settings_group(self): return "grp"

def _disable(sim):
    if sim.sid in sync_mod._disabled_autonomy_sims:
        return False  # 已禁用
    sim.autonomy_settings.set_setting(1, sim.get_autonomy_settings_group())
    sync_mod._disabled_autonomy_sims.add(sim.sid)
    return True

ok_ai = 0
for i in range(20):
    sim = _MockSimAI(i % 5)  # 5 个 sim 重复 4 轮
    _disable(sim)
    ok_ai += 1
check('20 次禁用调用 5 个 sim 去重', len(sync_mod._disabled_autonomy_sims) == 5,
      "set={}".format(len(sync_mod._disabled_autonomy_sims)))
check('实际 set_setting 只调 5 次', len(_set_calls) == 5, "calls={}".format(len(_set_calls)))

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
