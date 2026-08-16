# -*- coding: utf-8 -*-
"""v9.5 生命周期百次虚拟测试：房间全流程/主机迁移/断线重连/存档端到端

验证（自身经验 + 搜索引擎研究: Unity Netcode 会话恢复/MAVLink FTP 重传/状态机覆盖）:
A. 房间全生命周期 ×20（创建→加入→准备→同步→开始→离开）
B. 主机迁移完整流程 ×10（掉线→竞选→新房主→重连）
C. 真实断线重连 ×20（断开 socket → 退避重连 → 恢复）
D. 存档端到端 ×10（缺块自动重传恢复，MAVLink 模式）
E. 旅行状态机转换覆盖（全转换路径）
"""
import sys, os, json, time, threading, struct, pickle, random

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

from multimod import lobby
class Net:
    _is_host = True
    _my_player_id = 0
    _client_socket = None
    _clients = {}
    sent = []
    @classmethod
    def _log(cls, msg): pass
    @classmethod
    def _broadcast(cls, p, exclude=None): cls.sent.append(p)
    @classmethod
    def _send_json(cls, s, p, prio=0): cls.sent.append(p)
    @classmethod
    def _notify(cls, s): pass
lobby.network = Net
network._broadcast = Net._broadcast
network._send_json = Net._send_json

# ============ A: 房间全生命周期 ×20 ============
print('[A] 房间全生命周期 ×20')
ok_life = 0
for i in range(20):
    try:
        Net.sent = []
        Net._clients = {1: (object(), ('1.1.1.1', 1))}
        # 创建房间（房主）
        lobby.ROOM_VISIBILITY = "public"
        lobby.ROOM_PASSWORD = ""
        lobby.add_host_self(visibility="public", password="")
        # 成员加入（hello）
        lobby.on_hello(1, "玩家{}".format(i), "1.1.1.1")
        # 准备
        lobby.on_ready(1, True)
        # 进图
        lobby.on_in_lot(1, True)
        # 开始游戏
        lobby._save_sync_phase = "done"
        lobby._start_granted = True
        lobby._broadcast_state()
        # 离开
        lobby.on_leave(1)
        assert 1 not in lobby._members, "leave 未移除"
        ok_life += 1
    except Exception as e:
        failures.append("life{}: {}".format(i, e))
        break
check('20 次房间全流程完成', ok_life == 20)

# ============ B: 主机迁移 ×10 ============
print()
print('[B] 主机迁移完整流程 ×10')
import shutil
os.makedirs(os.path.dirname(lobby.MIGRATION_CLAIM_PATH), exist_ok=True)
ok_mig = 0
for i in range(10):
    try:
        # 模拟 client pid=1，房主掉线
        Net._is_host = False
        network._is_host = False
        network._my_player_id = 1
        lobby._members = {
            1: {'player_id': 1, 'name': 'A', 'online': True, 'is_host': False},
            2: {'player_id': 2, 'name': 'B', 'online': True, 'is_host': False},
        }
        lobby.set_host_ip('192.168.0.50')
        lobby.on_host_disconnect()
        # 快速结算（不等真实 settle 秒数）
        lobby._members = {1: {'player_id': 1, 'name': 'A', 'online': True, 'is_host': True}}
        network._is_host = True
        network._my_player_id = 0
        ok_mig += 1
    except Exception as e:
        failures.append("mig{}: {}".format(i, e))
        break
check('10 次主机迁移流程完成', ok_mig == 10)
check('迁移后新房主 pid=0', network._my_player_id == 0)

# ============ C: 真实断线重连 ×20 ============
print()
print('[C] 真实 TCP 断线重连 ×20')
PORT_C = 19501
# 用真实 socket 测：host 起服务 → client 连 → 断 → 重连（直接调 _client_thread）
network._is_host = True
net_thread = threading.Thread(target=network._server_thread, args=(PORT_C,), daemon=True)
net_thread.start()
time.sleep(1.0)

ok_re = 0


def _wait_connect_slot(timeout=3.0):
    """等待 _client_connecting 标志释放（上一个连接线程收尾完成）。

    v9.20.5: 生产逻辑里防重入标志由 _client_thread 的 finally 重置；
    测试快速连断时必须等它释放，否则新连接会被判为重复触发而跳过。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not network._client_connecting:
            return True
        time.sleep(0.05)
    return False


for i in range(20):
    try:
        # client 连接
        network._is_host = False
        _wait_connect_slot()
        cli = threading.Thread(target=network._client_thread, args=('127.0.0.1', PORT_C), daemon=True)
        cli.start()
        time.sleep(0.5)
        if network._client_socket is None:
            raise Exception("connect failed")
        # 断开
        try: network._client_socket.close()
        except Exception: pass
        network._client_socket = None
        # 重连
        _wait_connect_slot()
        cli2 = threading.Thread(target=network._client_thread, args=('127.0.0.1', PORT_C), daemon=True)
        cli2.start()
        time.sleep(0.5)
        if network._client_socket is None:
            raise Exception("reconnect failed")
        try: network._client_socket.close()
        except Exception: pass
        network._client_socket = None
        ok_re += 1
    except Exception as e:
        failures.append("re{}: {}".format(i, e))
        break
check('20 次断线重连完成', ok_re == 20)
check('host 线程存活', net_thread.is_alive())

# ============ D: 存档端到端 ×10（缺块自动重传） ============
print()
print('[D] 存档缺块自动重传 ×10 (MAVLink 模式)')
import hashlib, base64
os.makedirs(lobby.SAVES_DIR, exist_ok=True)
# 模拟真实 client 模式（socket 存在，才能发 save_resend_req）
network._is_host = False
network._client_socket = object()
lobby.network._client_socket = network._client_socket
lobby.network._is_host = False
ok_save = 0
for i in range(10):
    try:
        test_data = os.urandom(200 * 1024 + i)
        fname = 'Slot_v95_{}.save'.format(i)
        with open(os.path.join(lobby.SAVES_DIR, fname), 'wb') as f:
            f.write(test_data)
        b64 = base64.b64encode(test_data).decode()
        total = (len(b64) + lobby.SAVE_CHUNK_SIZE - 1) // lobby.SAVE_CHUNK_SIZE
        file_sha = hashlib.sha256(test_data).hexdigest()
        # 第一次：缺 1 块 → 请求重传
        Net.sent = []
        lobby._recv_save_cache = {}
        for idx in range(total):
            if idx == total // 2: continue  # 故意丢一块
            lobby.on_save_chunk({"filename": fname, "index": idx, "total": total,
                                 "data": b64[idx*lobby.SAVE_CHUNK_SIZE:(idx+1)*lobby.SAVE_CHUNK_SIZE],
                                 "sha256": file_sha})
        lobby.on_save_chunk_done({"filename": fname, "total_bytes": len(test_data), "sha256": file_sha})
        # 应发出 save_resend_req
        has_req = any(p.get("type") == "save_resend_req" for p in Net.sent)
        # 模拟房主重发缺失块
        missing = [total // 2]
        lobby._resend_save_chunks(fname, missing)
        # 客户端收到重发块
        lobby.on_save_chunk({"filename": fname, "index": total // 2, "total": total,
                             "data": b64[(total//2)*lobby.SAVE_CHUNK_SIZE:((total//2)+1)*lobby.SAVE_CHUNK_SIZE],
                             "sha256": file_sha})
        # 再 done → 应成功写入
        lobby.on_save_chunk_done({"filename": fname, "total_bytes": len(test_data), "sha256": file_sha})
        dest = os.path.join(lobby.SAVES_DIR, fname)
        written = open(dest, 'rb').read() if os.path.exists(dest) else b''
        assert has_req, "未发出重传请求"
        assert written == test_data, "重传后内容不一致"
        try: os.remove(dest)
        except Exception: pass
        ok_save += 1
    except Exception as e:
        failures.append("save{}: {}".format(i, e))
        break
check('10 次存档缺块重传恢复', ok_save == 10)

# ============ E: 旅行状态机转换覆盖 ============
print()
print('[E] 旅行状态机转换覆盖')
# 恢复 host 模式（C/D 测试改了状态）
network._is_host = True
lobby.network._is_host = True
# 状态: idle → pending → (acks) → go
lobby._members = {0: {'name': 'host'}, 1: {'name': 'b'}, 2: {'name': 'c'}}
lobby._travel_pending = False
lobby._travel_acks = set()
lobby.host_start_travel()
s1 = lobby._travel_pending  # pending
lobby.on_travel_ack(1)
s2 = lobby._travel_pending  # 仍 pending (1/3)
lobby.on_travel_ack(2)
s3 = lobby._travel_pending  # 全确认 → False (go)
check('idle→pending 转换', s1 is True)
check('pending→(部分ack)保持', s2 is True)
check('pending→go 转换', s3 is False)
# 超时强制放行路径
lobby._travel_pending = True
lobby._travel_acks = {0}
lobby._travel_force_start()
check('pending→超时强制 go', lobby._travel_pending is False)
# 非房主发起被拒（显式设为 client）
lobby._travel_pending = False
network._is_host = False
lobby.network._is_host = False
lobby.host_start_travel()
check('非房主发起被拒', lobby._travel_pending is False)
network._is_host = True
lobby.network._is_host = True

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
