# -*- coding: utf-8 -*-
"""v9.22 回归：时钟白名单修正 / per-connection nonce / 握手前帧策略 / 旅行增强

跑真实代码路径（同 v921 风格）:
A. HOST_ONLY: "clock" 生效（客机冒充改主机时间被丢弃），"clock_sync" 移除
B. per-connection nonce: 两客机并发握手派生各自独立 key（不再互踩）
C. 握手前帧策略: 未认证连接的非白名单类型/超大帧被丢弃，hello 正常入队
D. 旅行增强: 自动确认 / 自动到达上报 / 分级超时 / 到达状态板 / 快照刷新
E. sock→pid 反向索引 O(1) 查询
F. 启动器命令桥: mp_travel / mp_travel_auto 分发
"""
import os
import sys
import threading
import time

os.environ["SIMSYNC_ALLOW_SELF"] = "1"

# ---- Mock 游戏依赖（import 前必须做）----
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

_LOGS = []
network._log = lambda msg: _LOGS.append(msg)
network._notify = lambda msg: None
network._ensure_alarm = lambda: None
lobby._notify = lambda msg: None

_SENT = []
def _fake_send(sock, payload, prio=1):
    _SENT.append(payload)
    return True
network._send_json = _fake_send
network._send_world_snapshot = lambda sock: _SENT.append({"type": "world_snapshot"})

# 状态文件写到临时目录（不污染真实 Mods）
import tempfile  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="v922_")
lobby.LOBBY_STATE_PATH = os.path.join(_tmp, "state.json")
network.CMD_PATH = os.path.join(_tmp, "mp_cmd.json")

# ============ A: HOST_ONLY clock 修正 ============
print('[A] HOST_ONLY 白名单（clock 生效 / clock_sync 移除）')
check('白名单含 clock', "clock" in network.HOST_ONLY_TYPES)
check('白名单已移除 clock_sync', "clock_sync" not in network.HOST_ONLY_TYPES)

_applied = []
clock_sync.apply_remote_clock = lambda speed: _applied.append(speed)
network._is_host = True
network._incoming_queue.put(({"type": "clock", "speed": 3}, 2))   # 客机冒充
network._process_incoming()
check('客机冒充 clock 被丢弃', _applied == [], "applied={}".format(_applied))
network._incoming_queue.put(({"type": "clock", "speed": 2}, None))  # 主机自身路径
network._process_incoming()
check('合法 clock 放行', _applied == [2], "applied={}".format(_applied))

# ============ B: per-connection nonce ============
print('[B] per-connection nonce（并发握手不互踩）')
class _FakeSock:
    pass

s1, s2 = _FakeSock(), _FakeSock()
network._clients = {1: (s1, ("10.0.0.1", 1)), 2: (s2, ("10.0.0.2", 2))}
network._conn_nonces = {1: b"hostnonce-1", 2: b"hostnonce-2"}
network._hmac_keys = {}
network._client_socket = None
network._is_host = True
lobby.ROOM_PASSWORD = "pw"
lobby._members = {}
lobby._save_sync_phase = "idle"
lobby._start_granted = False
import multimod.sync as sync_mod  # noqa: E402
sync_mod.reset_pos_baseline = lambda reason="": None

for pid, cnonce in ((1, b"client-A"), (2, b"client-B")):
    lobby.process_message({"type": "hello", "name": "p{}".format(pid), "password": "pw",
                           "proto_version": network.PROTO_VERSION,
                           "client_nonce": cnonce.decode()}, sender_pid=pid)

k1 = network._hmac_keys.get(1)
k2 = network._hmac_keys.get(2)
check('两客机都派生了 key', k1 is not None and k2 is not None)
check('两客机 key 互不相同', k1 != k2)
check('key1 = derive(pw, clientA, hostnonce1)',
      k1 == network._derive_hmac_key("pw", b"client-A", b"hostnonce-1"))
check('key2 = derive(pw, clientB, hostnonce2)',
      k2 == network._derive_hmac_key("pw", b"client-B", b"hostnonce-2"))

# ============ C: 握手前帧策略 ============
print('[C] 握手前帧策略（未认证连接收帧白名单）')
import pickle      # noqa: E402  仅测试自产帧
import socket as _socket  # noqa: E402
from struct import unpack  # noqa: E402

PORT_C = 19831
_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
_srv.bind(('127.0.0.1', PORT_C))
_srv.listen(1)

_sender = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_sender.connect(('127.0.0.1', PORT_C))
_conn, _ = _srv.accept()
# 真实 _send_json 产帧（含 CRC）——先保存 fake 再还原
network._send_json = _orig_send = None
def _real_send(sock, payload, prio=1):
    import pickle as _p
    from struct import pack
    data = _p.dumps(payload)
    crc = __import__('binascii').crc32(data) & 0xffffffff
    sock.sendall(pack('>QI', len(data), crc) + b"\x00" * 32)
    sock.sendall(data)
    return True
network._send_json = _real_send

network._is_host = True
network._hmac_keys.pop(7, None)   # pid 7 = 未认证连接
_recv_thread = threading.Thread(
    target=network._recv_loop, args=(_conn, False, 7), daemon=True)
_recv_thread.start()

# 排空队列
while True:
    try:
        network._incoming_queue.get_nowait()
    except Exception:
        break

_real_send(_sender, {"type": "hello", "name": "x", "client_nonce": "abc"})
_real_send(_sender, {"type": "heartbeat", "pid": 7})                  # 小帧白名单内
_real_send(_sender, {"type": "chat", "from": "big", "text": "x" * 9000})  # 超大帧
time.sleep(0.6)
_sender.close()
_conn.close()
_srv.close()
network._send_json = _fake_send

_types = []
while True:
    try:
        msg, spid = network._incoming_queue.get_nowait()
        _types.append(msg.get("type"))
    except Exception:
        break
check('hello 入队', "hello" in _types, "types={}".format(_types))
check('heartbeat 入队', "heartbeat" in _types)
check('超大帧(>4KB)被丢弃', "chat" not in _types)
check('丢弃有日志', any("pre-auth oversized frame dropped" in l for l in _LOGS))

# ============ D: 旅行增强 ============
print('[D] 旅行增强（自动确认/自动上报/分级超时/状态板/快照刷新）')
network._is_host = False
network._my_player_id = 3
network._client_socket = _FakeSock()
_SENT.clear()
lobby._travel_auto_ack = True
lobby.on_travel_req({})
ack_sent = any(m.get("type") == "travel_ack" for m in _SENT)
check('travel_req 自动确认', ack_sent)

lobby._travel_auto_ack = False
_SENT.clear()
lobby.on_travel_req({})
check('手动模式下不发 ack', not any(m.get("type") == "travel_ack" for m in _SENT))

# 自动到达上报：非旅行态不报
_SENT.clear()
lobby._travel_active = False
lobby._travel_arrived = set()
check('非旅行态 auto_report 返回 False', lobby.auto_report_arrival() is False)
check('非旅行态不发 travel_arrived',
      not any(m.get("type") == "travel_arrived" for m in _SENT))
# 旅行态自动报
lobby._travel_active = True
_SENT.clear()
check('旅行态 auto_report 返回 True', lobby.auto_report_arrival() is True)
check('旅行态发 travel_arrived',
      any(m.get("type") == "travel_arrived" for m in _SENT))

# 分级超时：非 final 只提示不解锁；final 解锁并广播
lobby._members = {0: {"player_id": 0, "name": "host"}, 3: {"player_id": 3, "name": "me"}}
lobby._travel_arrived = {0}
lobby._travel_go_ts = time.time() - 40
lobby._travel_active = True
_SENT.clear()
lobby._travel_progress_check(False)
check('30/60s 进度检查不解锁', lobby._travel_active is True)
check('进度检查不打 travel_missing',
      not any(m.get("type") == "travel_missing" for m in _SENT))
_SENT.clear()
lobby._travel_progress_check(True)
check('90s 超时解锁', lobby._travel_active is False)
check('超时广播 travel_missing',
      any(m.get("type") == "travel_missing" for m in _SENT))

# 全员抵达 → 快照刷新
network._is_host = True
network._clients = {3: (s1, ("10.0.0.1", 1))}
lobby._travel_active = True
lobby._travel_arrived = {0, 3}
_SENT.clear()
lobby._check_travel_all_arrived()
check('全员抵达广播 all_arrived',
      any(m.get("type") == "travel_all_arrived" for m in _SENT))
check('全员抵达后刷新快照',
      any(m.get("type") == "world_snapshot" for m in _SENT))

# 状态板写入
lobby._write_state_file()
import json  # noqa: E402
with open(lobby.LOBBY_STATE_PATH, "r", encoding="utf-8") as f:
    _state = json.load(f)
check('状态文件含 travel 板', isinstance(_state.get("travel"), dict))
check('travel 板含 arrived/pending 字段',
      "arrived" in _state["travel"] and "pending" in _state["travel"])

# ============ E: sock→pid 反向索引 ============
print('[E] sock→pid 反向索引')
sE = _FakeSock()
network._clients = {9: (sE, ("1.2.3.4", 9))}
network._sock_pid_index = {id(sE): 9}
check('索引查询命中', network._sock_to_pid(sE) == 9)
check('未注册 socket 返回 None', network._sock_to_pid(_FakeSock()) is None)

# ============ F: 启动器命令桥 ============
print('[F] 启动器命令桥（mp_travel / mp_travel_auto）')
_calls = []
lobby.host_start_travel = lambda: _calls.append("travel")
lobby.toggle_travel_auto_ack = lambda: _calls.append("auto")
network._last_cmd_ts = 0
with open(network.CMD_PATH, "w", encoding="utf-8") as f:
    json.dump({"cmd": "mp_travel", "ts": time.time() + 100}, f)
network._check_launcher_cmd()
with open(network.CMD_PATH, "w", encoding="utf-8") as f:
    json.dump({"cmd": "mp_travel_auto", "ts": time.time() + 200}, f)
network._check_launcher_cmd()
check('mp_travel 分发到 host_start_travel', "travel" in _calls)
check('mp_travel_auto 分发到 toggle', "auto" in _calls)

# ============ 结果 ============
print()
print("=" * 50)
if failures:
    print("失败项:")
    for f_ in failures:
        print("  ❌", f_)
print("结果: {} 通过, {} 失败".format(pc, fc))
sys.exit(1 if fc else 0)
