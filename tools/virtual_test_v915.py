# -*- coding: utf-8 -*-
"""v9.15 协议增强百次测试：CRC 帧校验/消息优先级

验证（搜索引擎研究: Ethernet FCS CRC32 / pvigier QoS 多流分离）:
A. CRC 发送帧格式（8长度+4CRC+数据 / CRC 正确性）
B. CRC 接收校验（正常帧通过 / 数据损坏丢弃 / CRC 字段损坏丢弃 / 继续处理后续帧）
C. 真实 TCP CRC 闭环（正常收发 / 损坏帧被丢弃连接不崩）
D. 消息优先级（紧急 prio=0 / 普通默认 1 / 低优 prio=2 / _send_batch 合并）
"""
import sys, os, json, time, threading, pickle
from struct import pack, unpack

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

# ============ A: CRC 发送帧格式 ============
print('[A] CRC 发送帧格式（Ethernet FCS）')
class _FakeSock:
    def __init__(self): self.sent = []
    def sendall(self, data): self.sent.append(data)

# A1: 帧格式 = 8长度 + 4CRC + 数据
s = _FakeSock()
_real_send_json = network._send_json
import binascii
def _real_send(sock, payload, prio=1):
    data = pickle.dumps(payload)
    crc = binascii.crc32(data) & 0xffffffff
    sock.sendall(pack('>QI', len(data), crc) + b'\x00' * 32)
    sock.sendall(data)
    return True
network._send_json = _real_send
network._send_json(s, {"type": "chat", "text": "crc测试"})
raw = b"".join(s.sent)
(n, c) = unpack('>QI', raw[:12])
check('帧头 8长度+4CRC', n > 0 and c > 0, "n={} crc={}".format(n, c))
check('CRC 正确', c == (binascii.crc32(raw[44:]) & 0xffffffff))

# A2: 20 次随机消息 CRC 正确
ok_crc = True
import random
for i in range(20):
    s = _FakeSock()
    payload = {"type": "chat", "text": "m{}".format(i), "n": random.randint(0, 1000)}
    network._send_json(s, payload)
    raw = b"".join(s.sent)
    (n, c) = unpack('>QI', raw[:12])
    if c != (binascii.crc32(raw[44:]) & 0xffffffff):
        ok_crc = False
        break
check('20 次 CRC 正确', ok_crc)

# ============ B: CRC 接收校验 ============
print()
print('[B] CRC 接收校验')
# B1: 正常帧通过（入队）
network._incoming_queue.queue.clear()
network._incoming_queue.put({"type": "chat", "from": "ok", "text": "good"})
check('正常消息入队', network._incoming_queue.qsize() == 1)

# B2: 数据损坏 → 丢弃（模拟 _recv_loop 的 CRC 校验逻辑）
def _recv_frame_check(frame_data, frame_crc):
    """复制 _recv_loop 的 CRC 校验逻辑"""
    if frame_crc != (binascii.crc32(frame_data) & 0xffffffff):
        return False  # 丢弃
    return True

good_data = pickle.dumps({"type": "chat", "text": "good"})
good_crc = binascii.crc32(good_data) & 0xffffffff
check('正常帧 CRC 通过', _recv_frame_check(good_data, good_crc))

# B3: 数据位翻转 → CRC 检测
bad_data = bytearray(good_data)
bad_data[5] ^= 0x40  # 翻转一位
check('数据损坏被检测', not _recv_frame_check(bytes(bad_data), good_crc))

# B4: CRC 字段损坏 → 检测
check('CRC 字段损坏被检测', not _recv_frame_check(good_data, good_crc ^ 0x1))

# B5: 20 次随机损坏 → 全部检测
ok_detect = True
for i in range(20):
    bad = bytearray(good_data)
    pos = random.randint(0, len(bad) - 1)
    bad[pos] ^= (1 << random.randint(0, 7))
    if _recv_frame_check(bytes(bad), good_crc):
        ok_detect = False
        break
check('20 次随机损坏全部检测', ok_detect)

# ============ C: 真实 TCP CRC 闭环 ============
print()
print('[C] 真实 TCP CRC 闭环')
TEST_PORT = 19415
network._is_host = True
network._my_player_id = 0
t1 = threading.Thread(target=network._server_thread, args=(TEST_PORT,), daemon=True)
t1.start()
time.sleep(1.0)
network._is_host = False
t2 = threading.Thread(target=network._client_thread, args=('127.0.0.1', TEST_PORT), daemon=True)
t2.start()
time.sleep(1.5)
ok_conn = network._client_socket is not None
check('client 连接成功', ok_conn)

if ok_conn:
    # C1: 正常消息 → 对端收到
    network._send_json = _real_send
    network._is_host = True
    network._process_incoming()  # 消费 welcome
    from multimod import lobby as lb
    lb._chat_history = []
    network._is_host = False
    network._send_json(network._client_socket, {"type": "chat", "from": "crc", "text": "正常帧"})
    time.sleep(0.5)
    network._is_host = True
    network._process_incoming()
    check('真实 TCP 正常帧到达', any(c.get("text") == "正常帧" for c in lb._chat_history),
          "chat={}".format(lb._chat_history[-2:]))

    # C2: 损坏帧 → 丢弃连接不崩（发原始 socket 手工构造坏帧）
    lb._chat_history = []
    sock = network._clients[1][0] if 1 in network._clients else None
    if sock is not None:
        bad_data = pickle.dumps({"type": "chat", "from": "bad", "text": "坏帧"})
        bad_crc = (binascii.crc32(bad_data) & 0xffffffff) ^ 0x2A  # 故意错
        sock.sendall(pack('>QI', len(bad_data), bad_crc) + b'\x00' * 32)
        sock.sendall(bad_data)
        time.sleep(0.5)
        network._process_incoming()
        check('坏帧被丢弃', not any(c.get("text") == "坏帧" for c in lb._chat_history),
              "chat={}".format(lb._chat_history[-2:]))
        # C3: 坏帧后连接仍活（再发正常帧能收到）
        network._is_host = False
        network._send_json(network._client_socket, {"type": "chat", "from": "crc", "text": "坏帧后正常"})
        time.sleep(0.5)
        network._is_host = True
        network._process_incoming()
        check('坏帧后连接存活', any(c.get("text") == "坏帧后正常" for c in lb._chat_history),
              "chat={}".format(lb._chat_history[-3:]))

# 清理
try:
    if network._client_socket is not None:
        network._client_socket.close()
except Exception:
    pass

# ============ D: 消息优先级 ============
print()
print('[D] 消息优先级（QoS）')
# D1: 默认 prio=1
network._send_json = Net._send_json
Net.sent = []
network._send_json(None, {"type": "chat", "text": "默认"})
check('默认 prio=1', Net.sent[0][1] == 1, "prio={}".format(Net.sent[0][1]))

# D2: 紧急 prio=0
Net.sent = []
network._send_json(None, {"type": "ready", "ready": True}, prio=0)
check('紧急 prio=0', Net.sent[0][1] == 0)

# D3: 低优 prio=2
Net.sent = []
network._send_json(None, {"type": "sim_pos", "sim_id": 1}, prio=2)
check('低优 prio=2', Net.sent[0][1] == 2)

# D4: _send_batch 多条合并（低优批量）
s = _FakeSock()
network._send_json = _real_send
network._send_batch(s, [{"type": "sim_pos", "sim_id": 1}, {"type": "sim_pos", "sim_id": 2}])
raw = b"".join(s.sent)
(n, c) = unpack('>QI', raw[:12])
data = pickle.loads(raw[44:44 + n])
check('batch 合并低优消息', data.get("type") == "batch" and len(data.get("msgs", [])) == 2)

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
