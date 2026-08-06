# -*- coding: utf-8 -*-
"""v9.11 场景切换/旅行专项百次测试（研究增强验证）

验证（研究: S4MP 0.5.2 无限加载修复 + SimSync "travel together" + 黑屏恢复）:
A. 旅行完整生命周期（req→ack→go→arrived→all_arrived→解锁）×20
B. 抵达报告真实链路（房主收集→全员→广播）
C. 旅行锁定（_travel_active 期间 sync 暂停）
D. 超时恢复（travel_missing：部分未抵达→提示+解锁）
E. 幂等与异常（重复 arrived / 未旅行时报 arrived / 非房主）
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

# 阻止真实 Timer 启动（避免测试挂起）——no-op（不触发，避免时序竞争）
# v9.14: 之前立即执行会改变旅行时序（10s 强制变 0.01s），改不执行
_orig_timer = threading.Timer
class _NoopTimer:
    def __init__(self, *a, **k): pass
    def start(self): pass
    def cancel(self): pass
threading.Timer = _NoopTimer

# ============ A: 旅行完整生命周期（host 视角） ============
print('[A] 旅行完整生命周期（host）req→ack→go→arrived→all_arrived')
lobby._members = {0: {'name': 'host', 'player_id': 0, 'is_host': True, 'in_lot': True},
                  1: {'name': 'b', 'player_id': 1, 'is_host': False, 'in_lot': True}}
Net._is_host = True
Net._my_player_id = 0
network._is_host = True
network._my_player_id = 0

# A1: 发起旅行 → req + pending + 房主已确认
Net.sent = []
lobby._travel_pending = False
lobby._travel_acks = set()
lobby._travel_active = False
lobby.host_start_travel()
check('发起→travel_req 广播', any(p.get("type") == "travel_req" for p in Net.sent))
check('发起→pending=True', lobby._travel_pending is True)

# A2: 成员确认 → 全确认自动 go（含 in_lot 重置）
Net.sent = []
lobby.on_travel_ack(1)
check('全确认→travel_go 广播', any(p.get("type") == "travel_go" for p in Net.sent))
check('全确认→travel_active 锁定', lobby._travel_active is True)
check('旅行开始→in_lot 重置', all(m["in_lot"] is False for m in lobby._members.values()))

# A3: 房主抵达 → 自己记录 + 未全抵达（等成员）
Net.sent = []
lobby.report_travel_arrived(12345)
check('房主抵达→in_lot=True', lobby._members[0]["in_lot"] is True)
check('房主抵达→未广播 all_arrived（成员未到）',
      not any(p.get("type") == "travel_all_arrived" for p in Net.sent))

# A4: 成员抵达 → 全到 → all_arrived + 解锁
Net.sent = []
lobby.on_travel_arrived(1, 12345)
check('成员抵达→all_arrived 广播', any(p.get("type") == "travel_all_arrived" for p in Net.sent))
check('全员抵达→解锁', lobby._travel_active is False)

# A5: 完整流程 20 次循环（req→ack→go→arrived×2→all→解锁）
ok_life = True
for i in range(20):
    try:
        lobby._members = {0: {'name': 'host', 'player_id': 0, 'is_host': True, 'in_lot': True},
                          1: {'name': 'b', 'player_id': 1, 'is_host': False, 'in_lot': True}}
        lobby._travel_pending = False
        lobby._travel_acks = set()
        lobby._travel_active = False
        lobby._travel_arrived = set()
        lobby.host_start_travel()
        lobby.on_travel_ack(1)
        assert lobby._travel_active is True
        lobby.report_travel_arrived(1000 + i)
        lobby.on_travel_arrived(1, 1000 + i)
        assert lobby._travel_active is False
    except Exception:
        ok_life = False
        break
check('旅行完整流程 20 次稳定', ok_life)

# ============ B: 抵达报告真实链路（客机视角） ============
print()
print('[B] 抵达报告真实链路（client）')
# 模拟客机
Net._is_host = False
Net._my_player_id = 5
network._is_host = False
network._my_player_id = 5
lobby._members = {0: {'name': 'host', 'player_id': 0, 'is_host': True, 'in_lot': False},
                  5: {'name': 'me', 'player_id': 5, 'is_host': False, 'in_lot': False}}
Net._client_socket = object()

# B1: 客机收到 go → 锁定
lobby.on_travel_go({"ts": time.time()})
check('客机收到 go→锁定', lobby._travel_active is True)

# B2: 客机抵达 → 发 travel_arrived 给房主
Net.sent = []
lobby.report_travel_arrived(999)
sent_arr = [p for p in Net.sent if p.get("type") == "travel_arrived"]
check('客机抵达→发 travel_arrived', len(sent_arr) == 1 and sent_arr[0]["zone_id"] == 999)
check('客机抵达→in_lot=True', lobby._members[5]["in_lot"] is True)

# B3: 客机收到 all_arrived → 解锁
lobby._travel_active = True
lobby.on_travel_all_arrived({"ts": time.time()})
check('客机收 all_arrived→解锁', lobby._travel_active is False)

# B4: 20 次循环（go→arrived→all）
ok_client = True
for i in range(20):
    try:
        Net.sent = []
        lobby._travel_active = True
        lobby.report_travel_arrived(i)
        assert any(p.get("type") == "travel_arrived" for p in Net.sent)
        lobby.on_travel_all_arrived({"ts": time.time()})
        assert lobby._travel_active is False
    except Exception:
        ok_client = False
        break
check('客机链路 20 次稳定', ok_client)

# ============ C: 旅行锁定（sync 暂停） ============
print()
print('[C] 旅行锁定（sync 暂停）')
# 模拟 sync._sync_loop 的锁定检查
lobby._travel_active = True
paused = False
try:
    from multimod import sync as sync_mod
    sync_mod.network = Net
    if hasattr(lobby, "is_travel_active") and lobby.is_travel_active():
        paused = True
except Exception:
    pass
check('旅行锁定→sync 暂停', paused is True)

# 解锁后恢复
lobby._travel_active = False
resumed = not lobby.is_travel_active()
check('解锁→sync 恢复', resumed)

# ============ D: 超时恢复（travel_missing） ============
print()
print('[D] 超时恢复（travel_missing）')
# 房主视角：go 后 90s 未到齐
Net._is_host = True
Net._my_player_id = 0
network._is_host = True
network._my_player_id = 0
lobby._members = {0: {'name': 'host', 'player_id': 0, 'is_host': True, 'in_lot': False},
                  1: {'name': 'b', 'player_id': 1, 'is_host': False, 'in_lot': False}}
lobby._travel_active = True
lobby._travel_arrived = {0}  # 只有房主到了
Net.sent = []
lobby._travel_check_missing()
sent_miss = [p for p in Net.sent if p.get("type") == "travel_missing"]
check('超时未到齐→travel_missing 广播', len(sent_miss) == 1 and "b" in sent_miss[0].get("missing", []))
check('超时→解锁', lobby._travel_active is False)

# 已到齐 → 不广播 missing
lobby._travel_active = True
lobby._travel_arrived = {0, 1}
Net.sent = []
lobby._travel_check_missing()
check('已到齐→不广播 missing', not any(p.get("type") == "travel_missing" for p in Net.sent))
lobby._travel_active = False

# ============ E: 幂等与异常 ============
print()
print('[E] 幂等与异常')
# E1: 重复 arrived → 幂等（不重复通知崩溃）
try:
    lobby._travel_active = True
    lobby._travel_arrived = {0}
    lobby.on_travel_arrived(1, 5)
    lobby.on_travel_arrived(1, 5)  # 重复
    check('重复 arrived 幂等', True)
except Exception:
    check('重复 arrived 幂等', False)
lobby._travel_active = False

# E2: 未旅行时报 arrived → 不崩
try:
    lobby._travel_active = False
    lobby.report_travel_arrived(7)
    check('未旅行时报 arrived 不崩', True)
except Exception:
    check('未旅行时报 arrived 不崩', False)

# E3: 非房主 on_travel_arrived → 忽略（不崩）
try:
    Net._is_host = False
    network._is_host = False
    lobby.on_travel_arrived(9, 3)
    check('非房主收 arrived 忽略', True)
except Exception:
    check('非房主收 arrived 忽略', False)

# E4: 客机收到 travel_missing → 提示+解锁
Net._is_host = False
network._is_host = False
lobby._travel_active = True
try:
    lobby.on_travel_missing({"missing": ["b"]})
    check('客机收 missing→解锁', lobby._travel_active is False)
except Exception:
    check('客机收 missing→解锁', False)

# E5: process_message 分发完整（真实队列路径）
try:
    lobby._members = {0: {'name': 'host', 'player_id': 0, 'is_host': True, 'in_lot': False},
                      1: {'name': 'b', 'player_id': 1, 'is_host': False, 'in_lot': False}}
    Net._is_host = True
    network._is_host = True
    lobby._travel_active = True
    lobby._travel_arrived = {0}
    # travel_arrived 走 lobby.process_message
    lobby.process_message({"type": "travel_arrived", "zone_id": 42}, sender_pid=1)
    check('process_message 分发 arrived', 1 in lobby._travel_arrived)
    check('分发后 in_lot=True', lobby._members[1]["in_lot"] is True)
except Exception as e:
    check('process_message 分发 arrived', False, str(e))
lobby._travel_active = False

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
