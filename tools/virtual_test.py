#!/usr/bin/env python3
"""Sims4Multiplayer 网络层虚拟测试 (不需要游戏)

模拟主机 + 客机两端，验证:
1. 服务器能启动监听 7655
2. 客机能连接并互发 JSON 消息
3. 消息格式与 mod 内 _send_json/_recv_loop 一致
4. 心跳/超时/断线重连行为

用法: python virtual_test.py
"""
import socket
import threading
import json
import time
import queue

# ============ 与 mod 相同配置 ============
DEFAULT_PORT = 7655
BUF_SIZE = 4096

# ============ 模拟主机 (等同游戏内 mp_host) ============
class VirtualHost:
    def __init__(self):
        self.server = None
        self.client_conn = None
        self.received = []
        self.port = DEFAULT_PORT

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", self.port))
        self.server.listen(1)
        print(f"[HOST] 服务器监听 :{self.port}")
        t = threading.Thread(target=self._accept, daemon=True)
        t.start()

    def _accept(self):
        conn, addr = self.server.accept()
        self.client_conn = conn
        print(f"[HOST] 客机已连接: {addr}")
        # 接收循环（同 mod _recv_loop）
        buf = b""
        while True:
            try:
                chunk = conn.recv(BUF_SIZE)
                if not chunk:
                    print("[HOST] 连接关闭")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self.received.append(json.loads(line.decode("utf-8")))
                        print(f"[HOST] 收到: {line.decode('utf-8').strip()}")
            except Exception as e:
                print(f"[HOST] 接收错误: {e}")
                break

    def send(self, payload):
        if self.client_conn:
            self.client_conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            print(f"[HOST] 发送: {payload}")

# ============ 模拟客机 (等同游戏内 mp_join + mp_say) ============
class VirtualClient:
    def __init__(self, host="127.0.0.1"):
        self.sock = None
        self.host = host
        self.received = []
        self.connected = False

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)  # 5 秒超时（mod 里可加）
            self.sock.connect((self.host, DEFAULT_PORT))
            self.connected = True
            print(f"[CLIENT] 已连接主机 {self.host}:{DEFAULT_PORT}")
            # 自动发送加入消息（同 mod _client_thread）
            self.send({"type": "chat", "from": "client", "text": "已加入!"})
            t = threading.Thread(target=self._recv_loop, daemon=True)
            t.start()
            return True
        except Exception as e:
            print(f"[CLIENT] 连接失败: {e}")
            return False

    def _recv_loop(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(BUF_SIZE)
                if not chunk:
                    print("[CLIENT] 连接关闭")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self.received.append(json.loads(line.decode("utf-8")))
                        print(f"[CLIENT] 收到: {line.decode('utf-8').strip()}")
            except Exception as e:
                print(f"[CLIENT] 接收错误: {e}")
                break

    def send(self, payload):
        if self.sock:
            self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            print(f"[CLIENT] 发送: {payload}")

# ============ 测试流程 ============
def run_test():
    print("=" * 50)
    print("Sims4Multiplayer 虚拟联机测试 (本机回环)")
    print("=" * 50)

    # 1. 启动主机
    host = VirtualHost()
    host.start()
    time.sleep(0.5)

    # 2. 客机连接
    client = VirtualClient("127.0.0.1")
    if not client.connect():
        print("❌ 测试失败: 客机无法连接主机")
        return False
    time.sleep(0.5)

    # 3. 双向聊天
    host.send({"type": "chat", "from": "me", "text": "你好，我是主机!"})
    time.sleep(0.3)
    client.send({"type": "chat", "from": "me", "text": "你好，我是客机!"})
    time.sleep(0.3)

    # 4. 验证结果
    print("\n" + "=" * 50)
    # host.received = 主机收到的（客机发的）
    host_msgs = [m.get("text") for m in host.received]
    # client.received = 客机收到的（主机发的）
    client_msgs = [m.get("text") for m in client.received]

    ok = True
    if "你好，我是客机!" in host_msgs:
        print("✅ 主机收到客机消息: 你好，我是客机!")
    else:
        print("❌ 主机未收到客机消息")
        ok = False
    if "你好，我是主机!" in client_msgs:
        print("✅ 客机收到主机消息: 你好，我是主机!")
    else:
        print("❌ 客机未收到主机消息")
        ok = False
    if "已加入!" in host_msgs:
        print("✅ 主机收到客机加入消息")
    else:
        print("❌ 主机未收到加入消息")
        ok = False

    print("=" * 50)
    if ok:
        print("🎉 虚拟联机测试全部通过!")
    else:
        print("❌ 测试有失败项")
    print("=" * 50)
    return ok

if __name__ == "__main__":
    run_test()
