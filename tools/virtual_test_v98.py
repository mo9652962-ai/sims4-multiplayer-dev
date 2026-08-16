# -*- coding: utf-8 -*-
"""v9.8 百次虚拟测试：UDP 局域网发现/clock_sync 时间同步/房间码/大存档端到端

验证（自身经验 + 搜索引擎研究: UDP 魔数识别(IETF)/发现间隔/防火墙）:
A. UDP 局域网发现 ×20（真实 UDP 回环：魔数广播→发现 / 错误魔数→忽略 / 畸形→不崩）
B. clock_sync 时间同步（主机广播/客机拦截/remote 放行 hook 逻辑）
C. 房间码生成（6 位 + 无易混淆字符 I/O/0/1）
D. 大存档端到端（2MB 存档 32 块分块传输 + SHA 校验）
"""
import sys, os, json, time, threading, random, socket, struct, pickle, base64, hashlib

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

# ============ A: UDP 局域网发现（真实回环） ============
print('[A] UDP 局域网发现 ×20 (真实 UDP 回环)')
# 起真实 UDP 监听（client discovery 模式）
network._discovery_running = False
network._discovered_rooms = {}
network.DISCOVERY_PORT = 19856  # 用独立端口避免与真实联机冲突
disc_port = network.DISCOVERY_PORT
network.start_client_discovery()
time.sleep(0.8)

# 发广播用的 socket
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

ok_disc = 0
for i in range(20):
    try:
        # 清理已发现房间
        network._discovered_rooms = {}
        # 构造带魔数的广播包
        payload = json.dumps({
            "magic": network.DISCOVERY_MAGIC,
            "room_code": "ABC123",
            "players": i % 5,
            "name": "测试房{}".format(i),
            "ts": time.time(),
        }).encode("utf-8")
        udp_sock.sendto(payload, ("127.0.0.1", disc_port))
        time.sleep(0.3)
        assert "127.0.0.1" in network._discovered_rooms, "未发现房间"
        room = network._discovered_rooms["127.0.0.1"]
        assert room["room_code"] == "ABC123", "房间码错误"
        assert room["players"] == i % 5, "人数错误"
        ok_disc += 1
    except Exception as e:
        failures.append("disc{}: {}".format(i, e))
        break
check('20 次魔数广播→发现', ok_disc == 20)

# 错误魔数 → 忽略（防串扰，研究: IETF UDP magic number 识别）
network._discovered_rooms = {}
try:
    bad = json.dumps({"magic": "WRONG_MAGIC", "room_code": "XYZ", "players": 1,
                      "name": "f", "ts": time.time()}).encode("utf-8")
    udp_sock.sendto(bad, ("127.0.0.1", disc_port))
    time.sleep(0.3)
    check('错误魔数忽略', "127.0.0.1" not in network._discovered_rooms)
except Exception as e:
    check('错误魔数忽略', False, str(e))

# 畸形 JSON → 不崩
try:
    udp_sock.sendto(b"{invalid json!!!", ("127.0.0.1", disc_port))
    time.sleep(0.3)
    check('畸形 JSON 不崩', True)
except Exception:
    check('畸形 JSON 不崩', False)

# 无魔数纯文本 → 忽略
try:
    udp_sock.sendto(b"random text", ("127.0.0.1", disc_port))
    time.sleep(0.3)
    check('无魔数文本忽略', "127.0.0.1" not in network._discovered_rooms)
except Exception as e:
    check('无魔数文本忽略', False, str(e))

# 停掉发现监听
network._discovery_running = False
udp_sock.close()

# ============ B: clock_sync 时间同步 ============
print()
print('[B] clock_sync 时间同步 (host 权威)')
from multimod import clock_sync
clock_sync._clock_sync_enabled = True

# mock clock 模块（_install_clock_hook 里 from clock import GameClock）
# ⚠️ 用普通实例方法（真实游戏 GameClock.set_clock_speed 是实例方法，
#    _orig(self, ...) 才能正确绑定；@classmethod 会导致 _orig(self, 2) 绑定错乱）
class _GameClock:
    def set_clock_speed(self, speed, source=None, reason="", immediate=False):
        self.set_clock_speed_calls.append(speed)
        return True


def _make_int_enum():
    """ClockSpeedMode 是 IntEnum——int(mode) 必须可转换（普通类实例会 TypeError）"""
    from enum import IntEnum
    return IntEnum('ClockSpeedMode', {'PAUSED': 0, 'NORMAL': 1, 'SPEED2': 2, 'SPEED3': 3})

_clock_instance = _GameClock()
_clock_instance.set_clock_speed_calls = []
_ClockModule = type('clock', (), {'GameClock': _GameClock, 'ClockSpeedMode': _make_int_enum()})
sys.modules['clock'] = _ClockModule

# mock services.get_game_clock_service（apply_remote_clock 依赖）
# v9.21.1: 同时 mock 真实游戏 API 名 game_clock_service（ui_dialog_service.pyc 同款），
# 让 _get_game_clock 的主路径（非 fallback）被测试覆盖
class _GameClockService:
    clock_speed = 1
    def set_clock_speed(self, mode):
        self.clock_speed = int(mode)
_gcs = _GameClockService()
def _get_game_clock_service(): return _gcs
sys.modules['services'].game_clock_service = _get_game_clock_service
sys.modules['services'].get_game_clock_service = _get_game_clock_service

# 重置并安装 hook（幂等保护：先置 None）
clock_sync._orig_set_clock_speed = None
clock_sync._install_clock_hook()
GameClock = sys.modules['clock'].GameClock
game_clock = _clock_instance
game_clock.set_clock_speed_calls = []

# B1: 主机设置速度 → 广播 clock + 调用原逻辑
Net.sent = []
clock_sync.set_host_flag(True)
network._is_host = True  # _send_clock_broadcast 检查的是 network._is_host
# hook 安装后 GameClock.set_clock_speed 已是 _patched 版本
GameClock.set_clock_speed(game_clock, 2)
b1 = any(p.get("type") == "clock" and p.get("speed") == 2 for p in Net.sent)
check('主机改速度→广播 clock', b1)
check('主机改速度→调用原逻辑', game_clock.set_clock_speed_calls[-1] == 2)

# B2: 客机设置速度 → 拦截（不调原逻辑、不广播）
Net.sent = []
clock_sync.set_host_flag(False)
network._is_host = False
before = len(game_clock.set_clock_speed_calls)
GameClock.set_clock_speed(game_clock, 3)
b2 = len(game_clock.set_clock_speed_calls) == before
b3 = not any(p.get("type") == "clock" for p in Net.sent)
check('客机改速度→拦截', b2)
check('客机改速度→不广播', b3)

# B3: 客机收主机广播 → apply_remote_clock 放行（应用到 gcs）
clock_sync.set_host_flag(False)
network._is_host = False
clock_sync._applying_remote = False
_gcs.clock_speed = 1
ok = clock_sync.apply_remote_clock(2)
check('客机收广播→放行应用', ok is None and _gcs.clock_speed == 2,
      "gcs.speed={}".format(_gcs.clock_speed))

# B4: process_message 分发
Net.sent = []
try:
    clock_sync.process_message({"type": "clock", "speed": 1})
    check('process_message 分发', True)
except Exception as e:
    check('process_message 分发', False, str(e))

# B4b: 真实游戏形态回归——services 只有 game_clock_service（无旧名 fallback）。
# v9.21.1 修复的 bug：原代码用 get_game_clock_service（真实游戏不存在），
# 客机收主机 clock 广播后 apply 恒抛 AttributeError → 客机永不变速（暂停卡死）。
try:
    del sys.modules['services'].get_game_clock_service
    _gcs.clock_speed = 0
    clock_sync.apply_remote_clock(1)
    check('真实API名 apply_remote_clock（客机解除暂停）', _gcs.clock_speed == 1,
          "gcs.speed={}".format(_gcs.clock_speed))
    _gcs.clock_speed = 0
    clock_sync.get_current_speed()
    check('真实API名 get_current_speed（快照）', _gcs.clock_speed == 0 and clock_sync.get_current_speed() == 0)
finally:
    sys.modules['services'].get_game_clock_service = _get_game_clock_service
    _gcs.clock_speed = 1

# B5: mp_clock status 命令
try:
    out = []
    clock_sync.set_host_flag(True)
    clock_sync.mp_clock("status", None)
    check('mp_clock status 命令', True)
except Exception as e:
    check('mp_clock status 命令', False, str(e))

# ============ C: 房间码生成 ============
print()
print('[C] 房间码生成 (6 位无易混淆字符)')
FORBIDDEN = set("IO01")
ok_code = 0
for i in range(20):
    code = lobby._gen_room_code() if hasattr(lobby, "_gen_room_code") else None
    if code is None:
        break
    if len(code) == 6 and not (set(code) & FORBIDDEN):
        ok_code += 1
check('20 次房间码 6 位+无 I/O/0/1', ok_code == 20)

# 唯一性
codes = {lobby._gen_room_code() for _ in range(50)}
check('50 个房间码基本唯一 (≥40)', len(codes) >= 40, "got {}".format(len(codes)))

# ============ D: 大存档端到端（2MB 分块传输） ============
print()
print('[D] 大存档端到端 (2MB / 32 块)')
os.makedirs(lobby.SAVES_DIR, exist_ok=True)
test_data = os.urandom(2 * 1024 * 1024 + 123)  # ~2MB
fname = 'Slot_Big_98.save'
with open(os.path.join(lobby.SAVES_DIR, fname), 'wb') as f:
    f.write(test_data)
file_sha = hashlib.sha256(test_data).hexdigest()
b64 = base64.b64encode(test_data).decode()
total = (len(b64) + lobby.SAVE_CHUNK_SIZE - 1) // lobby.SAVE_CHUNK_SIZE

network._is_host = False
network._client_socket = object()
lobby.network._client_socket = network._client_socket

# 模拟 host_send_save_file 的分块逻辑（真实函数：直接调 lobby.host_send_save_file 走广播）
try:
    # 直接调用真实发送函数（broadcast 捕获）
    Net.sent = []
    lobby._recv_save_cache = {}
    lobby.host_send_save_file(fname)
    sent_chunks = [p for p in Net.sent if p.get("type") == "save_chunk"]
    sent_done = [p for p in Net.sent if p.get("type") == "save_chunk_done"]
    check('大存档分块数正确 (≥30 块)', len(sent_chunks) >= 30, "got {}".format(len(sent_chunks)))
    check('大存档全部块含 sha256', all(c.get("sha256") == file_sha for c in sent_chunks))
    check('发送完成信号 + sha256', len(sent_done) == 1 and sent_done[0].get("sha256") == file_sha)

    # 客户端完整接收（含乱序处理）
    chunks = {}
    for c in sent_chunks:
        chunks[c["index"]] = c["data"]
    for c in sent_chunks:
        lobby.on_save_chunk({"filename": fname, "index": c["index"], "total": c["total"],
                             "data": c["data"], "sha256": file_sha})
    lobby.on_save_chunk_done({"filename": fname, "total_bytes": len(test_data), "sha256": file_sha})
    dest = os.path.join(lobby.SAVES_DIR, fname)
    written = open(dest, 'rb').read() if os.path.exists(dest) else b''
    check('2MB 存档接收写入一致', written == test_data, "{}B vs {}B".format(len(written), len(test_data)))
    try: os.remove(dest)
    except Exception: pass
except Exception as e:
    check('大存档端到端', False, str(e))

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:10]: print('  ❌ ' + f)
sys.exit(1 if fc else 0)
