#!/usr/bin/env python3
"""Sims4 room_protocol v9.19 修复验证 - 网络层冒烟测试
验证: 正常 join / 重复 join 拒绝 / ready 状态流转 / 单人可自测
"""
import socket
import time
import json
import sys
import os

sys.path.insert(0, r'D:\Sims4-Multiplayer-Dev')
import room_protocol as rp

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("=== 1. 正常 join 流程 ===")
server = rp.RoomServer("房主", port=17760)
server.start()
time.sleep(0.3)

client = socket.socket()
client.settimeout(3)
client.connect(("127.0.0.1", 17760))
client.sendall(json.dumps({"type": "join", "name": "测试玩家", "room_code": server.room_code}).encode() + b"\n")
time.sleep(0.5)
data = client.recv(65536).decode()
joined = json.loads(data.split("\n")[0])
check("join 成功收到 joined", joined.get("type") == "joined", str(joined))
check("player_id 分配", joined.get("player_id", -1) >= 1)

print("=== 2. 重复 join 拒绝 ===")
client.sendall(json.dumps({"type": "join", "name": "重复玩家"}).encode() + b"\n")
time.sleep(0.5)
data = client.recv(65536).decode()
rej = json.loads(data.split("\n")[0])
check("重复 join 被拒", rej.get("type") == "join_rejected", str(rej))

print("=== 3. ready 状态流转（有客户端未 ready 时房主 ready 不够）===")
server.host_set_ready(True)
check("房主 ready 但玩家未 ready → WAITING", server.state == rp.ROOM_WAITING, server.state)
server.host_set_ready(False)
check("房主取消 ready → WAITING", server.state == rp.ROOM_WAITING, server.state)

print("=== 4. 客户端 ready + 状态广播 ===")
server.host_set_ready(True)
client.sendall(json.dumps({"type": "ready", "ready": True}).encode() + b"\n")
time.sleep(0.5)
# 读取广播的 members
members_msg = None
try:
    data = client.recv(65536).decode()
    for line in data.split("\n"):
        if line.strip():
            m = json.loads(line)
            if m.get("type") == "members":
                members_msg = m
except Exception:
    pass
check("全员 ready → READY", server.state == rp.ROOM_READY, server.state)

print("=== 5. 断连清理 ===")
client.close()
time.sleep(0.5)
check("客户端断连后 members 清理", len(server.members) == 1, str(len(server.members)))

print("=== 6. 恶意文件名（save_to 防护已在单测验证）===")
rc = rp.RoomClient("127.0.0.1")
rc._save_file = b"x"
rc._save_meta = {"filename": "../../../evil.txt", "total": 1}
import tempfile
tmp = tempfile.mkdtemp()
ok, path = rc.save_to(tmp)
check("路径遍历拦截 → 回退默认名", ok and os.path.basename(path) == "Slot_00000001.save", path)

server.stop()
print(f"\n=== 结果: {PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
