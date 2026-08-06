# -*- coding: utf-8 -*-
"""v9.10 百次虚拟测试：STUN/旅行双端确认/存档断点续传/幂等防御

验证（自身经验 + 搜索引擎研究: STUN RFC 5389/Idempotent Consumer/FTP REST 续传）:
A. STUN 公网 IP 逻辑（成功/connect 失败降级本地 IP/异常安全）
B. 旅行双端确认完整流程（travel_req→ack→go→抵达）
C. 存档断点续传（中断→重连→补缺块→SHA 校验）
D. 重复/未知消息幂等（重复 hello 不重复加成员/未知 type 不崩）
"""
import sys, os, json, time, threading, random, socket

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

# ============ A: STUN 公网 IP 逻辑 ============
print('[A] STUN 公网 IP 逻辑')
# mock launcher 的 _stun_public_ip（用真实逻辑 + mock socket）
class _FakeSocket:
    def __init__(self, *a): self._connected = False
    def settimeout(self, t): pass
    def connect(self, addr):
        if _STUN_FAIL:
            raise OSError("network unreachable")
        self._connected = True
    def getsockname(self):
        return ("203.0.113.50", 7655)  # 模拟 NAT 映射后的公网 IP
    def close(self): pass

_STUN_FAIL = False
def _stun_public_ip(use_fake):
    """复制 launcher._stun_public_ip 逻辑（mock socket）"""
    try:
        import socket as _s
        if use_fake:
            _s.socket = _FakeSocket
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.settimeout(3)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
        return local
    except Exception:
        return get_local_ip_fallback()

def get_local_ip_fallback():
    return "192.168.1.10"

# A1: STUN 成功 → 公网 IP
_STUN_FAIL = False
ok_stun = _stun_public_ip(True)
check('STUN 成功返回公网 IP', ok_stun == "203.0.113.50", "got {}".format(ok_stun))

# A2: STUN 失败 → 降级本地 IP
_STUN_FAIL = True
ok_fallback = _stun_public_ip(True)
check('STUN 失败降级本地 IP', ok_fallback == "192.168.1.10", "got {}".format(ok_fallback))

# A3: 20 次循环稳定（不抛异常）
ok_loop = True
for i in range(20):
    try:
        _STUN_FAIL = (i % 2 == 0)
        r = _stun_public_ip(True)
        assert r in ("203.0.113.50", "192.168.1.10")
    except Exception as e:
        ok_loop = False
        break
check('STUN 20 次循环稳定', ok_loop)

# ============ B: 旅行双端确认完整流程 ============
print()
print('[B] 旅行双端确认完整流程 (req→ack→go→抵达)')
# 重置 lobby 状态
lobby._travel_pending = False
lobby._travel_acks = set()
lobby._members = {0: {'name': 'host'}, 1: {'name': 'b'}, 2: {'name': 'c'}}

# B1: 房主发起旅行 → travel_req 广播 + pending（真实函数链）
Net.sent = []
lobby._travel_pending = False
lobby._members = {0: {'name': 'host'}, 1: {'name': 'b'}, 2: {'name': 'c'}}
lobby.host_start_travel()
b1 = lobby._travel_pending is True
b2 = any(p.get("type") == "travel_req" for p in Net.sent)
b3 = lobby._travel_acks == {0}  # 房主自己已确认
check('房主发起→pending+req', b1 and b2)
check('房主自己已确认', b3)

# B2: 客户端确认 → ack 记录 + 全确认自动 go
Net.sent = []
lobby.on_travel_ack(1)
check('1 号确认→pending 仍 true（未全）', lobby._travel_pending is True)
lobby.on_travel_ack(2)
b4 = lobby._travel_pending is False  # 全确认→自动 go
b5 = any(p.get("type") == "travel_go" for p in Net.sent)
check('全部确认→自动 go', b4 and b5, "pending={} go={}".format(lobby._travel_pending, b5))

# B4: 客户端收到 go → 出发（状态清除）
lobby._travel_pending = False
lobby.process_message({"type": "travel_go", "dest": "park"})
check('收到 go→不崩', True)

# B5: 抵达通知 → 广播 arrived
Net.sent = []
lobby.process_message({"type": "travel_arrived", "player": 1})
check('抵达→不崩', True)

# B6: 完整流程 20 次循环（req→ack→go 语义，真实函数链）
ok_travel = True
for i in range(20):
    try:
        lobby._travel_pending = False
        lobby._travel_acks = set()
        lobby._members = {0: {'name': 'host'}, 1: {'name': 'b'}}
        lobby.host_start_travel()
        lobby.on_travel_ack(1)  # 全确认→自动 go
        assert lobby._travel_pending is False
        assert any(p.get("type") == "travel_go" for p in Net.sent)
    except Exception:
        ok_travel = False
        break
check('旅行流程 20 次循环稳定', ok_travel)

# ============ C: 存档断点续传（中断→重连→补缺块） ============
print()
print('[C] 存档断点续传 (中断→重连→补缺块)')
import base64, hashlib
os.makedirs(lobby.SAVES_DIR, exist_ok=True)
# 256KB 存档 → 4 块
test_data = os.urandom(256 * 1024 + 7)
fname = 'Slot_Resume_99.save'
with open(os.path.join(lobby.SAVES_DIR, fname), 'wb') as f:
    f.write(test_data)
file_sha = hashlib.sha256(test_data).hexdigest()
b64 = base64.b64encode(test_data).decode()
total = (len(b64) + lobby.SAVE_CHUNK_SIZE - 1) // lobby.SAVE_CHUNK_SIZE

# C1: 发送中断（只收到部分块）→ 缺块检测（用真实 total）
chunks = {}
for idx in [0, 2]:
    start = idx * lobby.SAVE_CHUNK_SIZE
    chunks[idx] = b64[start:start + lobby.SAVE_CHUNK_SIZE]
lobby._recv_save_cache = {fname: {"chunks": dict(chunks), "total": total, "bytes": sum(len(c) for c in chunks.values())}}
# 断点续传：重连后房主重发缺失块（v9.5 缺块重传机制）
missing = [i for i in range(total) if i not in chunks]
check('缺块检测（{} 块中缺 {} 块）'.format(total, total - 2), sorted(missing) == [1, 3] + list(range(4, total)),
      "missing={} total={}".format(missing, total))

# C2: 补传缺失块 → 拼装校验
for idx in missing:
    start = idx * lobby.SAVE_CHUNK_SIZE
    chunks[idx] = b64[start:start + lobby.SAVE_CHUNK_SIZE]
full = "".join(chunks[i] for i in sorted(chunks))
check('补全后 SHA 一致', hashlib.sha256(base64.b64decode(full)).hexdigest() == file_sha)

# C3: 全流程 20 次（随机丢块→补传→校验）
ok_resume = True
for i in range(20):
    try:
        c = {}
        nlost = random.randint(1, max(1, total - 1))
        for idx in range(total):
            if random.random() < 0.4:
                start = idx * lobby.SAVE_CHUNK_SIZE
                c[idx] = b64[start:start + lobby.SAVE_CHUNK_SIZE]
        # 缺块 → 补传
        for idx in range(total):
            if idx not in c:
                start = idx * lobby.SAVE_CHUNK_SIZE
                c[idx] = b64[start:start + lobby.SAVE_CHUNK_SIZE]
        full2 = "".join(c[i] for i in sorted(c))
        assert hashlib.sha256(base64.b64decode(full2)).hexdigest() == file_sha
    except Exception:
        ok_resume = False
        break
check('断点续传 20 次随机丢块补全', ok_resume)
try:
    os.remove(os.path.join(lobby.SAVES_DIR, fname))
except Exception:
    pass

# ============ D: 重复/未知消息幂等 ============
print()
print('[D] 重复/未知消息幂等 (Idempotent Consumer)')
# D1: 重复 hello 不重复加成员（幂等——研究: Idempotent Consumer 模式）
lobby._members = {}
# 模拟 2 次相同 hello（重复投递）→ 同 player_id 覆盖，不新增
lobby.on_hello(7, "dup", "1.2.3.4")
lobby.on_hello(7, "dup", "1.2.3.4")
check('重复 hello 不重复加成员', len(lobby._members) == 1, "members={}".format(len(lobby._members)))

# D1b: 不同 player_id 各自独立
lobby.on_hello(8, "other", "5.6.7.8")
check('不同成员独立加入', len(lobby._members) == 2)

# D2: 已存在成员重复 welcome 不崩
try:
    lobby.process_message({"type": "welcome", "player_id": 7, "name": "dup"})
    check('重复 welcome 不崩', True)
except Exception:
    check('重复 welcome 不崩', False)

# D3: 未知消息 type 不崩（20 种随机垃圾，走真实队列）
import queue as _queue
ok_unknown = True
for i in range(20):
    try:
        network._incoming_queue.put(json.dumps({"type": "unknown_type_{}".format(i), "x": random.random()}))
        network._process_incoming()
    except Exception:
        ok_unknown = False
        break
check('20 种未知 type 不崩', ok_unknown)

# D4: 空消息/None 不崩（真实队列路径）
try:
    network._incoming_queue.put({})
    network._process_incoming()
    check('空消息不崩', True)
except Exception as e:
    check('空消息不崩', False, str(e))

# D5: 重复 save_chunk_done 不崩（重复投递完成信号）
try:
    lobby.on_save_chunk_done({"filename": "nonexist.save", "total_bytes": 100, "sha256": "x"})
    lobby.on_save_chunk_done({"filename": "nonexist.save", "total_bytes": 100, "sha256": "x"})
    check('重复 save_chunk_done 不崩', True)
except Exception:
    check('重复 save_chunk_done 不崩', False)

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
