# -*- coding: utf-8 -*-
"""M3d 增强虚拟测试：房间可见性 + 存档传输 + 主机迁移

验证:
A. 私密房间密码验证
B. 存档 base64 分块发送/接收/写入
C. 主机迁移竞选（player_id 最小者胜出）
"""
import sys
import os
import json
import time

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from multimod import network

sent = []
network._log = lambda msg: print('  [log] ' + str(msg))
network._notify = lambda msg: print('  [notify] ' + str(msg))
network._is_host = True
network._my_player_id = 0
network._client_socket = object()
network._clients = {1: (object(), ('1.2.3.4', 1))}
network._broadcast = lambda payload, exclude=None: sent.append(payload)
network._send_json = lambda sock, payload: sent.append(payload)
network._network_thread = None

from multimod import lobby

pc = fc = 0
def check(name, cond):
    global pc, fc
    if cond: pc += 1; print('  ✅ ' + name)
    else: fc += 1; print('  ❌ ' + name)

# ---- A: 私密房间密码 ----
print('[A] 私密房间密码')
lobby.add_host_self(visibility='private', password='1234')
check('可见性=private', lobby.ROOM_VISIBILITY == 'private')
check('密码已存', lobby.ROOM_PASSWORD == '1234')
lobby.process_message({"type": "hello", "name": "偷渡客", "password": "wrong"}, sender_pid=1)
check('错误密码被拒（不入列表）', 1 not in lobby._members)
lobby.process_message({"type": "hello", "name": "好友", "password": "1234"}, sender_pid=1)
check('正确密码加入', 1 in lobby._members)
check('广播含可见性', any('room_visibility' in p for p in sent))

# ---- B: 存档传输 ----
print()
print('[B] 存档自动传输')
os.makedirs(lobby.SAVES_DIR, exist_ok=True)
test_save = os.path.join(lobby.SAVES_DIR, 'Slot_00000001.save')
with open(test_save, 'wb') as f:
    f.write(os.urandom(500 * 1024))  # 500KB 测试数据
ok, desc = lobby.host_send_save_file('Slot_00000001.save')
check('发送成功', ok)
chunks = [p for p in sent if p.get('type') == 'save_chunk']
check('分块数>1', len(chunks) > 1)
check('所有块有 index/total', all('index' in c and 'total' in c for c in chunks))
# 模拟客户端接收
lobby._is_host_backup = network._is_host
network._is_host = False
for c in chunks:
    lobby.on_save_chunk(c)
lobby.on_save_chunk_done({"type": "save_chunk_done", "filename": "Slot_00000001.save", "total_bytes": 500*1024})
dest = os.path.join(lobby.SAVES_DIR, 'Slot_00000001.save')
check('接收端文件写入', os.path.exists(dest))
import hashlib
check('文件内容一致', hashlib.md5(open(test_save,'rb').read()).hexdigest() ==
      hashlib.md5(open(dest,'rb').read()).hexdigest())
# 还原 host 状态（避免影响 C）
network._is_host = True

# ---- C: 主机迁移 ----
print()
print('[C] 主机迁移')
# 模拟客户端 A(pid=1) 和 B(pid=2)，竞选：pid=1 胜出
network._is_host = False
network._my_player_id = 1
lobby._members = {
    1: {'player_id': 1, 'name': 'A', 'online': True, 'is_host': False},
    2: {'player_id': 2, 'name': 'B', 'online': True, 'is_host': False},
}
lobby.set_host_ip('192.168.0.112')
lobby.on_host_disconnect()
check('竞选 claim 文件写入', os.path.exists(lobby.MIGRATION_CLAIM_PATH))
time.sleep(lobby.MIGRATION_SETTLE_SEC + 0.5)  # 等结算
check('pid=1 成为新房主', network._is_host == True)
check('新房主 pid=0', network._my_player_id == 0)
check('房间重建含自己', 0 in lobby._members and lobby._members[0]['is_host'])

print()
print('结果: {} 通过, {} 失败'.format(pc, fc))
sys.exit(1 if fc else 0)
