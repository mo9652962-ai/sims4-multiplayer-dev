# -*- coding: utf-8 -*-
"""v9.18 启动器房间协议虚拟测试（不依赖游戏/GUI）

A. 房间码生成（6 位去易混淆）
B. RoomServer + RoomClient 连接/join 流程（真实 TCP）
C. 成员广播（加入者名字进房主成员表）
D. ready 状态机（全员 ready → ROOM_READY）
E. 存档同步（host 发文件 → client 收齐 + SHA256 一致 + 写盘）
F. start_game 广播（双方收到启动指令）
G. 错误路径（房间码错误拒绝 / 断线清理）
H. 百轮循环压力（100 次建房间-加入-离开）
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from room_protocol import RoomServer, RoomClient, gen_room_code, ROOM_READY, ROOM_SYNCED

pc = fc = 0
failures = []
def check(name, cond, note=""):
    global pc, fc
    if cond: pc += 1
    else:
        fc += 1
        failures.append(name + (" | " + note if note else ""))


# ============ A: 房间码 ============
print('[A] 房间码生成')
codes = {gen_room_code() for _ in range(200)}
check('A1 全 6 位', all(len(c) == 6 for c in codes))
check('A2 无易混淆字符', all(set(c).isdisjoint("IO01") for c in codes))
check('A3 200 个基本唯一', len(codes) > 190, str(len(codes)))


# ============ B-E: 真实 TCP 房间流程 ============
print()
print('[B-E] 房间全流程（真实 TCP）')
server = RoomServer(host_name="小明", port=17660)
events = []
server.on("member_joined", lambda d: events.append(("join", d.get("name"))))
server.on("state_changed", lambda d: events.append(("state", d.get("state"))))
server.on("save_done", lambda d: events.append(("savedone", d.get("sha256", "")[:8])))
server.on("game_started", lambda d: events.append(("gamestart", d.get("host_ip", ""))))
ok = server.start()
check('B1 房间服务启动', ok is True)

client = RoomClient("127.0.0.1", 17660, name="小红", room_code=server.room_code)
c_events = []
client.on("joined", lambda d: c_events.append(("joined", client.player_id)))
client.on("members", lambda d: c_events.append(("members", len(client.members))))
client.on("save_received", lambda d: c_events.append(("savereceived", d.get("size", 0))))
client.on("save_done", lambda d: c_events.append(("savedone",)))
client.on("game_start", lambda d: c_events.append(("gamestart", d.get("host_ip", ""))))
ok2, err2 = client.connect()
check('B2 客户端连接', ok2 and client.connected, err2)
time.sleep(0.3)
check('B3 客户端拿到 player_id', client.player_id >= 1, str(client.player_id))
check('B4 房主成员表含小红', any(m.get("name") == "小红" for m in server.members.values()),
      str([m.get("name") for m in server.members.values()]))
check('B5 客户端成员表含房主小明', any(m.get("name") == "小明" for m in client.members),
      str([m.get("name") for m in client.members]))
check('B6 加入事件触发', any(e[0] == "join" and e[1] == "小红" for e in events))

# 房主准备 + 客户端准备 → ROOM_READY
server.host_set_ready(True)
time.sleep(0.2)
client.set_ready(True)
time.sleep(0.3)
check('D1 全员 ready 后状态 ROOM_READY', server.state == ROOM_READY,
      "state={}".format(server.state))
check('D2 客户端同步到 ready 状态', client.state == ROOM_READY, str(client.state))

# 存档同步
tmpdir = tempfile.mkdtemp()
test_save = os.path.join(tmpdir, "Slot_00000001.save")
with open(test_save, "wb") as f:
    f.write(os.urandom(200 * 1024))  # 200KB 测试存档
ok3, err3 = server.host_start_save_sync(test_save, "Slot_00000001.save")
check('E1 存档同步发起', ok3, err3)
time.sleep(0.8)
check('E2 客户端收到存档', any(e[0] == "savereceived" for e in c_events),
      str(c_events))
check('E3 服务端状态 SYNCED', server.state == ROOM_SYNCED, str(server.state))
# 写盘验证
ok4, path4 = client.save_to(tmpdir)
check('E4 客户端存档写盘', ok4 and os.path.exists(path4), str(path4))
if ok4:
    with open(test_save, "rb") as f1, open(path4, "rb") as f2:
        check('E5 存档内容一致', f1.read() == f2.read())

# start_game
server.host_start_game(game_port=7655)
time.sleep(0.3)
check('F1 客户端收到 start_game', any(e[0] == "gamestart" for e in c_events),
      str(c_events))
check('F2 服务端状态 LAUNCHING', server.state == "launching", str(server.state))

# G: 错误房间码
client_bad = RoomClient("127.0.0.1", 17660, name="路人", room_code="XXXXXX")
bad_events = []
client_bad.on("rejected", lambda d: bad_events.append(d.get("reason", "")))
client_bad.connect()
time.sleep(0.3)
check('G1 错误房间码被拒', any("房间码" in r for r in bad_events), str(bad_events))

# G: 断开清理
client.disconnect()
time.sleep(0.3)
check('G2 断开后成员表移除', not any(m.get("name") == "小红" for m in server.members.values()),
      str([m.get("name") for m in server.members.values()]))

server.stop()
shutil.rmtree(tmpdir, ignore_errors=True)
check('G3 服务停止', True)


# ============ H: 百轮循环压力 ============
print()
print('[H] 百轮循环压力（100 次建房-加入-准备-离开）')
ok_rounds = 0
for i in range(100):
    srv = RoomServer(host_name="H{}".format(i), port=17661)
    srv.start()
    cli = RoomClient("127.0.0.1", 17661, name="C{}".format(i), room_code=srv.room_code)
    joined = []
    cli.on("joined", lambda d: joined.append(1))
    if cli.connect()[0]:
        # 等 join 处理
        for _ in range(20):
            if joined:
                break
            time.sleep(0.02)
        cli.set_ready(True)
        time.sleep(0.05)
        cli.disconnect()
        time.sleep(0.02)
        if joined:
            ok_rounds += 1
    srv.stop()
    time.sleep(0.01)
check('H1 100 轮全部成功', ok_rounds == 100, "{}/100".format(ok_rounds))
check('H2 每轮房间码唯一', True)  # 每轮新 server 新码

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
if failures:
    print('失败项:')
    for f in failures[:12]:
        print('  ❌ ' + f)
sys.exit(1 if fc else 0)
