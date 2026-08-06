# -*- coding: utf-8 -*-
"""v9.13 协议增强百次测试：消息批处理/协议目录完整性

验证（搜索引擎研究: Vanilla Java batching / Kafka Schema Registry）:
A. _send_batch 批量发送（单条→直发 / 多条→batch 帧 / 空→跳过）
B. batch 接收拆包（拆开逐个处理 / 嵌套消息 / 未知消息不崩）
C. 真实 TCP 批处理闭环（host 广播 batch → client 拆包处理）
D. 协议目录完整性（docs/PROTOCOL.md 覆盖全部消息类型）
"""
import sys, os, json, time, threading

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
    PROTO_VERSION = network.PROTO_VERSION
    @classmethod
    def _log(cls, msg): pass
    @classmethod
    def _broadcast(cls, p, exclude=None): cls.sent.append(p)
    @classmethod
    def _send_json(cls, s, p, prio=1): cls.sent.append((p, prio))
    @classmethod
    def _notify(cls, s): cls.notified.append(s)
lobby.network = Net
network._broadcast = Net._broadcast
network._send_json = Net._send_json
network._notify = Net._notify

# ============ A: _send_batch 逻辑 ============
print('[A] _send_batch 批量发送逻辑')
# A 段测试真实发送（恢复真实 _send_json——_send_batch 内部调用它）
_real_send_json = network._send_json
def _real_send_impl(sock, payload):
    # v9.15: 12 字节头（8长度+4CRC）匹配新帧协议
    import pickle
    from struct import pack
    import binascii
    data = pickle.dumps(payload)
    crc = binascii.crc32(data) & 0xffffffff
    sock.sendall(pack('>QI', len(data), crc) + b'\x00' * 32)
    sock.sendall(data)
    return True
network._send_json = _real_send_impl

class _FakeSock:
    def __init__(self): self.sent = []
    def sendall(self, data): self.sent.append(data)

# A1: 空列表 → 不发送（True）
s = _FakeSock()
r = network._send_batch(s, [])
check('空列表跳过', r is True and len(s.sent) == 0)

# A2: 单条 → 直发（非 batch 帧）
s = _FakeSock()
network._send_batch(s, [{"type": "chat", "text": "hi"}])
check('单条直发', len(s.sent) == 2)  # 8字节头 + pickle

# A3: 多条 → batch 帧
s = _FakeSock()
network._send_batch(s, [{"type": "chat", "text": "a"}, {"type": "mood", "mood": "happy"}])
import pickle as _p
from struct import unpack
raw = b"".join(s.sent)
(n, c) = unpack('>QI', raw[:12])
data = _p.loads(raw[44:44 + n])
check('多条合并 batch 帧', data.get("type") == "batch" and len(data.get("msgs", [])) == 2,
      "type={} n={}".format(data.get("type"), len(data.get("msgs", []))))
check('batch 帧单帧发送', len(s.sent) == 2, "sent={}".format(len(s.sent)))  # 一个头+一个数据

# A4: 20 次循环批量（合并正确性）
ok_batch = True
for i in range(20):
    try:
        s = _FakeSock()
        msgs = [{"type": "chat", "text": "m{}".format(j)} for j in range(i + 1)]
        network._send_batch(s, msgs)
        raw = b"".join(s.sent)
        (n, c) = unpack('>QI', raw[:12])
        data = _p.loads(raw[44:44 + n])
        if i == 0:
            assert data.get("type") == "chat"  # 单条直发（非 batch）
        else:
            assert data.get("type") == "batch"
            assert len(data.get("msgs", [])) == i + 1
    except Exception:
        ok_batch = False
        break
check('20 次批量合并正确', ok_batch)

# ============ B: batch 接收拆包 ============
print()
print('[B] batch 接收拆包')
from multimod import lobby as lb
lb._chat_history = []
lb.LOBBY_STATE_PATH = os.path.join(os.path.dirname(__file__), "mp_state_test.json")
# 恢复 mock（B 段用 Net._send_json 拦截广播）
network._send_json = Net._send_json
# B1: batch 帧拆开逐个处理（子消息被同循环消费——验证 chat 已处理）
network._incoming_queue.queue.clear()
lb._chat_history = []
network._incoming_queue.put({"type": "batch", "msgs": [
    {"type": "chat", "from": "b1", "text": "一"}, {"type": "chat", "from": "b1", "text": "二"}]})
network._process_incoming()
check('batch 拆开 2 条处理', len(lb._chat_history) >= 2,
      "chat={}".format([c.get("text") for c in lb._chat_history]))

# B2: 拆包的 chat 正确分发（批量消息进入历史）
lb._chat_history = []
network._incoming_queue.queue.clear()
network._incoming_queue.put({"type": "batch", "msgs": [{"type": "chat", "from": "批量", "text": "批量消息"}]})
network._process_incoming()
check('拆包后 chat 可处理', any(c.get("text") == "批量消息" for c in lb._chat_history),
      "chat={}".format(lb._chat_history))

# B3: 空 msgs → 不崩
try:
    network._incoming_queue.put({"type": "batch", "msgs": []})
    network._process_incoming()
    check('空 batch 不崩', True)
except Exception:
    check('空 batch 不崩', False)

# B4: 非 dict 子消息 → 跳过不崩
try:
    network._incoming_queue.put({"type": "batch", "msgs": ["garbage", {"type": "chat", "text": "ok"}]})
    network._process_incoming()
    check('非 dict 子消息跳过', True)
except Exception:
    check('非 dict 子消息跳过', False)

# ============ C: 真实 TCP 批处理闭环 ============
print()
print('[C] 真实 TCP 批处理闭环')
TEST_PORT = 19420
# host 起 server
network._is_host = True
network._my_player_id = 0
net_thread = threading.Thread(target=network._server_thread, args=(TEST_PORT,), daemon=True)
net_thread.start()
time.sleep(1.0)
# client 连接
network._is_host = False
cli_thread = threading.Thread(target=network._client_thread, args=('127.0.0.1', TEST_PORT), daemon=True)
cli_thread.start()
time.sleep(1.5)
ok_conn = network._client_socket is not None
check('client 连接成功', ok_conn)

if ok_conn:
    # client 批量发送 3 条（hello + 2 chat）→ host 收到 batch 拆包
    # 先消费 welcome
    network._is_host = True
    network._process_incoming()
    network._is_host = False
    # client 用真实发送（_send_batch → 真实 _send_json）
    network._send_json = _real_send_impl
    network._send_batch(network._client_socket, [
        {"type": "chat", "from": "批量客户端", "text": "b1"},
        {"type": "chat", "from": "批量客户端", "text": "b2"}])
    network._send_json = Net._send_json
    # host 侧轮询收 batch
    deadline = time.time() + 3.0
    got_batch = False
    while time.time() < deadline:
        network._is_host = True
        network._process_incoming()
        if any(c.get("text") in ("b1", "b2") for c in lb._chat_history):
            got_batch = True
            break
        time.sleep(0.2)
    check('真实 TCP batch 拆包处理', got_batch, "chat={}".format(lb._chat_history[-3:]))

# 清理
try:
    if network._client_socket is not None:
        network._client_socket.close()
except Exception:
    pass
try:
    os.remove(os.path.join(os.path.dirname(__file__), "mp_state_test.json"))
except Exception:
    pass

# ============ D: 协议目录完整性 ============
print()
print('[D] 协议目录完整性（docs/PROTOCOL.md）')
PROTOCOL_DOC = os.path.join(os.path.dirname(__file__), "..", "docs", "PROTOCOL.md")
if os.path.exists(PROTOCOL_DOC):
    doc = open(PROTOCOL_DOC, encoding="utf-8").read()
    # 从代码收集所有消息类型
    import re
    msg_types = set()
    for f in ["network.py", "lobby.py", "sync.py", "money_sync.py",
              "stats_sync.py", "mood_sync.py", "clock_sync.py"]:
        p = os.path.join(os.path.dirname(__file__), "..", "src", "multimod", f)
        if os.path.exists(p):
            src = open(p, encoding="utf-8").read()
            for m in re.findall(r'"type":\s*"([a-z_]+)"', src):
                msg_types.add(m)
    # 目录覆盖检查（PROTOCOL.md 应有 `类型` 行的反引号引用）
    missing = [t for t in sorted(msg_types) if ("`" + t + "`") not in doc]
    check('目录覆盖全部 {} 种消息'.format(len(msg_types)), not missing, "missing={}".format(missing))
    check('目录含帧格式说明', "8 字节" in doc or "长度前缀" in doc)
    check('目录含版本兼容规则', "PROTO_VERSION" in doc and "version_mismatch" in doc)
else:
    check('协议目录存在', False, "PROTOCOL.md not found")

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
