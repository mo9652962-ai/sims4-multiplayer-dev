# -*- coding: utf-8 -*-
"""v9.25 旅行代次（_travel_gen）回归——连续旅行/快速切场景的定时器竞态

场景：旅行A进行中（30/60/90s Timer 挂着）→ 旅行A到齐/超时 → 玩家立刻又旅行
（旅行B）→ 旅行A的残留 Timer 触发。旧实现：提前解锁 B / 乱发 travel_missing /
错乱恢复时钟。v9.25：集中取消 + 代次守卫双保险。

A. 旅行开始 → 3 个分级 Timer 创建且可被追踪
B. 正常到齐 → Timer 全部取消（旧实现残留——竞态源头）
C. 游离旧代次 Timer 触发 → no-op（不解锁/不广播/不恢复时钟）
D. 当前代次 final 检查 → 正常解锁+广播+恢复时钟
E. reset_room_state → 取消全部 + 代次作废
F. 旅行中再触发 zone 变化 → 跳过（不叠加旅行）
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
_tmp = tempfile.mkdtemp(prefix="v925_")
lobby.LOBBY_STATE_PATH = os.path.join(_tmp, "state.json")


class _FakeTimer:
    """可手动触发/记录取消的 Timer 替身"""
    instances = []
    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.cancelled = False
        self.fired = False
        _FakeTimer.instances.append(self)
    def start(self):
        pass
    def cancel(self):
        self.cancelled = True
    def fire(self):
        if not self.cancelled and not self.fired:
            self.fired = True
            self.fn()


threading.Timer = _FakeTimer

_BCAST = []
network._broadcast = lambda p, exclude=None, tags=None: _BCAST.append(p)
_CLOCK = []
network._send_clock_broadcast = lambda s: _CLOCK.append(int(s))
network._send_world_snapshot = lambda sock: None
clock_sync.get_current_speed = lambda: 2
network._is_host = True
network._clients = {}
lobby._members = {0: {"player_id": 0, "name": "host"}, 3: {"player_id": 3, "name": "me"}}

# ============ A: 旅行开始 → Timer 追踪 ============
print('[A] 旅行开始（force_start）→ 3 个分级 Timer')
lobby._travel_pending = True
lobby._travel_acks = {0}
gen_before = lobby._travel_gen
_FakeTimer.instances = []
lobby._travel_force_start()
check('代次自增', lobby._travel_gen == gen_before + 1)
check('旅行激活', lobby._travel_active is True)
check('3 个 Timer 创建', len(lobby._travel_timers) == 3,
      "timers={}".format(len(lobby._travel_timers)))
gen_a = lobby._travel_gen

# ============ B: 正常到齐 → Timer 全部取消 ============
print('[B] 全员到齐 → 旧 Timer 集中取消（竞态源头）')
lobby._travel_arrived = {0, 3}
lobby._check_travel_all_arrived()
check('到齐解锁', lobby._travel_active is False)
check('旧 Timer 全部取消', all(t.cancelled for t in lobby._travel_timers))

# ============ C: 游离旧代次 Timer → no-op ============
print('[C] 新旅行中，旧代次(gen={}) Timer 触发 → no-op'.format(gen_a))
# 玩家立刻又旅行（zone 变化路径）
lobby._travel_active = False
lobby.on_host_zone_changed(88)
gen_b = lobby._travel_gen
check('新旅行代次再自增', gen_b == gen_a + 1)
check('新旅行激活', lobby._travel_active is True)
_BCAST.clear()
_CLOCK.clear()
# 旧旅行 A 的 90s Timer 现在触发（fire 保留的旧实例——cancel 后 fire 无效，
# 更险恶的场景：绕过 cancel 直接调回调等价于旧代次直调）
lobby._travel_progress_check(True, gen=gen_a)
check('旧代次不解锁新旅行', lobby._travel_active is True)
check('旧代次不广播 travel_missing',
      not any(p.get("type") == "travel_missing" for p in _BCAST))
check('旧代次不恢复时钟', _CLOCK == [], "clock={}".format(_CLOCK))

# ============ D: 当前代次 final → 正常终局 ============
print('[D] 当前代次(gen={}) final 检查 → 解锁+广播+恢复时钟'.format(gen_b))
_BCAST.clear()
_CLOCK.clear()
lobby._travel_arrived = {0}  # me 没到
lobby._travel_progress_check(True, gen=gen_b)
check('当前代次正常解锁', lobby._travel_active is False)
check('广播 travel_missing',
      any(p.get("type") == "travel_missing" for p in _BCAST))
check('恢复时钟(速度2)', _CLOCK == [2], "clock={}".format(_CLOCK))

# ============ E: reset → 取消全部+代次作废 ============
print('[E] reset_room_state')
lobby._travel_active = True
_FakeTimer.instances = []
lobby._travel_pending = True
lobby._travel_acks = {0}
lobby._travel_force_start()
gen_c = lobby._travel_gen
lobby.reset_room_state()
check('reset 取消全部 Timer', all(t.cancelled for t in lobby._travel_timers))
check('reset 后无激活旅行', lobby._travel_active is False)
lobby._travel_active = True
lobby._travel_progress_check(True, gen=gen_c)
check('reset 前代次已被作废', lobby._travel_active is True,
      '旧代次检查不应生效')
lobby._travel_active = False

# ============ F: 旅行中 zone 变化 → 跳过 ============
print('[F] 旅行中再触发 zone 变化 → 跳过')
lobby._travel_active = True
gen_keep = lobby._travel_gen
_BCAST.clear()
lobby.on_host_zone_changed(99)
check('旅行中不叠加新旅行', lobby._travel_gen == gen_keep)
check('旅行中不广播 travel_follow',
      not any(p.get("type") == "travel_follow" for p in _BCAST))
lobby._travel_active = False

# ============ 结果 ============
print()
print("=" * 50)
if failures:
    print("失败项:")
    for f_ in failures:
        print("  ❌", f_)
print("结果: {} 通过, {} 失败".format(pc, fc))
sys.exit(1 if fc else 0)
