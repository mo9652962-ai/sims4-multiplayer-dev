# -*- coding: utf-8 -*-
"""v9.24 游戏内旅行自动跟随回归

A. 主机 zone 变化检测——首次读数建基线，变化触发 on_host_zone_changed
B. travel_follow 广播 + 时钟锁定 + 旅行激活 + 分级超时启动
C. 客机 on_travel_follow → 原生 send_travel_switch_to_zone_op 自动切 zone
D. 跟随开关关闭 → zone 变化被忽略
E. HOST_ONLY 含 travel_follow（防客机伪造跟随指令）
F. reset_room_state 重置 zone 基线
"""
import os
import sys
import threading
import time

os.environ["SIMSYNC_ALLOW_SELF"] = "1"

class _Commands:
    CommandType = type('CT', (), {'Live': 1, 'Cheat': 2})
    def Command(self, name, command_type=None):
        def deco(fn):
            return fn
        return deco
    def CheatOutput(self, conn):
        return lambda msg: None

class _Sims4:
    commands = _Commands()

sys.modules['sims4'] = _Sims4()
sys.modules['sims4.commands'] = _Sims4.commands
sys.modules['services'] = type('services', (), {})
sys.modules['autonomy'] = type('autonomy', (), {})
sys.modules['autonomy.settings'] = type('settings', (), {})
sys.modules['sims4.resources'] = type('resources', (), {})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import network  # noqa: E402
from multimod import lobby    # noqa: E402
from multimod import clock_sync  # noqa: E402

pc = fc = 0
failures = []

def check(name, cond, note=""):
    global pc, fc
    if cond:
        pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))

network._log = lambda msg: None
network._notify = lambda msg: None
network._ensure_alarm = lambda: None
lobby._notify = lambda msg: None

import tempfile  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="v924_")
lobby.LOBBY_STATE_PATH = os.path.join(_tmp, "state.json")

class _DummyTimer:
    def __init__(self, *a, **kw):
        pass
    def start(self):
        pass
    def cancel(self):
        pass
threading.Timer = _DummyTimer

_BCAST = []
network._broadcast = lambda p, exclude=None, tags=None: _BCAST.append(p)
_CLOCK = []
network._send_clock_broadcast = lambda s: _CLOCK.append(int(s))
clock_sync.get_current_speed = lambda: 2

# ============ A: zone 变化检测 ============
print('[A] 主机 zone 变化检测（基线 → 变化触发）')
network._is_host = True
network._last_zone_id = None
_zid = [10]
sys.modules['services'].current_zone_id = lambda: _zid[0]
network._check_zone_change()
check('首次读数只建基线', network._last_zone_id == 10 and _BCAST == [])
_zid[0] = 22
lobby._follow_travel = True
lobby._travel_active = False
lobby._members = {0: {"player_id": 0, "name": "host"}, 3: {"player_id": 3, "name": "me"}}
network._check_zone_change()
check('zone 变化被检测', network._last_zone_id == 22)

# ============ B: travel_follow 广播 + 时钟锁定 ============
print('[B] travel_follow 广播 + 时钟锁定')
tf = [p for p in _BCAST if p.get("type") == "travel_follow"]
check('广播 travel_follow(带 zone_id)', len(tf) == 1 and tf[0].get("zone_id") == 22,
      "bcast={}".format(tf))
check('旅行激活', lobby._travel_active is True)
check('时钟锁定 PAUSED', _CLOCK == [0], "clock={}".format(_CLOCK))
check('记住旅行前速度', lobby._travel_saved_speed == 2)

# 重复 zone 变化（旅行中）不重复触发
_BCAST.clear()
_zid[0] = 33
network._check_zone_change()
check('旅行中重复变化不重复触发',
      not any(p.get("type") == "travel_follow" for p in _BCAST))

# ============ C: 客机自动跟随（原生 API）============
print('[C] 客机 on_travel_follow → 原生 zone 切换')
_travel_ops = []
class _FakeSimInfo:
    def send_travel_switch_to_zone_op(self, zone_id=0):
        _travel_ops.append(zone_id)
class _FakeSim:
    sim_info = _FakeSimInfo()
class _FakeClient:
    active_sim = _FakeSim()
sys.modules['services'].client_manager = lambda: type('CM', (), {
    'get_first_client': staticmethod(lambda: _FakeClient())})()
network._is_host = False
lobby._travel_active = False
lobby.on_travel_follow({"type": "travel_follow", "zone_id": 22})
check('旅行激活(客机)', lobby._travel_active is True)
check('原生 travel op 被调用(zone=22)', _travel_ops == [22], "ops={}".format(_travel_ops))

# API 失败路径 → 返回 False 不炸
sys.modules['services'].client_manager = lambda: None
check('无 client 时安全失败', lobby._auto_travel_to_zone(5) is False)

# ============ D: 跟随开关 ============
print('[D] 跟随开关关闭')
network._is_host = True
lobby._follow_travel = False
lobby._travel_active = False
network._last_zone_id = None
_BCAST.clear()
_zid[0] = 44
network._check_zone_change()
network._check_zone_change()
_zid[0] = 55
network._check_zone_change()
check('关闭时不广播 travel_follow',
      not any(p.get("type") == "travel_follow" for p in _BCAST))
check('关闭时不激活旅行', lobby._travel_active is False)
lobby._follow_travel = True

# ============ E: HOST_ONLY 防伪造 ============
print('[E] HOST_ONLY 含 travel_follow')
check('travel_follow 在白名单', "travel_follow" in network.HOST_ONLY_TYPES)

# ============ F: 重置基线 ============
print('[F] reset_room_state 重置 zone 基线')
network._last_zone_id = 99
lobby.reset_room_state()
check('zone 基线被重置', network._last_zone_id is None)

# ============ 结果 ============
print()
print("=" * 50)
if failures:
    print("失败项:")
    for f_ in failures:
        print("  ❌", f_)
print("结果: {} 通过, {} 失败".format(pc, fc))
sys.exit(1 if fc else 0)
