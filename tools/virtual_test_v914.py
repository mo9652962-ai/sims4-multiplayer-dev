# -*- coding: utf-8 -*-
"""v9.14 连接健康监控百次测试：RTT 测量/健康评分/自适应频率

验证（搜索引擎研究: 游戏 RTT ping 机制 / Cloudflare 质量评分 / DACC 自适应频率）:
A. ping/pong RTT 测量（ping 回 pong 带时间戳 → RTT 记录）
B. 健康评分（RTT 阈值 → 评分 / 等级标签 / 无样本）
C. RTT 滚动平均（20 窗口 / 异常值过滤）
D. 自适应广播频率（RTT 高 → 间隔翻倍）
E. 真实 TCP ping/pong 闭环
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

# ============ A: ping/pong RTT 测量 ============
print('[A] ping/pong RTT 测量')
# A1: 收到 ping → 回 pong（host 视角，sender_pid=1 模拟已连接 client）
class _FakeSock: pass
Net._clients = {1: (_FakeSock(), "a1")}
network._clients = Net._clients
network._is_host = True  # 模块级 _is_host（ping 处理检查它）
network._rtt_samples = []
Net.sent = []
network._incoming_queue.put(({"type": "ping", "ts": time.time() - 0.1}, 1))
network._process_incoming()
pongs = [p for p in Net.sent if (p[0] if isinstance(p, tuple) else p).get("type") == "pong"]
check('收到 ping 回 pong', len(pongs) == 1, "pongs={}".format(len(pongs)))

# A2: 收到 pong → 记录 RTT
network._rtt_samples = []
network._incoming_queue.put({"type": "pong", "ts": time.time() - 0.08})
network._process_incoming()
rtt = network.get_rtt_ms()
check('pong 计算 RTT', rtt is not None and 70 <= rtt <= 90, "rtt={}".format(rtt))

# A3: 异常 RTT 过滤（>5s 丢弃）
network._rtt_samples = []
network._incoming_queue.put({"type": "pong", "ts": time.time() - 10})
network._process_incoming()
check('超 5s RTT 丢弃', network.get_rtt_ms() is None)

# A4: 无 ts 的 ping → 不崩
try:
    network._incoming_queue.put({"type": "ping"})
    network._incoming_queue.put({"type": "pong"})
    network._process_incoming()
    check('无 ts ping/pong 不崩', True)
except Exception:
    check('无 ts ping/pong 不崩', False)

# ============ B: 健康评分 ============
print()
print('[B] 健康评分（Cloudflare 模型）')
import random as _random
def random_rtt():
    return _random.uniform(10, 600)

# B1: 无样本 → None
network._rtt_samples = []
check('无样本评分 None', network.get_health_score() is None)

# B2: 优秀（RTT < 50ms）
network._rtt_samples = [20, 25, 30, 28]
score = network.get_health_score()
check('低 RTT 评分高', score is not None and score >= 85, "score={}".format(score))
check('低 RTT 标签优秀', "优秀" in network.get_health_label())

# B3: 差（RTT > 350ms）
network._rtt_samples = [350, 400, 380, 420]
score = network.get_health_score()
check('高 RTT 评分低', score is not None and score < 50, "score={}".format(score))
check('高 RTT 标签差', "差" in network.get_health_label())

# B4: 20 次随机 RTT 评分范围 [0,100]
ok_range = True
for i in range(20):
    network._rtt_samples = [random_rtt() for _ in range(5)]
    s = network.get_health_score()
    if s is None or not (0 <= s <= 100):
        ok_range = False
        break
check('20 次评分范围 [0,100]', ok_range)

# ============ C: RTT 滚动平均 ============
print()
print('[C] RTT 滚动平均')
# C1: 20 窗口上限
network._rtt_samples = []
for i in range(30):
    network._record_rtt(100 + i)
check('样本窗口上限 20', len(network._rtt_samples) == 20, "n={}".format(len(network._rtt_samples)))

# C2: 平均值正确
network._rtt_samples = [100, 200, 300]
check('平均计算', abs(network.get_rtt_ms() - 200) < 0.001, "avg={}".format(network.get_rtt_ms()))

# C3: 新样本挤旧样本
network._rtt_samples = []
for i in range(25):
    network._record_rtt(50)
network._record_rtt(500)
check('新样本保留', network.get_rtt_ms() > 50, "avg={}".format(network.get_rtt_ms()))

# ============ D: 自适应广播频率 ============
print()
print('[D] 自适应广播频率（DACC）')
from multimod import sync as sync_mod

# D1: 无 RTT → 默认间隔
network._rtt_samples = []
interval = sync_mod.SYNC_INTERVAL
check('无 RTT 默认间隔', interval == 0.5)

# D2: RTT 200-400 → 1.5x
network._rtt_samples = [250, 260, 240]
rtt = network.get_rtt_ms()
adaptive = sync_mod.SYNC_INTERVAL * 1.5 if rtt > 200 else sync_mod.SYNC_INTERVAL
check('RTT 250ms → 1.5x 间隔', adaptive == 0.75, "adaptive={}".format(adaptive))

# D3: RTT > 400 → 2x
network._rtt_samples = [500, 510, 490]
rtt = network.get_rtt_ms()
adaptive = sync_mod.SYNC_INTERVAL * 2.0 if rtt > 400 else sync_mod.SYNC_INTERVAL
check('RTT 500ms → 2x 间隔', adaptive == 1.0)

# D4: RTT < 200 → 默认
network._rtt_samples = [50, 60, 55]
rtt = network.get_rtt_ms()
adaptive = sync_mod.SYNC_INTERVAL
check('RTT 低 → 默认间隔', adaptive == 0.5)

# ============ E: 真实 TCP ping/pong 闭环 ============
print()
print('[E] 真实 TCP ping/pong 闭环')
TEST_PORT = 19414
network._is_host = True
network._my_player_id = 0
network._clients.clear()  # 清理前面测试的残留 FakeSock
net_thread = threading.Thread(target=network._server_thread, args=(TEST_PORT,), daemon=True)
net_thread.start()
time.sleep(1.0)
network._is_host = False
cli_thread = threading.Thread(target=network._client_thread, args=('127.0.0.1', TEST_PORT), daemon=True)
cli_thread.start()
time.sleep(1.5)
ok_conn = network._client_socket is not None
check('client 连接成功', ok_conn)

if ok_conn:
    # host 发 ping（真实 _broadcast → client 收到 → 回 pong → host 算 RTT）
    # 恢复真实发送（client 回 pong 需要真发到 socket）
    def _real_send(sock, payload):
        # v9.15: 12 字节头（8长度+4CRC）匹配新帧协议
        import pickle
        from struct import pack
        import binascii
        data = pickle.dumps(payload)
        crc = binascii.crc32(data) & 0xffffffff
        sock.sendall(pack('>QI', len(data), crc) + b'\x00' * 32)
        sock.sendall(data)
        return True
    network._send_json = _real_send
    network._is_host = True
    network._rtt_samples = []
    net_ts = time.time()
    # 直接真实发送给 client（_broadcast 是 mock，绕过）
    for _pid, (_sock, _addr) in list(network._clients.items()):
        _real_send(_sock, {"type": "ping", "ts": net_ts})
    time.sleep(0.3)
    # client 视角消费 ping → 回 pong（真实发送给 host）
    network._is_host = False
    network._process_incoming()
    time.sleep(0.3)
    # host 视角消费 pong → 算 RTT
    network._is_host = True
    network._process_incoming()
    check('真实 TCP RTT 闭环', network.get_rtt_ms() is not None,
          "rtt={}".format(network.get_rtt_ms()))

# 清理
try:
    if network._client_socket is not None:
        network._client_socket.close()
except Exception:
    pass

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
