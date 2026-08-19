# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.18 - 启动器房间协议层（独立于游戏）

百轮研究（2026-08-05，47 查询）确认:
- S4MP 官方流程 = 启动器建房 → 房间码 → 加入 → 存档同步 → Start Game → 进游戏
- EOS Lobby/Session 分离: Lobby = 预游戏聚集（启动器房间），Session = 游戏内
- Nakama all_ready 模式: 全员 ready 才允许开始
- SaveSync: 先同步存档再开始游玩

架构: 房主启动器开 TCP 房间服务（默认 7660），加入者连接。
      房间阶段（准备/存档同步）全部在启动器完成，**不启动游戏**；
      点"开始游戏"后双方启动器才拉起游戏，mod 读配置自动连接（7655）。

协议（JSON 行分隔）:
  C→H: {"type":"join","name":...,"room_code":...}
  H→C: {"type":"joined","player_id":N,"members":[...],"host":...}
  C→H: {"type":"ready","ready":bool}
  H→ALL: {"type":"members","members":[...]}      # 状态广播
  H→C: {"type":"save_sync_start","filename":...,"size":N}
  H→C: {"type":"save_chunk","index":i,"total":n,"data":base64}
  H→ALL: {"type":"save_sync_done","sha256":...}
  H→ALL: {"type":"start_game","host_ip":...,"game_port":...,"save_name":...}
  C→H: {"type":"leave"}
"""
import base64
import hashlib
import hmac
import json
import os
import random
import socket
import threading
import time

ROOM_PORT = 7660
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SAVE_CHUNK_SIZE = 64 * 1024  # 64KB base64 前

# 房间状态机
ROOM_WAITING = "waiting"      # 等人加入
ROOM_READY = "ready"          # 全员准备
ROOM_SYNCING = "syncing"      # 存档同步中
ROOM_SYNCED = "synced"        # 存档同步完成
ROOM_LAUNCHING = "launching"  # 开始游戏


def gen_room_code():
    return "".join(random.choice(ROOM_CODE_ALPHABET) for _ in range(6))


def _recv_line(sock, buf):
    """从缓冲读取一行（\n 结尾），返回 (line, buf)
    v9.19: 缓冲上限 8MB→256KB（单条消息最大 86KB，256KB 足够）"""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
        if len(buf) > 256 * 1024:
            try:
                sock.close()
            except Exception:
                pass
            return None, b""
    idx = buf.index(b"\n")
    line = buf[:idx]
    buf = buf[idx + 1:]
    return line, buf


class RoomServer:
    """房主启动器内的 TCP 房间服务"""

    def __init__(self, host_name="房主", room_code=None, port=ROOM_PORT, password=""):
        self.host_name = host_name
        self.room_code = room_code or gen_room_code()
        self.port = port
        self.password = password  # v9.18: 房间密码（空=公开）
        self.state = ROOM_WAITING
        self.members = {0: {"player_id": 0, "name": host_name, "ready": False,
                            "ip": "127.0.0.1", "is_host": True}}
        self._clients = {}  # player_id -> sock
        self._client_locks = {}  # player_id -> Lock（v9.19: 防并发写 TCP 错乱）
        self._udp_sock = None  # v9.19: UDP 发现 socket（stop 时主动关）
        self._sock = None
        self._thread = None
        self._lock = threading.Lock()
        self._next_pid = 1
        self._callbacks = {}  # event -> fn(data)
        self._running = False

    # ---------------- 生命周期 ----------------
    def start(self):
        """启动 TCP 房间服务（不启动游戏）"""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.listen(8)
        self._sock.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        # v9.18: UDP 局域网发现（让加入者不用手动输 IP）
        # v9.19: socket 绑定到实例（stop 时主动关）
        self._udp_sock = _start_discovery_listener(self, self.host_name, self.room_code)
        return True

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        # v9.19: 主动关闭 UDP 发现 socket（否则后台线程要等 recvfrom 超时）
        try:
            if self._udp_sock:
                self._udp_sock.close()
        except Exception:
            pass
        for sock in list(self._clients.values()):
            try:
                sock.close()
            except Exception:
                pass
        self._clients.clear()

    def on(self, event, fn):
        self._callbacks[event] = fn

    def _emit(self, event, data=None):
        fn = self._callbacks.get(event)
        if fn:
            try:
                fn(data)
            except Exception:
                pass

    # ---------------- 连接 ----------------
    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn, addr):
        buf = b""
        try:
            # 启用 TCP keepalive（OS 层面检测死连接，约 2 小时后断开）
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # 不设 timeout——房间连接可能长时间无消息（等待用户操作），
            # 死连接由 TCP keepalive 检测。客户端主动断开时 recv 返回空自动清理。
            while self._running:
                line, buf = _recv_line(conn, buf)
                if line is None:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(conn, msg)
        except Exception:
            pass
        finally:
            # 清理断开的客户端（v9.19: 连带删除发送锁）
            with self._lock:
                for pid, sock in list(self._clients.items()):
                    if sock is conn:
                        del self._clients[pid]
                        self._client_locks.pop(pid, None)
                        self.members.pop(pid, None)
                        break
            self._broadcast_members()
            self._emit("client_left")

    def _dispatch(self, conn, msg):
        mtype = msg.get("type")
        if mtype == "join":
            # v9.19: 重复 join 拒绝（防幽灵成员）
            if self._pid_of(conn) is not None:
                self._send(conn, {"type": "join_rejected", "reason": "重复加入"})
                return
            # v9.19: 非 waiting/ready 状态拒绝加入（防中途扰乱同步）
            if self.state not in (ROOM_WAITING, ROOM_READY):
                self._send(conn, {"type": "join_rejected", "reason": "游戏已在同步或进行中"})
                return
            name = str(msg.get("name", "玩家"))[:16]
            code = str(msg.get("room_code", "")).upper()
            if code and code != self.room_code:
                self._send(conn, {"type": "join_rejected", "reason": "房间码错误"})
                return
            # v9.18: 密码验证（v9.19: hmac.compare_digest 恒定时间比对防时序攻击）
            if self.password and not hmac.compare_digest(
                    str(msg.get("password", "")), self.password):
                self._send(conn, {"type": "join_rejected", "reason": "房间密码错误"})
                return
            with self._lock:
                pid = self._next_pid
                self._next_pid += 1
                self._clients[pid] = conn
                self._client_locks[pid] = threading.Lock()
                self.members[pid] = {"player_id": pid, "name": name, "ready": False,
                                     "ip": str(msg.get("ip", "")), "is_host": False}
            self._send(conn, {"type": "joined", "player_id": pid,
                              "members": list(self.members.values()),
                              "room_code": self.room_code, "state": self.state})
            self._broadcast_members()
            self._emit("member_joined", {"player_id": pid, "name": name})
        elif mtype == "ready":
            pid = self._pid_of(conn)
            if pid is None:
                return
            with self._lock:
                if pid in self.members:
                    self.members[pid]["ready"] = bool(msg.get("ready", True))
            self._update_state()
            self._broadcast_members()
        elif mtype == "save_sync_ack":
            self._emit("save_ack")
        elif mtype == "leave":
            self._send(conn, {"type": "left"})

    def _pid_of(self, conn):
        with self._lock:
            for pid, sock in self._clients.items():
                if sock is conn:
                    return pid
        return None

    def _update_state(self):
        """全员 ready → ROOM_READY（v9.19: 房主必须 ready + 单人房间可自测）"""
        with self._lock:
            host_ready = self.members.get(0, {}).get("ready", False)
            non_host = [m for pid, m in self.members.items() if not m.get("is_host")]
            # 房主必须准备；若有其他成员，其他成员也必须全部准备
            if host_ready and (len(non_host) == 0 or all(m.get("ready") for m in non_host)):
                self.state = ROOM_READY
            else:
                self.state = ROOM_WAITING
        self._emit("state_changed", {"state": self.state})

    # ---------------- 发送 ----------------
    def _send(self, sock, msg, pid=None):
        """发送 JSON 行（v9.19: 每连接独立锁防并发写切碎 TCP 流）"""
        try:
            data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
            lock = self._client_locks.get(pid) if pid is not None else None
            if lock:
                with lock:
                    sock.sendall(data)
            else:
                sock.sendall(data)
        except Exception:
            pass

    def broadcast(self, msg, exclude=None):
        with self._lock:
            for pid, sock in list(self._clients.items()):
                if pid != exclude:
                    self._send(sock, msg, pid)

    def send_to(self, pid, msg):
        with self._lock:
            sock = self._clients.get(pid)
        if sock:
            self._send(sock, msg, pid)

    def _broadcast_members(self):
        self.broadcast({"type": "members", "members": list(self.members.values()),
                        "state": self.state})

    # ---------------- 房间操作（host 侧调用） ----------------
    def host_set_ready(self, ready=True):
        with self._lock:
            self.members[0]["ready"] = bool(ready)
        self._update_state()
        self._broadcast_members()

    def host_start_save_sync(self, filepath, filename):
        """host 把存档分块发给所有客户端"""
        if self.state not in (ROOM_READY, ROOM_SYNCING, ROOM_SYNCED):
            return False, "需全员准备后才可同步存档"
        try:
            with open(filepath, "rb") as f:
                raw = f.read()
        except Exception as e:
            return False, "读取存档失败: {}".format(e)
        sha = hashlib.sha256(raw).hexdigest()
        self.state = ROOM_SYNCING
        self._broadcast_members()
        total = (len(raw) + SAVE_CHUNK_SIZE - 1) // SAVE_CHUNK_SIZE
        self.broadcast({"type": "save_sync_start", "filename": filename,
                        "size": len(raw), "total": total, "sha256": sha})
        for i in range(total):
            chunk = raw[i * SAVE_CHUNK_SIZE:(i + 1) * SAVE_CHUNK_SIZE]
            b64 = base64.b64encode(chunk).decode("ascii")
            self.broadcast({"type": "save_chunk", "index": i, "total": total,
                            "data": b64})
            time.sleep(0.01)  # 防 TCP 拥塞
        self.broadcast({"type": "save_sync_done", "sha256": sha, "filename": filename})
        self.state = ROOM_SYNCED
        self._broadcast_members()
        self._emit("save_done", {"sha256": sha})
        return True, "存档已发送 ({} 块, SHA256 {})".format(total, sha[:8])

    def host_start_game(self, game_port=7655, save_name=""):
        """广播开始游戏 → 双方启动器拉起游戏"""
        if self.state != ROOM_SYNCED:
            # 无存档同步也允许（单人进度相同场景）
            pass
        self.state = ROOM_LAUNCHING
        self.broadcast({"type": "start_game", "host_ip": self.get_host_ip(),
                        "game_port": game_port, "save_name": save_name})
        self._broadcast_members()
        self._emit("game_started", {"host_ip": self.get_host_ip()})

    def get_host_ip(self):
        """获取局域网 IP（优先 192.168/10/172.16 网段，排除虚拟网卡——v9.18 修复）"""
        try:
            candidates = []
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                                  "172.2", "172.30.", "172.31.")):
                    return ip
                candidates.append(ip)
            if candidates:
                return candidates[0]
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


class RoomClient:
    """加入者连接房主启动器"""

    def __init__(self, host_ip, port=ROOM_PORT, name="玩家", room_code="", password=""):
        self.host_ip = host_ip
        self.port = port
        self.name = name
        self.room_code = room_code
        self.password = password  # v9.18
        self.player_id = -1
        self.members = []
        self.state = ROOM_WAITING
        self.connected = False
        self._sock = None
        self._thread = None
        self._callbacks = {}
        self._recv_buf = b""
        self._save_file = None
        self._save_chunks = {}
        self._running = False

    def on(self, event, fn):
        self._callbacks[event] = fn

    def _emit(self, event, data=None):
        fn = self._callbacks.get(event)
        if fn:
            try:
                fn(data)
            except Exception:
                pass

    # ---------------- 连接 ----------------
    def connect(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(8.0)
            self._sock.connect((self.host_ip, self.port))
            self._sock.settimeout(None)
            self.connected = True
            self._running = True
            self._thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._thread.start()
            # 发 join
            msg = {"type": "join", "name": self.name, "room_code": self.room_code,
                   "ip": self._local_ip()}
            if self.password:
                msg["password"] = self.password
            self._send(msg)
            return True, "已连接"
        except Exception as e:
            return False, "连接失败: {}".format(e)

    def disconnect(self):
        self._running = False
        try:
            if self._sock:
                self._send({"type": "leave"})
                self._sock.close()
        except Exception:
            pass
        self.connected = False

    def _local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return ""

    def _send(self, msg):
        try:
            self._sock.sendall(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception:
            pass

    def _recv_loop(self):
        while self._running:
            try:
                line, self._recv_buf = _recv_line(self._sock, self._recv_buf)
                if line is None:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(msg, dict):
                    self._handle(msg)
            except Exception:
                break
        self.connected = False
        self._emit("disconnected")

    def _handle(self, msg):
        mtype = msg.get("type")
        if mtype == "joined":
            self.player_id = int(msg.get("player_id", 1))
            self.members = msg.get("members", [])
            self.state = msg.get("state", ROOM_WAITING)
            self._emit("joined", msg)
        elif mtype == "join_rejected":
            self._emit("rejected", {"reason": msg.get("reason", "")})
        elif mtype == "members":
            self.members = msg.get("members", [])
            self.state = msg.get("state", self.state)
            self._emit("members", msg)
        elif mtype == "save_sync_start":
            self._save_chunks = {}
            self._save_meta = {"filename": msg.get("filename", "save"), "total": int(msg.get("total", 0)),
                               "sha256": msg.get("sha256", ""), "size": int(msg.get("size", 0))}
            self._emit("save_start", msg)
        elif mtype == "save_chunk":
            idx = int(msg.get("index", -1))
            data = msg.get("data", "")
            total = int(msg.get("total", 0))
            # v9.19: 边界校验（防非法 index 导致提前组装/错位）
            if 0 <= idx < total:
                try:
                    self._save_chunks[idx] = base64.b64decode(data)
                except Exception:
                    pass
            if total > 0 and len(self._save_chunks) == total:
                if set(self._save_chunks.keys()) == set(range(total)):
                    self._assemble_save()
        elif mtype == "save_sync_done":
            self._emit("save_done", msg)
        elif mtype == "start_game":
            self._emit("game_start", msg)
        elif mtype == "left":
            self.connected = False

    def _assemble_save(self):
        meta = getattr(self, "_save_meta", {})
        total = meta.get("total", 0)
        raw = b"".join(self._save_chunks[i] for i in sorted(self._save_chunks.keys()) if i in self._save_chunks)
        sha = hashlib.sha256(raw).hexdigest()
        if meta.get("sha256") and sha != meta.get("sha256"):
            self._emit("save_error", {"reason": "SHA256 校验失败"})
            return
        self._save_file = raw
        self._emit("save_received", {"filename": meta.get("filename", "save"),
                                     "size": len(raw), "sha256": sha})

    # ---------------- 客户端操作 ----------------
    def set_ready(self, ready=True):
        self._send({"type": "ready", "ready": ready})

    def save_to(self, saves_dir):
        """把收到的存档写入 Saves 目录（v9.19: 防路径遍历——只取 basename + .save 白名单）"""
        if self._save_file is None:
            return False, "无存档数据"
        try:
            os.makedirs(saves_dir, exist_ok=True)
            raw_name = getattr(self, "_save_meta", {}).get("filename", "")
            safe_name = os.path.basename(raw_name.replace("\\", "/"))
            if not safe_name.endswith(".save") or ".." in safe_name:
                safe_name = "Slot_00000001.save"
            path = os.path.join(saves_dir, safe_name)
            if os.path.exists(path):
                os.replace(path, path + ".bak")
            with open(path, "wb") as f:
                f.write(self._save_file)
            return True, path
        except Exception as e:
            return False, str(e)


# ============ v9.18: 局域网发现（UDP 广播） ============
DISCOVERY_PORT = 7661
DISCOVERY_MAGIC = b"SIMSYNC_DISCOVER"


def discover_rooms(timeout=2.0):
    """广播发现局域网内的房间，返回列表 [{name, room_code, ip, players}]"""
    import json as _json
    results = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.sendto(DISCOVERY_MAGIC, ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
                info = _json.loads(data.decode("utf-8"))
                info["ip"] = addr[0]
                results.append(info)
            except socket.timeout:
                break
            except Exception:
                continue
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return results


def _start_discovery_listener(room_server, host_name, room_code, port=DISCOVERY_PORT):
    """在后台线程响应局域网发现请求（UDP）"""
    import json as _json
    sock = None

    def _listen():
        nonlocal sock
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(3.0)
            while getattr(room_server, "_running", False):
                try:
                    data, addr = sock.recvfrom(1024)
                    if data == DISCOVERY_MAGIC:
                        info = _json.dumps({
                            "name": host_name,
                            "room_code": room_code,
                            "players": len(room_server.members),
                            "has_password": bool(room_server.password),
                        }, ensure_ascii=False)
                        sock.sendto(info.encode("utf-8"), addr)
                except socket.timeout:
                    continue
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    return sock
