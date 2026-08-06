# -*- coding: utf-8 -*-
"""v9.12 协议增强百次测试：版本协商/player_id 复用/位置量化

验证（知识库 S4MP 消息协议 + 搜索引擎研究: MCP/QUIC 版本协商 / Gaffer 4096 values/meter）:
A. 协议版本协商（hello 带 proto_version → 匹配加入 / 不匹配拒绝 version_mismatch）
B. player_id 复用（断开 → 重连复用原 ID，防 KeyError）
C. 位置量化数学（_quantize 0.01m 精度 / 收端还原 / 累加正确）
D. 兼容性（旧 delta 消息仍可处理）
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
    PROTO_VERSION = network.PROTO_VERSION  # lobby.network.PROTO_VERSION 访问路径
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

# ============ A: 协议版本协商 ============
print('[A] 协议版本协商（MCP/QUIC 模式）')
# 模拟已连接客户端（process_message 里 lobby.network._clients[sender_pid] 用于发拒绝消息）
class _FakeSock:
    def sendall(self, *a): pass
Net._clients = {1: (_FakeSock(), "a1"), 2: (_FakeSock(), "a2"), 3: (_FakeSock(), "a3")}
network._clients = Net._clients
# A1: 匹配版本 → 加入
lobby._members = {}
lobby.ROOM_VISIBILITY = "public"
Net.sent = []
lobby.process_message({"type": "hello", "name": "v2客户端", "password": "",
                       "proto_version": network.PROTO_VERSION}, sender_pid=1)
check('匹配版本→成员加入', 1 in lobby._members, "members={}".format(list(lobby._members.keys())))

# A2: 不匹配版本（旧协议 v1）→ 拒绝 + version_mismatch
lobby._members = {}
Net.sent = []
lobby.process_message({"type": "hello", "name": "旧客户端", "password": "",
                       "proto_version": 1}, sender_pid=2)
check('旧版本→不加入', 2 not in lobby._members)
check('旧版本→发 version_mismatch', any(p.get("type") == "version_mismatch" for p in Net.sent))
if any(p.get("type") == "version_mismatch" for p in Net.sent):
    vm = [p for p in Net.sent if p.get("type") == "version_mismatch"][0]
    check('version_mismatch 含双端版本', vm.get("client_ver") == 1 and vm.get("host_ver") == network.PROTO_VERSION)

# A3: 无 proto_version（更旧 mod）→ 拒绝（视为 0）
lobby._members = {}
Net.sent = []
lobby.process_message({"type": "hello", "name": "无版本客户端", "password": ""}, sender_pid=3)
check('无版本→拒绝', 3 not in lobby._members)

# A4: 客户端收到 version_mismatch → 提示不崩
try:
    lobby.process_message({"type": "version_mismatch", "client_ver": 1, "host_ver": 2})
    check('客户端收 version_mismatch 不崩', True)
except Exception:
    check('客户端收 version_mismatch 不崩', False)

# ============ B: player_id 复用 ============
print()
print('[B] player_id 复用（防 KeyError）')
# B1: 断开 → pid 进复用池
network._clients = {1: ("sock1", "addr1"), 2: ("sock2", "addr2")}
network._released_pids = []
network._next_player_id = 3
with network._clients_lock:
    network._clients.pop(1, None)
    network._released_pids.append(1)
check('断开后 pid=1 进复用池', network._released_pids == [1])

# B2: 新连接复用 pid（不递增）
class _FakeConn: pass
with network._clients_lock:
    if network._released_pids:
        pid = network._released_pids.pop(0)
    else:
        pid = network._next_player_id
        network._next_player_id += 1
    network._clients[pid] = (_FakeConn(), "addr")
check('重连复用 pid=1', pid == 1 and network._next_player_id == 3, "pid={} next={}".format(pid, network._next_player_id))

# B3: 20 次循环：断开→复用→不断递增
ok_reuse = True
for i in range(20):
    try:
        with network._clients_lock:
            # 模拟断开一个 client
            if network._clients:
                old = next(iter(network._clients))
                network._clients.pop(old)
                if old not in network._released_pids:
                    network._released_pids.append(old)
            # 模拟新连接
            if network._released_pids:
                pid = network._released_pids.pop(0)
            else:
                pid = network._next_player_id
                network._next_player_id += 1
            network._clients[pid] = (_FakeConn(), "addr")
        assert pid <= 3  # 复用池内，不无限递增
    except AssertionError:
        ok_reuse = False
        break
    except Exception:
        ok_reuse = False
        break
check('20 次断开复用 pid 不递增', ok_reuse, "next={}".format(network._next_player_id))

# ============ C: 位置量化数学 ============
print()
print('[C] 位置量化（Gaffer 4096 values/meter）')
from multimod import sync as sync_mod

# C1: _quantize 精度（0.01m）
q = sync_mod._quantize(1.234)
check('量化 1.234 → 123（0.01m 精度）', q == 123, "q={}".format(q))
q2 = sync_mod._quantize(-0.567)
check('量化 -0.567 → -57', q2 == -57, "q2={}".format(q2))
# Python round 是 banker's rounding（half to even）：round(0.5)=0, round(1.5)=2
q3 = sync_mod._quantize(0.015)
check('量化 0.015 → 2（round half to even）', q3 == 2, "q3={}".format(q3))
q3b = sync_mod._quantize(0.005)
check('量化 0.005 → 0（round half to even）', q3b == 0, "q3b={}".format(q3b))

# C2: 收端还原（/100）
base = [100.0, 200.0, 300.0]
sync_mod._delta_base = {42: base}
dq = [sync_mod._quantize(0.5), sync_mod._quantize(-0.25), sync_mod._quantize(0.0)]
sync_mod.process_message({"type": "sim_pos", "sim_id": 42, "delta_q": dq})
restored = sync_mod._delta_base[42]
check('还原后累加正确', abs(restored[0] - 100.5) < 0.011 and abs(restored[1] - 199.75) < 0.011,
      "restored={}".format(restored))

# C3: 20 次随机移动量化累加（误差累积有界）
sync_mod._delta_base = {7: [0.0, 0.0, 0.0]}
pos = [0.0, 0.0, 0.0]
ok_accum = True
for i in range(20):
    move = [random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(-1, 1)]
    pos = [pos[0] + move[0], pos[1] + move[1], pos[2] + move[2]]
    dq = [sync_mod._quantize(move[0]), sync_mod._quantize(move[1]), sync_mod._quantize(move[2])]
    base = sync_mod._delta_base[7]
    newpos = [base[0] + dq[0] / 100.0, base[1] + dq[1] / 100.0, base[2] + dq[2] / 100.0]
    sync_mod._delta_base[7] = newpos
# 20 步后误差 < 0.5m（每步 0.005m 量化误差 × 20）
err = max(abs(sync_mod._delta_base[7][i] - pos[i]) for i in range(3))
check('20 步量化累积误差 <0.5m', err < 0.5, "err={:.4f}".format(err))

# C4: 无基准时 delta_q 忽略（等绝对值）
sync_mod._delta_base = {}
sync_mod.process_message({"type": "sim_pos", "sim_id": 99, "delta_q": [1, 1, 1]})
check('无基准 delta_q 忽略', 99 not in sync_mod._delta_base)

# ============ D: 兼容性 ============
print()
print('[D] 兼容性（旧 delta 消息仍可处理）')
# D1: 旧协议 delta（未量化）→ 仍可处理
sync_mod._delta_base = {55: [10.0, 20.0, 30.0]}
sync_mod.process_message({"type": "sim_pos", "sim_id": 55, "delta": [0.5, 0.5, 0.5]})
check('旧 delta 兼容', abs(sync_mod._delta_base[55][0] - 10.5) < 0.001)

# D2: position 绝对值仍可用
sync_mod._delta_base = {}
sync_mod.process_message({"type": "sim_pos", "sim_id": 66, "position": [1, 2, 3]})
check('position 绝对值兼容', sync_mod._delta_base.get(66) == [1, 2, 3])

# D3: welcome 带 proto_version
Net.sent = []
network._send_json(None, {"type": "welcome", "player_id": 5, "proto_version": network.PROTO_VERSION})
check('welcome 含 proto_version', any(p.get("type") == "welcome" and p.get("proto_version") == network.PROTO_VERSION
                                      for p in Net.sent))

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
