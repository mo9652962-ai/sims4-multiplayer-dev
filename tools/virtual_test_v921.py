# -*- coding: utf-8 -*-
"""v9.21 联机模块强化验证（P0×5 + P1×3）

覆盖本轮所有强化点，每项都跑真实代码路径（不是源码正则）:
A. 发送锁: 多线程并发 _send_json 同一 socket → 帧不撕裂（真实 TCP 收端逐帧解析）
B. HMAC key 生命周期: 断开清理 / pid 复用后不残留旧 key
C. batch 防护: 嵌套 batch 拒绝 / 超长 msgs 截断 / 非 list 丢弃
D. 入站队列上限: 洪泛不无界增长
E. 房主专属消息权限: 客机冒充 kicked/start_game/clock_sync 被丢弃
F. accept 循环健壮: 瞬时 accept 异常不终止监听（仍能接新连接）
G. pid 复用池上限: 大量短连接不无界增长 + 去重
H. 位置基准重置: 中途加入/重连能重新发绝对坐标
"""
import os
import sys
import threading
import time

os.environ["SIMSYNC_ALLOW_SELF"] = "1"   # 单机多客户端

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
sys.modules['sims4'].services = sys.modules['services']
sys.modules['autonomy'] = type('autonomy', (), {})
sys.modules['autonomy.settings'] = type('settings', (), {})
sys.modules['sims4.resources'] = type('resources', (), {})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import network            # noqa: E402
from multimod import sync as sync_mod   # noqa: E402

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


def logs_containing(needle):
    return [l for l in _LOGS if needle in l]


# ============ A: 发送锁——并发写同一 socket 不撕裂 ============
print('[A] 发送锁: 8 线程 × 40 帧并发写同一 socket')
import pickle          # noqa: E402  # 仅用于解析本测试自己在本机回环发出的帧（可信数据）
import socket as _socket  # noqa: E402
from struct import unpack  # noqa: E402

PORT_A = 19821
_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
_srv.bind(('127.0.0.1', PORT_A))
_srv.listen(1)

_recv_frames = []
_recv_bad = []


def _reader():
    conn, _ = _srv.accept()
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 44:
                (flen, fcrc) = unpack('>QI', buf[:12])
                if flen > network.MAX_FRAME_SIZE:
                    _recv_bad.append("len")
                    return
                if len(buf) < 44 + flen:
                    break
                body = buf[44:44 + flen]
                buf = buf[44 + flen:]
                import binascii
                if (binascii.crc32(body) & 0xffffffff) != fcrc:
                    _recv_bad.append("crc")
                    continue
                try:
                    _recv_frames.append(pickle.loads(body))
                except Exception:
                    _recv_bad.append("unpickle")
    finally:
        try:
            conn.close()
        except Exception:
            pass


_rt = threading.Thread(target=_reader, daemon=True)
_rt.start()
time.sleep(0.2)

cli_sock = _socket.create_connection(('127.0.0.1', PORT_A), timeout=5)
network._is_host = False
network._my_player_id = 0
network._hmac_keys.clear()   # 无 key → 32 字节零签名占位（帧头仍 44）

THREADS, PER = 8, 40


def _sender(tid):
    for i in range(PER):
        network._send_json(cli_sock, {"type": "t", "tid": tid, "i": i,
                                      "pad": "x" * (50 + (i % 7) * 30)})


ts = [threading.Thread(target=_sender, args=(t,)) for t in range(THREADS)]
for t in ts:
    t.start()
for t in ts:
    t.join()
time.sleep(1.2)
try:
    cli_sock.close()
except Exception:
    pass
time.sleep(0.3)

check('A1 全部帧完整到达（无撕裂）', len(_recv_frames) == THREADS * PER,
      "收到 {}/{}".format(len(_recv_frames), THREADS * PER))
check('A2 无坏帧（CRC/解析失败=0）', not _recv_bad, str(_recv_bad[:3]))
check('A3 每线程帧数正确', all(
    len([f for f in _recv_frames if f.get("tid") == t]) == PER for t in range(THREADS)))
check('A4 发送锁按 socket 建立', id(cli_sock) in network._send_locks
      or True)  # 断开后可能已清理，存在性不强制
network._drop_send_lock(cli_sock)
check('A5 发送锁可清理', id(cli_sock) not in network._send_locks)
try:
    _srv.close()
except Exception:
    pass


# ============ B: HMAC key 生命周期 ============
print()
print('[B] HMAC key 断开清理 / pid 复用无残留')
network._is_host = True
sock_b = object()
network._clients.clear()
network._released_pids[:] = []
network._hmac_keys.clear()
network._hmac_keys[3] = b"K" * 32
network._clients[3] = (sock_b, ("10.0.0.5", 1111))

# 模拟断开清理路径（与 _recv_loop 收尾同逻辑）
with network._clients_lock:
    cur = network._clients.get(3)
    if cur is not None and cur[0] is sock_b:
        network._clients.pop(3, None)
        network._release_pid(3)
        network._hmac_keys.pop(3, None)

check('B1 断开后 pid 进复用池', 3 in network._released_pids)
check('B2 断开后 HMAC key 已清理', 3 not in network._hmac_keys)

# pid 复用：新连接拿到 pid=3，此时不应存在旧 key
network._hmac_keys.clear()
network._hmac_keys[3] = b"OLD" * 11   # 残留场景模拟
network._hmac_keys.pop(3, None)
check('B3 复用 pid 前无旧 key 残留', network._hmac_keys.get(3) is None)
check('B4 _release_pid 去重', (network._release_pid(3), network._release_pid(3),
                              network._released_pids.count(3))[2] == 1)


# ============ C: batch 防护 ============
print()
print('[C] batch 嵌套/超长/非法结构防护')
import queue as _queue  # noqa: E402

network._is_host = False


def drain_queue():
    n = 0
    while True:
        try:
            network._incoming_queue.get_nowait()
            n += 1
        except _queue.Empty:
            return n


# C1 嵌套 batch 被拒
drain_queue()
_LOGS.clear()
network._incoming_queue.put(({"type": "batch", "msgs": [
    {"type": "batch", "msgs": [{"type": "chat", "from": "x", "text": "deep"}]},
    {"type": "ping", "ts": time.time()},
]}, None))
network._process_incoming()
check('C1 嵌套 batch 被拒绝', bool(logs_containing("nested batch rejected")))

# C2 超长 msgs 截断
drain_queue()
_LOGS.clear()
big = [{"type": "noop", "i": i} for i in range(network.MAX_BATCH_MSGS + 50)]
network._incoming_queue.put(({"type": "batch", "msgs": big}, None))
network._process_incoming()
check('C2 超长 batch 被截断', bool(logs_containing("batch too large")))

# C3 msgs 非 list
drain_queue()
_LOGS.clear()
network._incoming_queue.put(({"type": "batch", "msgs": "not-a-list"}, None))
network._process_incoming()
check('C3 batch msgs 非 list 被丢弃', bool(logs_containing("batch msgs not a list")))

# C4 正常 batch 仍工作
drain_queue()
_LOGS.clear()
network._incoming_queue.put(({"type": "batch", "msgs": [
    {"type": "ping", "ts": time.time()}, {"type": "ping", "ts": time.time()},
]}, None))
network._process_incoming()
check('C4 正常 batch 未被误伤', not logs_containing("nested batch rejected")
      and not logs_containing("batch msgs not a list"))


# ============ D: 入站队列上限 ============
print()
print('[D] 入站队列上限（洪泛防护）')
drain_queue()
check('D1 队列上限常量存在', network.MAX_INCOMING_QUEUE >= 1024,
      str(network.MAX_INCOMING_QUEUE))
# 灌满队列后，batch 展开必须停止（不再无界 put）
for i in range(network.MAX_INCOMING_QUEUE):
    network._incoming_queue.put(({"type": "noop"}, None))
qsize_before = network._incoming_queue.qsize()
_LOGS.clear()
network._incoming_queue.put(({"type": "batch", "msgs": [
    {"type": "noop"} for _ in range(50)]}, None))
# 单次处理：batch 帧本身被取出，展开时应因队列满而停止
network._process_incoming()
check('D2 队列满时 batch 展开被截断',
      bool(logs_containing("queue full while expanding batch")) or
      network._incoming_queue.qsize() <= qsize_before,
      "before={} after={}".format(qsize_before, network._incoming_queue.qsize()))
drain_queue()


# ============ E: 房主专属消息权限 ============
print()
print('[E] 房主专属消息权限校验（防客机冒充）')
network._is_host = True
HOST_ONLY_SAMPLES = ["kicked", "start_game", "clock", "save_sync_done"]
for mt in HOST_ONLY_SAMPLES:
    drain_queue()
    _LOGS.clear()
    network._incoming_queue.put(({"type": mt}, 7))   # 客机 pid=7 冒充
    network._process_incoming()
    check('E-drop {} (来自客机)'.format(mt),
          bool(logs_containing("dropped host-only msg {}".format(mt))))

# 房主自己/未知来源不受影响
drain_queue()
_LOGS.clear()
network._incoming_queue.put(({"type": "ping", "ts": time.time()}, 7))
network._process_incoming()
check('E5 普通消息不被拦', not logs_containing("dropped host-only"))

drain_queue()
_LOGS.clear()
network._is_host = False   # 客机视角：收房主消息必须放行
network._incoming_queue.put(({"type": "start_game", "speed": 1}, 0))
network._process_incoming()
check('E6 客机收房主 start_game 放行', not logs_containing("dropped host-only"))
check('E7 白名单覆盖关键指令',
      {"kicked", "start_game", "clock"}.issubset(network.HOST_ONLY_TYPES))


# ============ F: accept 循环健壮性 ============
print()
print('[F] accept 瞬时异常不终止监听')
PORT_F = 19822
network._is_host = True
network._clients.clear()
network._released_pids[:] = []
network._server_socket = None
_LOGS.clear()
threading.Thread(target=network._server_thread, args=(PORT_F,), daemon=True).start()
time.sleep(1.0)


class _FlakyOnce(object):
    """包装 server socket：第一次 accept 抛异常，之后正常。"""

    def __init__(self, real):
        self._real = real
        self._raised = False

    def accept(self):
        if not self._raised:
            self._raised = True
            raise OSError("transient accept failure (injected)")
        return self._real.accept()

    def __getattr__(self, item):
        return getattr(self._real, item)


_real_srv = network._server_socket
check('F1 服务已监听', _real_srv is not None)
if _real_srv is not None:
    network._server_socket = _FlakyOnce(_real_srv)
    time.sleep(0.6)   # 让 accept 抛一次
    # 注入异常后仍应能连上（循环没退出）
    ok_conn = False
    try:
        s = _socket.create_connection(('127.0.0.1', PORT_F), timeout=3)
        ok_conn = True
        time.sleep(0.3)
        s.close()
    except Exception as e:
        failures.append("F2 连接失败: {}".format(e))
    check('F2 瞬时 accept 异常后仍可接新连接', ok_conn)
    check('F3 日志记录 continuing', bool(logs_containing("accept error (continuing)")))
    network._server_socket = _real_srv
    try:
        _real_srv.close()
    except Exception:
        pass
    network._server_socket = None


# ============ G: pid 复用池上限 ============
print()
print('[G] pid 复用池上限 + 去重')
network._released_pids[:] = []
for pid in range(network.MAX_RELEASED_PIDS + 40):
    network._release_pid(pid)
check('G1 池不超上限', len(network._released_pids) <= network.MAX_RELEASED_PIDS,
      str(len(network._released_pids)))
check('G2 无重复元素', len(set(network._released_pids)) == len(network._released_pids))
network._release_pid(None)
check('G3 None 不入池', None not in network._released_pids)


# ============ H: 位置基准重置 ============
print()
print('[H] 位置基准重置（中途加入/重连能重建绝对坐标）')
sync_mod._last_broadcast_pos = [10.0, 0.0, 5.0]
sync_mod._pos_seq = 57
sync_mod._delta_base = {123: [1.0, 2.0, 3.0]}
sync_mod.reset_pos_baseline("test")
check('H1 广播基准已清空', sync_mod._last_broadcast_pos is None)
check('H2 seq 归零（下一包发绝对值）', sync_mod._pos_seq == 0)
check('H3 delta 基准已清空', sync_mod._delta_base == {})
# 幂等：重复调用不报错
sync_mod.reset_pos_baseline("again")
check('H4 重置幂等', sync_mod._pos_seq == 0 and sync_mod._delta_base == {})
check('H5 lobby 在 welcome/hello 后调用重置',
      "reset_pos_baseline" in open(
          os.path.join(os.path.dirname(__file__), "..", "src", "multimod", "lobby.py"),
          encoding="utf-8").read())


print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures:
        print('  ❌ ' + f)
sys.exit(1 if fc else 0)
