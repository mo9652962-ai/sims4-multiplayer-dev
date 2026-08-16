# -*- coding: utf-8 -*-
"""v9.23 稳定性强化回归（研究: OneUptime 状态同步 / GamedevSE full-vs-delta）

A. 每 tick 消息处理预算——洪泛不卡死主线程，余量顺延
B. 周期性状态刷新——主机 30s 重播权威时钟，丢失更新自愈（幂等）
C. 旅行时钟锁定——go 时广播 PAUSED，到齐/超时恢复原速（防加载期时间漂移）
D. 状态文件健康度字段（RTT + 评级，启动器显示）
E. 网络线程看门狗——主机线程死亡自动重启监听
"""
import os
import sys
import threading
import time

os.environ["SIMSYNC_ALLOW_SELF"] = "1"

# ---- Mock 游戏依赖 ----
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
_tmp = tempfile.mkdtemp(prefix="v923_")
lobby.LOBBY_STATE_PATH = os.path.join(_tmp, "state.json")

# 旅行 force_start 会起 30/60/90s 真实 Timer（非 daemon → 测试进程挂 90s）——
# 换成空操作 Timer
class _DummyTimer:
    def __init__(self, *a, **kw):
        pass
    def start(self):
        pass
    def cancel(self):
        pass
threading.Timer = _DummyTimer

_CLOCK_SENT = []
network._send_clock_broadcast = lambda speed: _CLOCK_SENT.append(int(speed))

# ============ A: 每 tick 处理预算 ============
print('[A] 每 tick 消息处理预算（防洪泛卡帧）')
check('预算常量合理', 0 < network.MAX_MSGS_PER_TICK <= network.MAX_INCOMING_QUEUE)
for _ in range(network.MAX_MSGS_PER_TICK + 50):
    network._incoming_queue.put(({"type": "noop_budget_test"}, None))
network._process_incoming()
left = network._incoming_queue.qsize()
check('单 tick 处理量受限 (剩 {} 条顺延)'.format(left), left == 50,
      "left={}".format(left))
network._process_incoming()
check('下一 tick 清空余量', network._incoming_queue.qsize() == 0)

# ============ B: 周期性状态刷新 ============
print('[B] 周期性状态刷新（权威时钟 30s 重播自愈）')
_CLOCK_SENT.clear()
network._is_host = True
clock_sync.get_current_speed = lambda: 2
network._last_state_refresh = time.time() - 60  # 已过期
network._periodic_state_refresh()
check('过期后刷新广播时钟', _CLOCK_SENT == [2], "sent={}".format(_CLOCK_SENT))
network._periodic_state_refresh()
check('间隔内不重复刷新', _CLOCK_SENT == [2])
network._is_host = False
network._last_state_refresh = time.time() - 60
_CLOCK_SENT.clear()
network._periodic_state_refresh()
check('客机不刷新', _CLOCK_SENT == [])

# ============ C: 旅行时钟锁定 ============
print('[C] 旅行时钟锁定（PAUSED → 恢复原速）')
network._is_host = True
clock_sync.get_current_speed = lambda: 3
lobby._travel_pending = True
lobby._travel_acks = {0}
lobby._members = {0: {"player_id": 0, "name": "host"}, 3: {"player_id": 3, "name": "me"}}
_CLOCK_SENT.clear()
lobby._travel_force_start()
check('go 时广播 PAUSED(0)', _CLOCK_SENT == [0], "sent={}".format(_CLOCK_SENT))
check('记住旅行前速度', lobby._travel_saved_speed == 3)
check('旅行激活', lobby._travel_active is True)
_CLOCK_SENT.clear()
lobby._travel_arrived = {0, 3}
network._clients = {}
network._send_world_snapshot = lambda sock: None
lobby._check_travel_all_arrived()
check('到齐后恢复原速(3)', 3 in _CLOCK_SENT, "sent={}".format(_CLOCK_SENT))
# 超时路径也要恢复
lobby._travel_active = True
lobby._travel_arrived = {0}
_CLOCK_SENT.clear()
lobby._travel_progress_check(True)
check('超时后恢复原速(3)', 3 in _CLOCK_SENT, "sent={}".format(_CLOCK_SENT))
check('超时解锁', lobby._travel_active is False)

# ============ D: 状态文件健康度 ============
print('[D] 状态文件健康度字段')
network.get_rtt_ms = lambda: 42.56
network.get_health_label = lambda: "🟢 优秀"
lobby._write_state_file()
import json  # noqa: E402
with open(lobby.LOBBY_STATE_PATH, "r", encoding="utf-8") as f:
    _st = json.load(f)
h = _st.get("health") or {}
check('health.rtt_ms 写入', h.get("rtt_ms") == 42.6, "h={}".format(h))
check('health.label 写入', h.get("label") == "🟢 优秀")

# ============ E: 网络线程看门狗 ============
print('[E] 网络线程看门狗')
_restarts = []
network._server_thread = lambda port=7655: _restarts.append(port)
_dead = threading.Thread(target=lambda: None)
_dead.start()
_dead.join()
network._is_host = True
network._server_socket = object()  # server 还开着但线程死了
network._network_thread = _dead
network._host_port = 7655
network._last_watchdog_check = 0
network._network_watchdog()
check('主机线程死亡 → 重启监听', _restarts == [7655], "restarts={}".format(_restarts))
network._last_watchdog_check = 0
network._network_thread = threading.Thread(target=lambda: None)  # 存活线程（未启动视为存活? is_alive False!）
# 未启动线程 is_alive()=False 会被误判——用已启动存活的线程
_alive = threading.Thread(target=lambda: time.sleep(2))
_alive.start()
network._network_thread = _alive
network._last_watchdog_check = 0
_restarts.clear()
network._network_watchdog()
check('线程存活不重启', _restarts == [])

# ============ 结果 ============
print()
print("=" * 50)
if failures:
    print("失败项:")
    for f_ in failures:
        print("  ❌", f_)
print("结果: {} 通过, {} 失败".format(pc, fc))
sys.exit(1 if fc else 0)
