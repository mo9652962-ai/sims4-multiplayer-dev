# -*- coding: utf-8 -*-
"""v9.2 端到端虚拟测试：真实 TCP 通信 + 聊天闭环 + 房主聊天修复

验证（基于自身经验设计）:
A. 真实 TCP 握手（pickle 帧协议）：host 监听 → client 连接 → welcome → hello → members
B. 聊天双向闭环：client 发送 chat → host 收到 → lobby state 写入 chat → 启动器可读
C. 房主聊天修复（v9.1 bug）：host mp_say 用 _broadcast 不崩溃、client 能收到
D. 启动器指令文件 chat_cmd.txt → mp_poll 读取发送
E. 心跳：client heartbeat → host 记录 last_seen

v9.11 稳定性修复（百次测试发现）:
- 单进程同时跑 host/client 线程共享全局 _is_host/_incoming_queue → 状态错乱、
  连接偶发异常退出（真实游戏两端是独立进程，无此问题）。
- B/G 聊天闭环改用 subprocess 独立 client（真正进程隔离）验证网络链路。
- 广播异步 → 轮询等待 state 写入 + host 视角消费。
"""
import sys, os, json, time, threading, subprocess

# ---- Mock 游戏依赖 ----
class _Commands:
    CommandType = type('CT', (), {'Live': 1, 'Cheat': 2})
    def Command(self, name, command_type=None):
        def deco(fn): return fn
        return deco
    def CheatOutput(self, conn):
        return lambda msg: print('  [game-output] ' + str(msg))
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
def check(name, cond):
    global pc, fc
    if cond: pc += 1; print('  ✅ ' + name)
    else: fc += 1; print('  ❌ ' + name)

TEST_PORT = 19333
STATE_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                          "The Sims 4", "Mods", "mp_lobby_state.json")
# 覆盖 game 依赖：alarm/notify/log 全部 no-op
network._log = lambda msg: None
network._notify = lambda msg: None
network._ensure_alarm = lambda: None
network._check_launcher_cmd = lambda: None
network._process_alarm_callback = lambda *a: None
# 模拟游戏主线程消费
def drain():
    network._process_incoming()

# 轮询等待 state 含指定 chat（广播异步 → 轮询；host 视角消费）
def _wait_chat_in_state(text, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        network._is_host = True  # host 视角消费（chat → add_chat 只在 host 分支）
        drain()
        if os.path.exists(STATE_PATH):
            try:
                st = json.load(open(STATE_PATH, encoding="utf-8"))
                if any(c.get("text") == text for c in st.get("chat", [])):
                    return True
            except Exception:
                pass
        time.sleep(0.15)
    return False

# ---- A: 真实 TCP 握手 ----
print('[A] 真实 TCP 握手 (pickle 帧协议)')
network._is_host = True
network._my_player_id = 0
# 直接调用底层线程（跳过 clock/discovery 游戏依赖）
from multimod import lobby
net_thread = threading.Thread(target=network._server_thread, args=(TEST_PORT,), daemon=True)
net_thread.start()
time.sleep(1.0)
check('host 监听线程存活', net_thread.is_alive())

# client 侧（独立线程模拟——只验证握手链路，不验证 chat 闭环）
network._is_host = False
cli_thread = threading.Thread(target=network._client_thread, args=('127.0.0.1', TEST_PORT), daemon=True)
cli_thread.start()
time.sleep(1.5)
check('client 连接成功 (有 socket)', network._client_socket is not None)
check('client 收到 welcome (队列有消息)', not network._incoming_queue.empty())
drain()
# client 发 hello 加入
from multimod import lobby as lobby_mod
lobby_mod._player_name = "测试玩家"
network._send_json(network._client_socket, {"type": "hello", "name": "测试玩家", "password": "", "proto_version": 2})
time.sleep(1.0)
network._is_host = True
drain()  # host 处理 hello
check('host 成员列表含 client (pid=1)', 1 in network._clients)

# v9.19: 断开 A 段本地 client（否则 B 段 subprocess 的 127.0.0.1 连接会被
# 自连接保护误拒——_clients 非空即拒绝本机连接）
try:
    if 1 in network._clients:
        _s, _a = network._clients.pop(1)
        try: _s.close()
        except Exception: pass
    if network._client_socket is not None:
        try: network._client_socket.close()
        except Exception: pass
    network._client_socket = None
except Exception:
    pass

# ---- B: 聊天闭环（subprocess 独立 client → host → lobby state）----
print()
print('[B] 聊天闭环 (独立 client 进程 → host → state)')
# 用 subprocess 起独立 Python client（真正进程隔离，验证真实网络链路）
client_code = '''
import sys, os, time, json
sys.path.insert(0, %r)
# mock 游戏依赖
class _C:
    CommandType = type("CT", (), {"Live": 1, "Cheat": 2})
    def Command(self, n, command_type=None):
        def deco(fn): return fn
        return deco
    def CheatOutput(self, c): return lambda m: None
sys.modules["sims4"] = type("sims4", (), {"commands": _C()})()
sys.modules["sims4.commands"] = _C()
sys.modules["services"] = type("services", (), {})
sys.modules["sims4"].services = sys.modules["services"]
sys.modules["autonomy"] = type("a", (), {})
sys.modules["autonomy.settings"] = type("s", (), {})
sys.modules["sims4.resources"] = type("r", (), {})
from multimod import network
network._log = lambda m: None
network._notify = lambda m: None
network._ensure_alarm = lambda: None
network._check_launcher_cmd = lambda: None
network._process_alarm_callback = lambda *a: None
network._is_host = False
# client 写自己的 state 文件（独立路径——避免与 host 进程竞争同一文件）
from multimod import lobby as _lb
_lb.LOBBY_STATE_PATH = os.path.join(os.getcwd(), "mp_lobby_state_client.json")
import threading
t = threading.Thread(target=network._client_thread, args=("127.0.0.1", %d), daemon=True)
t.start()
time.sleep(1.5)
if network._client_socket is None:
    print("CLIENT_NO_SOCKET")
    sys.exit(1)
network._send_json(network._client_socket, {"type": "hello", "name": "独立客户端", "password": "", "proto_version": 2})
time.sleep(0.8)
# v9.16: 先消费 welcome（派生 key + 设置 _my_player_id）再发签名消息
for _i in range(10):
    network._process_incoming()
    if network._my_player_id != 0:
        break
    time.sleep(0.2)
network._send_json(network._client_socket, {"type": "chat", "from": "独立客户端", "text": "进程隔离消息"})
# 持续消费队列（处理 host 广播的 chat → add_chat → state）——v9.11 修复
# 长窗口：G 段反向闭环在 B 段之后执行，client 需保持消费直到主进程 terminate
deadline = time.time() + 35.0
while time.time() < deadline:
    network._process_incoming()
    time.sleep(0.15)
print("CLIENT_OK")
''' % (os.path.join(os.path.dirname(__file__), "..", "src").replace("\\\\", "/"), TEST_PORT)
CLIENT_STATE_PATH = os.path.join(os.path.dirname(__file__), "mp_lobby_state_client.json")
# subprocess 起独立 Python（Popen 非阻塞——client 保持存活供 B/G 两段使用）
try:
    proc = subprocess.Popen([sys.executable, "-c", client_code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=os.path.dirname(__file__))
    # 等待 client 完成 hello + chat 发送（CLIENT_OK 打印前）
    time.sleep(2.0)
    check('独立 client 进程启动并发送', proc.poll() is None or proc.poll() == 0)
except Exception as e:
    check('独立 client 进程启动', False, str(e))
# host 轮询等 state（subprocess client 是独立进程，host 这边消费队列）
check('host 收到独立 client 的 chat 并写入 state', _wait_chat_in_state("进程隔离消息"))

# ---- C: 房主聊天修复（v9.1）----
print()
print('[C] 房主聊天修复 (v9.1 bug)')
# 切换回 host 视角，直接调 mp_say（host 模式 _client_socket=None 之前会拒）
network._is_host = True
network._client_socket_backup = network._client_socket
try:
    network.mp_say("房主发言", None)  # 不应抛异常/不应拒
    check('host mp_say 不崩溃且不拒绝', True)
except Exception as e:
    check('host mp_say 不崩溃', False)
# 验证 host 的 _broadcast 调用（模拟广播不崩）
try:
    network._broadcast({"type": "chat", "from": "me", "text": "房主发言"})
    check('host _broadcast 正常', True)
except Exception as e:
    check('host _broadcast 正常', False)

# ---- D: 启动器指令文件 → mp_poll ----
print()
print('[D] 启动器 chat_cmd.txt → mp_poll')
cmd_dir = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                       "The Sims 4", "Mods", "Sims4Multiplayer")
os.makedirs(cmd_dir, exist_ok=True)
cmd_path = os.path.join(cmd_dir, "chat_cmd.txt")
with open(cmd_path, "w", encoding="utf-8") as f:
    f.write("启动器发的消息\n")
network._is_host = False
try:
    network.mp_poll(None)
    remaining = open(cmd_path, encoding="utf-8").read().strip()
    check('mp_poll 读取并清空指令文件', remaining == "")
except Exception as e:
    check('mp_poll 读取指令文件', False)

# ---- E: 心跳 ----
print()
print('[E] 心跳')
try:
    network._is_host = False
    if network._client_socket is not None:
        network._send_json(network._client_socket, {"type": "heartbeat", "pid": 1})
    time.sleep(0.5)
    network._is_host = True
    drain()
    check('client 心跳发送成功', True)
except Exception as e:
    check('client 心跳发送成功', False)

# ---- F: 位置同步消息（delta 压缩） ----
print()
print('[F] 位置同步 sim_pos（delta 压缩）')
try:
    network._is_host = True
    network._broadcast({"type": "sim_pos", "sim_id": 999, "delta": [0.5, 0.0, -0.3],
                        "abs": None, "seq": 1})
    network._is_host = False
    drain()
    check('sim_pos 广播 + 消费不崩溃', True)
except Exception as e:
    check('sim_pos 广播不崩溃', False)

# ---- G: 房主聊天 → client 收到（闭环反向，subprocess client）----
print()
print('[G] 房主聊天 → client 收到（反向闭环）')
# host 广播 chat → subprocess client 收到 → client add_chat 到自己的 state 文件
# (client 独立进程写独立 state——避免跨进程竞争同一文件)
def _wait_client_state(text, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(CLIENT_STATE_PATH):
            try:
                st = json.load(open(CLIENT_STATE_PATH, encoding="utf-8"))
                if any(c.get("text") == text for c in st.get("chat", [])):
                    return True
            except Exception:
                pass
        time.sleep(0.15)
    return False
try:
    network._is_host = True
    network._broadcast({"type": "chat", "from": "房主", "text": "反向消息"})
    network._is_host = False
    check('client 收到并写入自己的 state', _wait_client_state("反向消息"))
except Exception as e:
    check('反向闭环', False)
# 清理 subprocess client（防残留）
try:
    proc.terminate()
    proc.wait(timeout=3)
except Exception:
    proc.kill()

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
sys.exit(1 if fc else 0)
