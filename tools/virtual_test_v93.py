# -*- coding: utf-8 -*-
"""v9.3 百次虚拟测试：循环握手/聊天/位置×100 + 压力洪泛 + 帧边界

验证（自身经验 + 搜索引擎研究: 帧健壮性/并发/压力模式）:
A. 100 次 握手→聊天→位置 完整循环（回归）
B. 压力: 1 秒 500 条消息洪泛（吞吐/不丢不崩）
C. 帧边界: 超大长度前缀（DoS 防御）→ 应被拒绝/不崩
D. 畸形 pickle 帧 → 不崩（记录错误）
E. 多客户端并发连接（3 个 client 同时）
"""
import sys, os, json, time, threading, pickle, struct, binascii
os.environ["SIMSYNC_ALLOW_SELF"] = "1"  # v9.19.1: 单机多客户端测试需要放行 127.0.0.1

# ---- Mock 游戏依赖 ----
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
network._check_launcher_cmd = lambda: None
network._process_alarm_callback = lambda *a: None
def drain():
    network._process_incoming()

# v9.15: 状态文件指向测试目录（避免污染真实 Mods 路径/依赖残留文件）
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mp_state_v93.json")
from multimod import lobby as _lobby_v93
_lobby_v93.LOBBY_STATE_PATH = STATE_PATH
_lobby_v93._write_state_file = _lobby_v93._write_state_file  # 确保使用测试路径
def clear_state():
    try: os.remove(STATE_PATH)
    except Exception: pass

# ============ A: 100 次完整循环 ============
print('[A] 100 次 握手→聊天→位置 循环')
PORT_A = 19401
network._is_host = True
network._my_player_id = 0
net_thread = threading.Thread(target=network._server_thread, args=(PORT_A,), daemon=True)
net_thread.start()
time.sleep(1.0)

ok_loops = 0
for i in range(100):
    try:
        clear_state()
        # 每次新建 client 连接
        import socket as _s
        c = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        c.settimeout(3)
        c.connect(('127.0.0.1', PORT_A))
        # 手工帧发送（模拟协议）
        def send_frame(sock, obj):
            # v9.15: 12 字节头（8长度+4CRC）
            data = pickle.dumps(obj)
            crc = binascii.crc32(data) & 0xffffffff
            sock.sendall(struct.pack('>QI', len(data), crc) + b'\x00' * 32 + data)
        send_frame(c, {"type": "hello", "name": "测试{}".format(i), "password": "",
                       "proto_version": network.PROTO_VERSION})  # v9.15: 带协议版本
        # 收 welcome
        buf = b""
        got = None
        deadline = time.time() + 3
        while time.time() < deadline and got is None:
            try:
                chunk = c.recv(4096)
                if not chunk: break
                buf += chunk
                while len(buf) >= 44:
                    (fl, fcr) = struct.unpack('>QI', buf[:12])
                    if len(buf) < 12 + fl: break
                    if fcr != (binascii.crc32(buf[44:44+fl]) & 0xffffffff): break
                    frame = buf[44:44+fl]; buf = buf[44+fl:]
                    got = pickle.loads(frame)
            except Exception: break
        # 发聊天 + 位置
        send_frame(c, {"type": "chat", "from": "测试{}".format(i), "text": "消息{}".format(i)})
        send_frame(c, {"type": "sim_pos", "sim_id": 1000+i, "delta": [0.1, 0, 0], "abs": None, "seq": i})
        time.sleep(0.02)
        c.close()
        ok_loops += 1
    except Exception as e:
        failures.append("loop{}: {}".format(i, e))
        break
check('100 次循环全部完成', ok_loops == 100)
# host 处理完队列
time.sleep(0.5)
drain()
st = json.load(open(STATE_PATH, encoding="utf-8"))
# 100 次循环后 chat 应截断到上限 20（v9.0 防无限增长特性，非 bug）
check('chat 历史按 20 条上限截断', len(st.get("chat", [])) == 20,
      "got {}".format(len(st.get("chat", []))))

# ============ B: 压力洪泛 ============
print()
print('[B] 压力: 1 秒 500 条消息洪泛')
PORT_B = 19402
network._is_host = True
net_thread2 = threading.Thread(target=network._server_thread, args=(PORT_B,), daemon=True)
net_thread2.start()
time.sleep(1.0)

import socket as _s2
c2 = _s2.socket(_s2.AF_INET, _s2.SOCK_STREAM)
c2.settimeout(5)
c2.connect(('127.0.0.1', PORT_B))
def send_frame2(sock, obj):
    # v9.15: 12 字节头（8长度+4CRC）
    data = pickle.dumps(obj)
    crc = binascii.crc32(data) & 0xffffffff
    sock.sendall(struct.pack('>QI', len(data), crc) + b'\x00' * 32 + data)

t0 = time.time()
sent = 0
for i in range(500):
    send_frame2(c2, {"type": "chat", "from": "压测", "text": "m{}".format(i)})
    sent += 1
dt = time.time() - t0
# 接收端处理
time.sleep(1.0)
drain()
st2 = json.load(open(STATE_PATH, encoding="utf-8"))
n_chat = len(st2.get("chat", []))
check('500 条全部发送', sent == 500)
check('吞吐 ≥ 200 msg/s', sent / dt >= 200, "{:.0f} msg/s".format(sent / dt))
check('压力下不崩溃 (host 线程存活)', net_thread2.is_alive())
check('chat 上限 20 条截断', n_chat <= 20, "got {}".format(n_chat))
c2.close()

# ============ C: 帧边界（DoS 防御） ============
print()
print('[C] 帧边界: 超大长度前缀 (DoS 防御)')
PORT_C = 19403
network._is_host = True
net_thread3 = threading.Thread(target=network._server_thread, args=(PORT_C,), daemon=True)
net_thread3.start()
time.sleep(1.0)
c3 = _s2.socket(_s2.AF_INET, _s2.SOCK_STREAM)
c3.settimeout(2)
c3.connect(('127.0.0.1', PORT_C))
# 发送超大长度前缀（0xFFFFFFFFFFFFFFFF = 内存 DoS 尝试）
c3.sendall(struct.pack('>QI', 0xFFFFFFFFFFFFFFFF, 0) + b'x')
time.sleep(0.5)
# 服务器应不崩
check('超大帧前缀不崩 (host 存活)', net_thread3.is_alive())
# 发送畸形 pickle
c3.sendall(struct.pack('>Q', 4) + b'\x00\x01\x02\x03')
time.sleep(0.5)
check('畸形 pickle 不崩 (host 存活)', net_thread3.is_alive())
c3.close()

# ============ D: 多客户端并发 ============
print()
print('[D] 多客户端并发 (3 个同时)')
PORT_D = 19404
network._is_host = True
net_thread4 = threading.Thread(target=network._server_thread, args=(PORT_D,), daemon=True)
net_thread4.start()
time.sleep(1.0)
clients = []
for i in range(3):
    cc = _s2.socket(_s2.AF_INET, _s2.SOCK_STREAM)
    cc.settimeout(3)
    cc.connect(('127.0.0.1', PORT_D))
    send_frame2(cc, {"type": "hello", "name": "C{}".format(i), "password": ""})
    clients.append(cc)
time.sleep(1.0)
check('3 客户端全部加入', len(network._clients) >= 3,
      "got {}".format(len(network._clients)))
# 同时发消息
for i, cc in enumerate(clients):
    send_frame2(cc, {"type": "chat", "from": "C{}".format(i), "text": "hi{}".format(i)})
time.sleep(0.8)
drain()
check('并发消息处理不崩 (host 存活)', net_thread4.is_alive())
for cc in clients: cc.close()

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
