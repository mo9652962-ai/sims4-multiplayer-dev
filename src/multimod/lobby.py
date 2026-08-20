# -*- coding: utf-8 -*-
"""Sims4Multiplayer M3d - 房间系统（Lobby，对齐 S4MP 多人联机）

功能（用户需求 2026-08-04）:
1. 房间列表：房主创建房间后在列表；加入者看到自己 + 所有成员
2. 准备机制：房主等所有人准备就绪才能进入下一步
3. 存档同步：非房主全准备 → 房主可同步存档 → 同步完成所有人有"开始游戏"
4. 进图门槛：房主进家庭地段时若有人未进 → 通知等人齐，不能开始
5. 时间由房主操控（M3c 已实现）

协议（JSON over TCP）:
- 客户端→主机: hello {name}              连接握手（带玩家名）
- 主机→客户端: welcome {player_id}       分配 ID
- 主机→所有:  lobby {members:[...]}       房间状态广播（成员/准备/进图）
- 客户端→主机: ready {ready: bool}       准备/取消
- 客户端→主机: in_lot {in_lot: bool}     报告进入/离开家庭地段
- 主机→客户端: save_sync_req             请求确认存档同步
- 客户端→主机: save_sync_ack {ok}        确认
- 主机→所有:  save_sync_done             存档同步完成（所有人可开始）
- 主机→所有:  start_game                 开始游戏（全部进地段）

成员状态: {player_id, name, ip, ready, in_lot, is_host}
"""

import json
import os
import random  # v9.4: 重连退避 jitter（研究: 防 thundering herd）
import socket
import time
import threading
import hmac as _hmac_mod  # v9.25: 密码时序安全比对

import sims4.commands

from multimod import network

# ============ 配置 ============
# 房间状态文件（启动器 GUI 轮询读取显示房间列表）
LOBBY_STATE_PATH = network.LOBBY_STATE_PATH

# ============ 全局状态 ============
# members: {player_id: {name, ip, ready, in_lot, is_host, online, last_seen}}
_members = {}
_save_sync_phase = "idle"   # idle | waiting_ack | done
_state_lock = threading.Lock()  # v9.11: state 文件写锁（并发写安全）
_save_sync_acks = set()
_start_granted = False       # 存档同步完成后所有成员获得开始权
_player_name = "玩家"
_last_broadcast_state = None
_reconnect_in_progress = False  # v9.20.4: 重连循环防重入（断线风暴时避免循环叠加）

# M3d 增强（十轮研究 2026-08-04）:
# - 房间码: 房主建房生成 6 位短码，加入者输码免输 IP
# - 心跳: 每 5s 客户端 ping，host 记录 last_seen，>15s 标记离线
# - 离开/踢人/转交: 房主权限
ROOM_CODE = ""
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去易混淆字符
HEARTBEAT_INTERVAL = 5.0    # 客户端心跳间隔（秒）
HEARTBEAT_TIMEOUT = 15.0    # 超过该时间未收到心跳 → 标记离线
_last_heartbeat_ts = 0

# 房间可见性（十轮研究: 公开/好友/私密 - flackr/lobby 模式）
ROOM_VISIBILITY = "public"   # public | private（好友模式简化合并到 private 用 IP 限制）

# v9.0: 聊天历史（最近 20 条，启动器聊天页读取；研究: 状态文件轮询模式）
_chat_history = []


def add_chat(from_name, text):
    """追加聊天记录（网络收到或本机发送时调用）"""
    global _chat_history
    try:
        _chat_history.append({"from": from_name, "text": str(text)[:200],
                              "ts": time.strftime("%H:%M:%S")})
        if len(_chat_history) > 20:
            _chat_history = _chat_history[-20:]
        _write_state_file()
    except Exception:
        pass
ROOM_PASSWORD = ""           # private 房间的加入密码（4-8 位）
MAX_PLAYERS = 8              # v9.25: 房间人数上限（防恶意握手内存膨胀）


def _gen_room_code():
    """生成 6 位房间码（去易混淆字符 I/O/0/1）"""
    import random
    return "".join(random.choice(ROOM_CODE_ALPHABET) for _ in range(6))


def _get_player_name():
    global _player_name
    # v9.16: 优先读启动器配置的玩家名（用户自定义昵称）——没有才用电脑名
    try:
        cfg = network._load_launcher_config()
        if cfg and cfg.get("name"):
            _player_name = str(cfg["name"])[:16]
            return _player_name
    except Exception:
        pass
    try:
        import socket
        _player_name = socket.gethostname()
    except Exception:
        pass
    return _player_name


def _collect_local_addrs():
    """收集本机所有局域网地址（IPv4 + IPv6），借鉴 S4MP room_code 多地址格式。

    S4MP 配置: room_code="local/host/192.168.0.112;fe80::bad0:...;..."
    —— host 把 IPv4+IPv6 全列出来，方便客机在复杂网络（双栈/多网卡）下找到可达地址。
    返回 "ip1;ip2;..." 字符串（空则空串）。
    """
    try:
        import socket as _s
        addrs = set()
        try:
            hostname = _s.gethostname()
            for info in _s.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ":" not in ip:
                    addrs.add(ip)
        except Exception:
            pass
        # UDP 探测法拿主网卡 IP（不真正发包）
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addrs.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        return ";".join(sorted(addrs))
    except Exception:
        return ""


def _connection_health():
    """v9.23: 当前连接健康摘要（RTT + 质量评级，None=未测量）"""
    try:
        rtt = network.get_rtt_ms()
        return {"rtt_ms": (round(rtt, 1) if rtt is not None else None),
                "label": network.get_health_label()}
    except Exception:
        return {"rtt_ms": None, "label": "未测量"}


def _write_state_file():
    """写房间状态到文件（启动器 GUI 轮询读取）

    v9.11: 加线程锁 + 原子写（临时文件→rename）——
    多线程并发 open("w") 偶发 PermissionError（Windows 文件锁），
    会导致聊天/成员状态写入丢失（v92 百次测试发现 1/6 偶发失败）
    """
    try:
        state = {
            "ts": time.time(),
            "is_host": network._is_host,
            "my_player_id": network._my_player_id,
            "members": list(_members.values()),
            "save_sync_phase": _save_sync_phase,
            "start_granted": _start_granted,
            "room_code": ROOM_CODE,
            "room_visibility": ROOM_VISIBILITY,
            # v9.20: 本机多地址（IPv4+IPv6 分号分隔，借鉴 S4MP room_code 格式）
            "room_addr": _collect_local_addrs(),
            "chat": _chat_history,  # v9.0: 聊天历史（启动器聊天页读取）
            # v9.23: 连接健康（RTT + 质量评级，启动器房间页显示）
            "health": _connection_health(),
            # v9.22: 旅行状态板（启动器房间页显示谁已抵达/待抵达）
            "travel": _travel_board if _travel_board is not None else {
                "active": _travel_active,
                "auto_ack": _travel_auto_ack,
                "follow": _follow_travel,
                "arrived": [m.get("name", pid) for pid, m in _members.items()
                            if m.get("player_id", pid) in _travel_arrived],
                "pending": [m.get("name", pid) for pid, m in _members.items()
                            if m.get("player_id", pid) not in _travel_arrived],
            },
        }
        # v7.0: 加入者附加局域网发现的房间（启动器"发现房间"功能）
        if not network._is_host:
            try:
                state["discovered_rooms"] = network.get_discovered_rooms()
            except Exception:
                pass
        with _state_lock:
            tmp = LOBBY_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, LOBBY_STATE_PATH)
            except OSError:
                # 旧系统无 os.replace → 回退直接写
                os.remove(LOBBY_STATE_PATH)
                os.rename(tmp, LOBBY_STATE_PATH)
    except Exception as e:
        network._log("lobby state write error: {}".format(e))


def _broadcast_state():
    """广播房间状态（主机 → 所有客户端）"""
    global _last_broadcast_state
    try:
        payload = {"type": "lobby", "members": list(_members.values()),
                   "save_sync_phase": _save_sync_phase, "start_granted": _start_granted,
                   "room_code": ROOM_CODE, "room_visibility": ROOM_VISIBILITY,
                   # v9.22: 旅行状态板随房间状态广播（客机启动器同步显示）
                   "travel": {"active": _travel_active, "auto_ack": _travel_auto_ack,
                              "follow": _follow_travel,
                              "arrived": [m.get("name", pid) for pid, m in _members.items()
                                          if m.get("player_id", pid) in _travel_arrived],
                              "pending": [m.get("name", pid) for pid, m in _members.items()
                                          if m.get("player_id", pid) not in _travel_arrived]}}
        if network._is_host:
            network._broadcast(payload)
        else:
            if network._client_socket is not None:
                network._send_json(network._client_socket, payload)
        _write_state_file()
    except Exception as e:
        network._log("lobby broadcast error: {}".format(e))


def _notify(text):
    network._notify(text)


# ============ 主机侧事件 ============
def on_client_connected(player_id, addr):
    """新客户端 TCP 连接（握手完成，等 hello）"""
    network._log("lobby: client {} connected, waiting hello".format(player_id))


def on_client_disconnect(player_id):
    """客户端断开 → 移除成员 + 广播"""
    global _members
    if player_id in _members:
        name = _members[player_id].get("name", "?")
        del _members[player_id]
        network._log("lobby: {} left the room".format(name))
        _notify("{} 离开了房间".format(name))
        _broadcast_state()


def add_host_self(visibility="public", password=""):
    """房主把自己加入房间列表 + 生成房间码 + 设置可见性（mp_host 时调用）"""
    global _members, ROOM_CODE, ROOM_VISIBILITY, ROOM_PASSWORD
    ROOM_CODE = _gen_room_code()
    ROOM_VISIBILITY = visibility if visibility in ("public", "private") else "public"
    ROOM_PASSWORD = password if ROOM_VISIBILITY == "private" else ""
    _members[0] = {
        "player_id": 0,
        "name": _get_player_name(),
        "ip": "127.0.0.1",
        "ready": False,
        "in_lot": False,
        "is_host": True,
        "online": True,
        "last_seen": time.time(),
    }
    _write_state_file()
    network._log("lobby: host {} created room, code={}, visibility={}".format(
        _members[0]["name"], ROOM_CODE, ROOM_VISIBILITY))


def on_hello(player_id, name, ip):
    """客户端发来 hello → 加入成员列表 + 广播"""
    global _members
    # v9.25: 房间人数上限（防恶意握手内存膨胀/广播风暴）
    if player_id not in _members and len(_members) >= MAX_PLAYERS:
        network._log("lobby: room full, rejected {}".format(player_id))
        if network._client_socket is not None:
            network._send_json(network._client_socket,
                               {"type": "join_rejected", "reason": "room_full"})
        return
    # v9.25: 昵称清洗（长度 16 + 去控制字符——防刷屏/富文本混淆）
    clean_name = (name or "").strip()[:16]
    clean_name = "".join(c for c in clean_name if c.isprintable() and ord(c) >= 32)
    _members[player_id] = {
        "player_id": player_id,
        "name": clean_name or "玩家{}".format(player_id),
        "ip": ip or "",
        "ready": False,
        "in_lot": False,
        "is_host": False,
        "online": True,
        "last_seen": time.time(),
    }
    network._log("lobby: {} joined room".format(_members[player_id]["name"]))
    _notify("{} 加入了房间".format(_members[player_id]["name"]))
    _broadcast_state()


# ============ M3d 增强: 心跳检测 ============
def on_heartbeat(player_id):
    """收到客户端心跳 → 更新 last_seen + 恢复 online"""
    if player_id in _members:
        _members[player_id]["last_seen"] = time.time()
        if not _members[player_id].get("online"):
            _members[player_id]["online"] = True
            network._log("lobby: {} back online".format(_members[player_id]["name"]))
            _broadcast_state()


def check_heartbeats():
    """主机每 5s 检查成员心跳超时 → 标记离线（alarm 调用）"""
    global _members
    if not network._is_host:
        return
    now = time.time()
    changed = False
    for pid, m in list(_members.items()):
        if m.get("is_host"):
            continue
        if now - m.get("last_seen", now) > HEARTBEAT_TIMEOUT:
            if m.get("online"):
                m["online"] = False
                network._log("lobby: {} offline (heartbeat timeout)".format(m["name"]))
                _notify("{} 掉线了".format(m["name"]))
                changed = True
    if changed:
        _broadcast_state()


def start_heartbeat():
    """客户端开始心跳（每 5s 发 ping 给主机）"""
    global _last_heartbeat_ts
    if network._is_host:
        return
    def _beat():
        global _last_heartbeat_ts
        while True:
            try:
                if network._client_socket is not None:
                    network._send_json(network._client_socket, {"type": "heartbeat"})
                _last_heartbeat_ts = time.time()
            except Exception:
                pass
            time.sleep(HEARTBEAT_INTERVAL)
    import threading
    threading.Thread(target=_beat, daemon=True).start()
    network._log("lobby: heartbeat started")


# ============ M3d 增强: 存档自动传输（SimSync 模式） ============
# 房主选择存档文件 → base64 分块发送 → 成员自动写入 Saves 目录
# v7.0: 块大小 64KB（研究: Python TCP 64KB 高效分块）→ base64 后 ~89KB/包
SAVES_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                         "The Sims 4", "Saves")
SAVE_CHUNK_SIZE = 64 * 1024  # 64KB 每块（研究推荐值）


def _pick_latest_save():
    """v9.20.1: 选 Saves 目录最新 .save 文件（排除 .bak/备份）"""
    try:
        os.makedirs(SAVES_DIR, exist_ok=True)
        saves = [f for f in os.listdir(SAVES_DIR)
                 if f.endswith(".save") and not f.endswith(".bak")]
        if not saves:
            network._log("lobby: no save files in {}".format(SAVES_DIR))
            return None
        saves.sort(key=lambda f: os.path.getmtime(os.path.join(SAVES_DIR, f)), reverse=True)
        return saves[0]
    except Exception as e:
        network._log("lobby pick save error: {}".format(e))
        return None


def host_send_save_file(filename):
    """房主发送存档文件给所有客户端（base64 分块）

    filename: Saves 目录下的文件名，如 Slot_00000001.save
    返回: (bool ok, str desc)
    """
    try:
        import base64
        import hashlib
        path = os.path.join(SAVES_DIR, filename)
        if not os.path.exists(path):
            return False, "存档不存在: {}".format(path)
        with open(path, "rb") as f:
            raw = f.read()
        # v9.4: 整体 SHA256（研究: S3 per-part checksum——防网络损坏静默入库）
        file_sha = hashlib.sha256(raw).hexdigest()
        b64 = base64.b64encode(raw).decode("ascii")
        total_chunks = (len(b64) + SAVE_CHUNK_SIZE - 1) // SAVE_CHUNK_SIZE
        network._log("lobby: sending save {} ({}B, {} chunks, sha={})".format(
            filename, len(raw), total_chunks, file_sha[:12]))
        _notify("正在发送存档 {} ({:.1f}KB)...".format(filename, len(raw) / 1024))
        for i in range(total_chunks):
            chunk = b64[i * SAVE_CHUNK_SIZE:(i + 1) * SAVE_CHUNK_SIZE]
            network._broadcast({
                "type": "save_chunk",
                "filename": filename,
                "index": i,
                "total": total_chunks,
                "data": chunk,
                "sha256": file_sha,  # v9.4: 接收端校验
            })
        network._broadcast({
            "type": "save_chunk_done",
            "filename": filename,
            "total_bytes": len(raw),
            "sha256": file_sha,
        })
        _notify("存档发送完成!")
        network._log("lobby: save send complete {}".format(filename))
        return True, "已发送 {}".format(filename)
    except Exception as e:
        network._log("lobby send save error: {}".format(e))
        return False, str(e)


# 客户端接收缓存: filename -> {chunks: {}, total, bytes}
_recv_save_cache = {}


def _safe_save_filename(filename):
    """v9.25: 存档文件名安全校验（防路径穿越——../../ 任意文件写入 RCE）"""
    if not filename:
        return None
    name = os.path.basename(str(filename).replace("\\", "/"))
    if not (name.endswith(".save") or name.endswith(".save.bak")):
        network._log("lobby: invalid save filename rejected: {}".format(name))
        return None
    return name


def _resend_save_chunks(filename, missing_indices):
    """v9.5: 房主重发缺失存档块（研究: MAVLink FTP missing chunks re-request）"""
    try:
        import base64
        filename = _safe_save_filename(filename)
        if filename is None:
            return
        path = os.path.join(SAVES_DIR, filename)
        if not os.path.exists(path):
            network._log("lobby: resend failed, save missing {}".format(path))
            return
        with open(path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        total_chunks = (len(b64) + SAVE_CHUNK_SIZE - 1) // SAVE_CHUNK_SIZE
        for i in missing_indices:
            if 0 <= i < total_chunks:
                chunk = b64[i * SAVE_CHUNK_SIZE:(i + 1) * SAVE_CHUNK_SIZE]
                network._broadcast({
                    "type": "save_chunk", "filename": filename,
                    "index": i, "total": total_chunks, "data": chunk,
                })
        network._log("lobby: resent {} missing chunks of {}".format(len(missing_indices), filename))
    except Exception as e:
        network._log("lobby resend error: {}".format(e))


def on_save_chunk(data):
    """客户端收到存档块 → 缓存"""
    global _recv_save_cache
    filename = _safe_save_filename(data.get("filename", ""))
    if filename is None:
        return
    index = data.get("index", 0)
    total = data.get("total", 1)
    chunk = data.get("data", "")
    if filename not in _recv_save_cache:
        _recv_save_cache[filename] = {"chunks": {}, "total": total}
    _recv_save_cache[filename]["chunks"][index] = chunk
    _recv_save_cache[filename]["total"] = total
    # 进度提示（每 25%）
    got = len(_recv_save_cache[filename]["chunks"])
    if total > 0 and got % max(1, total // 4) == 0:
        network._log("lobby: save recv {}/{} chunks".format(got, total))


def on_save_chunk_done(data):
    """客户端收完所有块 → 拼装 + SHA256 校验 + 写入 Saves

    v9.5: 缺块时请求重传缺失块（研究: MAVLink FTP missing chunks re-request），
    而非直接失败——网络丢包可自动恢复
    """
    global _recv_save_cache
    try:
        import base64
        import hashlib
        filename = _safe_save_filename(data.get("filename", ""))
        if filename is None:
            return
        if filename not in _recv_save_cache:
            return
        cache = _recv_save_cache[filename]
        total = cache["total"]
        chunks = cache["chunks"]
        if len(chunks) < total:
            missing = [i for i in range(total) if i not in chunks]
            network._log("lobby: save recv missing chunks {} ({}/{})".format(
                missing[:5], len(chunks), total))
            # v9.5: 请求重传缺失块（最多 3 次，防无限循环）
            _recv_save_cache[filename]["retries"] = cache.get("retries", 0) + 1
            if cache.get("retries", 0) < 3:
                _notify("存档块缺失，正在请求重传 ({} 块)...".format(len(missing)))
                if network._client_socket is not None:
                    network._send_json(network._client_socket, {
                        "type": "save_resend_req", "filename": filename,
                        "missing": missing[:20], "total": total})
                return
            else:
                _notify("存档接收仍不完整，请房主重新发送")
                _recv_save_cache.pop(filename, None)
                return
        # v9.4: 校验块连续性（防丢块错位）
        if sorted(chunks.keys()) != list(range(total)):
            network._log("lobby: save recv chunk gap! {} != 0..{}".format(
                sorted(chunks.keys()), total - 1))
            _notify("存档块缺失，请房主重新发送")
            return
        b64 = "".join(chunks[i] for i in sorted(chunks.keys()))
        raw = base64.b64decode(b64)
        # v9.4: SHA256 完整性校验（研究: S3 checksum verify——防网络损坏静默入库）
        expect_sha = data.get("sha256", "")
        actual = ""
        if expect_sha:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expect_sha:
                network._log("lobby: save SHA mismatch! {} != {}".format(
                    actual[:12], expect_sha[:12]))
                _notify("存档校验失败（可能损坏），请房主重新发送")
                return
        # 写入 Saves 目录（备份旧文件）
        os.makedirs(SAVES_DIR, exist_ok=True)
        dest = os.path.join(SAVES_DIR, filename)
        if os.path.exists(dest):
            os.replace(dest, dest + ".bak")
        with open(dest, "wb") as f:
            f.write(raw)
        _recv_save_cache.pop(filename, None)
        _notify("存档已接收并写入: {} ({:.1f}KB, 校验✅)".format(filename, len(raw) / 1024))
        network._log("lobby: save received and written {} ({}B, sha={})".format(
            filename, len(raw), actual[:12] if expect_sha else "n/a"))
        # v9.20.1: 提示重新加载——游戏运行中无法热切换存档，
        # 文件已就位但必须回主菜单读档才能生效（修复"同步了但游戏里没变"的另一半）
        _notify("存档已同步! 请返回主菜单后重新加载存档 {}".format(filename))
        # 同步完成
        if network._client_socket is not None:
            network._send_json(network._client_socket, {"type": "save_sync_ack", "ok": True, "filename": filename})
    except Exception as e:
        network._log("lobby save write error: {}".format(e))
        _notify("存档写入失败: {}".format(e))


# ============ M3d 增强: 主机迁移（Unity EOS 5s 机制，简化文件协调版） ============
# 房主掉线 → 所有客户端检测到断开 → 写 claim 文件（player_id + 时间戳）
# → 3s 后读文件：player_id 最小者成为新房主 → 开始监听 → 其他客户端重连
MIGRATION_CLAIM_PATH = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                                    "The Sims 4", "Mods", "mp_migration_claim.json")
MIGRATION_SETTLE_SEC = 3.0   # 竞选等待时间（Unity 建议 5s，我们简化 3s）
_last_known_host_ip = ""


def set_host_ip(ip):
    """记录主机 IP（客户端用于迁移重连）"""
    global _last_known_host_ip
    _last_known_host_ip = ip or ""


def schedule_reconnect_with_backoff(broken_sock=None):
    """v7.0: 断线自动重连（指数退避 1.5s→60s，最多 20 次）

    先尝试重连原主机（可能是临时断线）；失败达到上限后才触发主机迁移竞选

    v9.19.1: broken_sock 参数——记录触发断线的 socket。醒来时若
    _client_socket 已被外部替换（手动重连/新连接），说明连接已恢复，
    本调度必须退出，绝不能 close 新连接（旧 bug：cli1 断线触发的
    调度 1.5s 后醒来误杀 cli2 的新连接）。
    """
    global _last_known_host_ip, _reconnect_in_progress
    # v9.20.4: 防重入——断线风暴时 _recv_loop 每次断线都调度新循环,
    # 旧循环未结束又起新循环 → attempts 永远 1/20, 指数退避从未累计。
    # 已有循环在跑则跳过 (循环内部会持续重试直到成功/耗尽)。
    if _reconnect_in_progress:
        network._log("reconnect: loop already running, skip")
        return
    _reconnect_in_progress = True
    try:
        import threading
        from multimod import network as net

        def _reconnect_loop():
            global _reconnect_in_progress
            try:
                delay = 1.5
                attempts = 0
                while attempts < 20:
                    time.sleep(delay)
                    # v9.19.1: socket 已被替换 → 已恢复，退出（防止误杀新连接）
                    # v9.20.4: 修复回归——断线后 _client_socket 被清理为 None,
                    # None is not broken_sock 恒为 True → 首次检查就误判"已替换"而退出,
                    # 导致客机断线后永不重连(离线收不到主机广播)。只有 _client_socket
                    # 是"有效的新连接"(非 None 且非断线 socket) 才允许退出。
                    if (broken_sock is not None
                            and net._client_socket is not None
                            and net._client_socket is not broken_sock):
                        network._log("reconnect: socket replaced, skipping")
                        return
                    if net._client_socket is not None:
                        # v9.19: 如果网络线程已停止 → 强制允许重连
                        if net._network_thread and net._network_thread.is_alive():
                            network._log("reconnect: already connected, skipping")
                            return
                        # 线程死了但 socket 还在（recv_loop 未及时清理）→ 重置后重连
                        network._log("reconnect: stale socket, resetting")
                        try:
                            net._client_socket.close()
                        except Exception:
                            pass
                        net._client_socket = None
                    host_ip = _last_known_host_ip or claim_get_host_ip()
                    if not host_ip or host_ip == "127.0.0.1":
                        network._log("reconnect: no host ip")
                        return
                    attempts += 1
                    try:
                        network._log("reconnect attempt {}/{} to {}:{} (delay {:.1f}s)".format(
                            attempts, 20, host_ip, net.DEFAULT_PORT, delay))
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(3)
                        sock.connect((host_ip, net.DEFAULT_PORT))
                        sock.settimeout(None)
                        net._client_socket = sock
                        network._log("reconnect: connected to {}:{}".format(host_ip, net.DEFAULT_PORT))
                        _notify("已重连主机")
                        # 重新握手 hello
                        try:
                            net._send_json(sock, {"type": "hello", "name": _get_player_name(),
                                                  "password": "",
                                                  "proto_version": net.PROTO_VERSION})
                        except Exception:
                            pass
                        net._ensure_alarm()
                        import threading as _t
                        _t.Thread(target=net._recv_loop, args=(sock, True), daemon=True).start()
                        return
                    except Exception as e:
                        network._log("reconnect attempt failed: {}".format(e))
                    # v9.4: 指数退避 + jitter（研究: AWS 防 thundering herd——
                    # 多客户端同步重试会同时冲击主机，随机 ±20% 打散）
                    # 1.5 → 3 → 6 → 12 → 24 → 48 → 60 cap
                    delay = min(delay * 2, 60.0) * random.uniform(0.8, 1.2)
                # 重连失败 → 触发主机迁移竞选
                network._log("reconnect exhausted, triggering host migration")
                on_host_disconnect()
            finally:
                # v9.20.4: 循环结束(成功/耗尽/退出) → 允许下次调度
                _reconnect_in_progress = False

        threading.Thread(target=_reconnect_loop, daemon=True).start()
    except Exception as e:
        network._log("reconnect schedule error: {}".format(e))


def on_host_disconnect():
    """客户端检测到房主断开 → 触发迁移竞选"""
    global _members
    try:
        # 只保留在线成员信息（从本地副本）
        candidates = [m for m in _members.values()
                      if not m.get("is_host") and m.get("online", True)]
        my_pid = network._my_player_id
        if not candidates:
            return
        # 写竞选 claim（自己的 player_id + 时间戳 + 已知成员）
        claim = {
            "my_player_id": my_pid,
            "my_name": _get_player_name(),
            "ts": time.time(),
            "candidates": [c["player_id"] for c in candidates],
            "candidate_names": {c["player_id"]: c["name"] for c in candidates},
            "host_ip": _last_known_host_ip,
        }
        with open(MIGRATION_CLAIM_PATH, "w", encoding="utf-8") as f:
            json.dump(claim, f, ensure_ascii=False)
        network._log("lobby: host disconnect, migration claim written (my_pid={})".format(my_pid))
        _notify("房主掉线，正在竞选新房主...")
        # 等待竞选结算
        import threading

        def _settle():
            time.sleep(MIGRATION_SETTLE_SEC)
            try:
                _complete_migration()
            except Exception as e:
                network._log("migration settle error: {}".format(e))

        threading.Thread(target=_settle, daemon=True).start()
    except Exception as e:
        network._log("migration claim error: {}".format(e))


def _complete_migration():
    """竞选结算：player_id 最小者成为新房主"""
    global _members
    try:
        if not os.path.exists(MIGRATION_CLAIM_PATH):
            return
        with open(MIGRATION_CLAIM_PATH, "r", encoding="utf-8") as f:
            claim = json.load(f)
        my_pid = network._my_player_id
        candidates = claim.get("candidates", [])
        if my_pid not in candidates:
            return
        # 最小 player_id 获胜（加入最早者优先）
        new_host_pid = min(candidates)
        if my_pid == new_host_pid:
            # 我是新房主 → 开始监听 + 重建房间
            _become_new_host(claim)
        else:
            # 我不是新房主 → 等待新房主启动后重连
            network._log("lobby: {} is new host, waiting for re-connect".format(new_host_pid))
            _notify("{} 成为新房主，正在重连...".format(claim.get("candidate_names", {}).get(new_host_pid, "成员")))
            _schedule_reconnect()
    except Exception as e:
        network._log("migration complete error: {}".format(e))


def _become_new_host(claim):
    """成为新房主：切换角色 + 开服务器 + 广播"""
    global _members, ROOM_CODE, ROOM_VISIBILITY, ROOM_PASSWORD
    try:
        network._is_host = True
        network._my_player_id = 0
        # 重建房间（保留原房间码？重新生成更安全）
        ROOM_CODE = _gen_room_code()
        ROOM_VISIBILITY = "public"
        ROOM_PASSWORD = ""
        _members = {
            0: {
                "player_id": 0,
                "name": _get_player_name(),
                "ip": "127.0.0.1",
                "ready": False,
                "in_lot": False,
                "is_host": True,
                "online": True,
                "last_seen": time.time(),
            }
        }
        # 时间同步角色切换
        try:
            from multimod import clock_sync
            clock_sync.set_host_flag(True)
        except Exception:
            pass
        # 启动服务器
        import threading
        from multimod import network as net
        net._is_host = True
        net._my_player_id = 0
        net._network_thread = threading.Thread(target=net._server_thread, daemon=True)
        net._network_thread.start()
        _write_state_file()
        network._log("lobby: I am new host now, room recreated")
        _notify("🏠 你已成为新房主！其他成员将自动重连")
        # 清除竞选文件
        try:
            os.remove(MIGRATION_CLAIM_PATH)
        except Exception:
            pass
    except Exception as e:
        network._log("become new host error: {}".format(e))


def _schedule_reconnect():
    """非新房主：延迟重连到新房主（用 claim 里的 IP）"""
    try:
        import threading
        from multimod import network as net

        def _reconnect():
            time.sleep(MIGRATION_SETTLE_SEC + 2)
            try:
                # 新房主 IP：用原主机 IP（同一局域网内相同）
                host_ip = _last_known_host_ip or claim_get_host_ip()
                if not host_ip or host_ip == "127.0.0.1":
                    network._log("migration: no host ip to reconnect")
                    return
                network._log("migration: reconnecting to {}:{}".format(host_ip, net.DEFAULT_PORT))
                net._client_socket = None
                net._network_thread = threading.Thread(
                    target=net._client_thread, args=(host_ip,), daemon=True)
                net._network_thread.start()
            except Exception as e:
                network._log("reconnect error: {}".format(e))

        threading.Thread(target=_reconnect, daemon=True).start()
    except Exception as e:
        network._log("schedule reconnect error: {}".format(e))


def claim_get_host_ip():
    """从竞选文件读原主机 IP"""
    try:
        if os.path.exists(MIGRATION_CLAIM_PATH):
            with open(MIGRATION_CLAIM_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("host_ip", "")
    except Exception:
        pass
    return ""


def leave_room():
    """本机离开房间（发 leave 给主机 + 清理本地）"""
    global _members, _save_sync_phase, _start_granted
    try:
        if not network._is_host and network._client_socket is not None:
            network._send_json(network._client_socket, {"type": "leave"})
        _members = {}
        _save_sync_phase = "idle"
        _start_granted = False
        _write_state_file()
        network._log("lobby: left room")
    except Exception as e:
        network._log("lobby leave error: {}".format(e))


def on_leave(player_id):
    """成员离开 → 移除 + 广播"""
    global _members
    if player_id in _members:
        name = _members[player_id]["name"]
        del _members[player_id]
        network._log("lobby: {} left".format(name))
        _notify("{} 离开了房间".format(name))
        _broadcast_state()


def kick_member(player_id):
    """房主踢人：通知对方断开 + 移除"""
    global _members
    if not network._is_host:
        return False
    if player_id == 0 or player_id not in _members:
        return False
    name = _members[player_id]["name"]
    # 通知被踢者
    if player_id in network._clients:
        sock = network._clients[player_id][0]
        network._send_json(sock, {"type": "kicked", "reason": "房主将你移出房间"})
    del _members[player_id]
    network._log("lobby: {} kicked".format(name))
    _notify("已将 {} 移出房间".format(name))
    _broadcast_state()
    return True


def promote_member(player_id):
    """房主转交房主身份（简化：标记新房主）"""
    global _members
    if not network._is_host:
        return False
    if player_id == 0 or player_id not in _members:
        return False
    # 当前房主降为成员
    _members[0]["is_host"] = False
    # 目标升为房主（真正的主机迁移需网络重建，这里只标记）
    _members[player_id]["is_host"] = True
    network._log("lobby: promoted {} to host (标记)".format(_members[player_id]["name"]))
    _notify("已将房主身份转交 {} (需重建房间生效)".format(_members[player_id]["name"]))
    _broadcast_state()
    return True


# ============ v8.3: 旅行双端确认（研究: S4MP travel 黑屏修复） ============
# 场景切换（旅行）前双端确认：房主发起 → 成员确认 → 双端同时加载
# 避免 S4MP 经典 bug：一人先加载 → 另一人黑屏 / KeyError:2
_travel_pending = False
_travel_acks = set()


# ============ 旅行同步（v8.3 双端确认 + v9.11 场景切换增强） ============
# v9.11: 研究 S4MP 0.5.2 无限加载修复 + SimSync "travel together" 模式——
#   1) 双端确认出发（已有）
#   2) 抵达报告：客户端到新场景后广播 travel_arrived（带 zone_id）
#   3) 全员抵达确认：房主收齐后广播 travel_all_arrived → 双方确认都到了
#   4) 超时恢复：go 后 90s 未到齐 → travel_missing 提示（防黑屏/无限加载）
_travel_active = False      # 旅行进行中（场景切换锁定）
_travel_arrived = set()     # 已抵达新场景的成员（房主侧）
_travel_go_ts = 0           # go 广播时间（超时检测用）
_travel_auto_ack = True     # v9.22: 客机自动确认旅行（默认开——原实现要求每人
                            # 手输 mp_travel_ack，实际对局没人输 → 全靠 10s 超时兜底）
_travel_board = None        # v9.22: 客机侧镜像的旅行状态板（lobby 广播携带）
_travel_saved_speed = 1     # v9.23: 旅行前主机时钟速度（到齐后恢复——加载期间
                            # 客机无法处理 tick，锁时钟防时间漂移，S4MP 加载黑屏
                            # 场景的经典对策）
_follow_travel = True       # v9.24: 游戏内旅行自动跟随（主机 zone 变化 → 全员
                            # 自动切同一 zone——反编译 sims.visit_target_sim 确认
                            # sim_info.send_travel_switch_to_zone_op 为原生入口）
_travel_gen = 0             # v9.25: 旅行代次——连续旅行/快速切场景时，旧旅行的
                            # 30/60/90s Timer 还挂着；新旅行开始后旧 Timer 触发会
                            # 提前解锁新旅行/乱发 travel_missing/错乱恢复时钟。
                            # 每个 Timer 捕获启动时的代次，触发时代次不匹配即 no-op。
_travel_timers = []         # v9.25: 当前旅行的分级 Timer（集中取消用）


def _cancel_travel_timers():
    """v9.25: 集中取消当前旅行的所有分级 Timer。

    调用时机：新旅行开始（作废旧 Timer）/ 旅行正常结束 / 超时终局 / 房间重置。
    """
    global _travel_timers
    timers = _travel_timers
    _travel_timers = []
    for t in timers:
        try:
            t.cancel()
        except Exception:
            pass


def _start_travel_timers():
    """v9.25: 统一启动 30/60/90s 分级超时（代次捕获——旧代次触发时 no-op）。

    原实现 force_start / on_host_zone_changed 各自裸起 Timer 且从不取消：
    连续旅行时旧 90s Timer 在新旅行中途触发 → 提前解锁 + 乱发超时 + 恢复时钟。
    Timer 设 daemon——游戏退出不被残留定时器阻塞。
    """
    global _travel_timers
    _cancel_travel_timers()
    gen = _travel_gen
    try:
        import threading
        for delay, final in ((30.0, False), (60.0, False), (90.0, True)):
            t = threading.Timer(
                delay, lambda g=gen, f=final: _travel_progress_check(f, gen=g))
            t.daemon = True
            _travel_timers.append(t)
            t.start()
    except Exception as e:
        network._log("travel timers start error: {}".format(e))


def on_host_zone_changed(new_zone_id):
    """v9.24: 主机游戏内旅行被检测到（current_zone_id 变化）→ 全员自动跟随。

    完整链路：主机在游戏里正常旅行（手机选地点/点地图/拜访）→ 本函数广播
    travel_follow + 锁时钟 → 客机 on_travel_follow 调原生 API 自动切 zone →
    各端进图后 auto_report_arrival（v9.22）→ 全员到齐自动恢复时钟+刷新快照
    （v9.23）。玩家全程不需要输任何命令。
    """
    global _travel_active, _travel_arrived, _travel_go_ts, _travel_saved_speed, _travel_gen
    if not _follow_travel:
        network._log("lobby: follow travel off, host zone change ignored")
        return
    if _travel_active:
        network._log("lobby: travel already active, skip zone change")
        return
    _travel_active = True
    _travel_arrived = {0}  # 房主已在路上
    _travel_go_ts = time.time()
    _travel_gen += 1  # v9.25: 新旅行代次（作废旧 Timer）
    # 时钟锁定（同 _travel_force_start）
    try:
        from multimod import clock_sync
        sp = clock_sync.get_current_speed()
        _travel_saved_speed = int(sp) if sp is not None else 1
    except Exception:
        _travel_saved_speed = 1
    try:
        _cb = getattr(network, "_send_clock_broadcast", None)
        if _cb is not None:
            _cb(0)
    except Exception as e:
        network._log("lobby: travel clock lock error: {}".format(e))
    network._broadcast({"type": "travel_follow", "zone_id": int(new_zone_id),
                        "ts": time.time()})
    _notify("🧳 检测到你的旅行，成员正在自动跟随...")
    network._log("lobby: travel_follow broadcast zone={} (gen={})".format(
        new_zone_id, _travel_gen))
    # v9.25: 分级超时统一走 _start_travel_timers（代次捕获 + 取消旧 Timer）
    _start_travel_timers()
    _write_state_file()


def on_travel_follow(data):
    """v9.24: 客机收到主机旅行跟随 → 自动切换到同一 zone（原生 API）"""
    global _travel_active
    _travel_active = True
    zone_id = data.get("zone_id")
    network._notify("🧳 房主已出发旅行，正在自动跟随前往同一地点...")
    network._log("lobby: travel_follow received zone={}".format(zone_id))
    ok = _auto_travel_to_zone(zone_id)
    if not ok:
        network._notify("⚠️ 请手动旅行到房主的新地点（跟随失败已记录日志）")
    _write_state_file()


def _auto_travel_to_zone(zone_id):
    """v9.24: 客机自动跟随旅行——游戏原生 zone 切换。

    API 来源：反编译 server_commands/sim_commands.pyc 的 sims.visit_target_sim
    ——游戏"传送到目标小人所在地"就是一句 sim.sim_info.send_travel_switch_to_
    zone_op(zone_id=...)。同一存档同一世界模型下，客机切到同 zone 后由游戏
    自己完成加载与小人迁移。
    """
    try:
        import services
        client = services.client_manager().get_first_client()
        if client is None or client.active_sim is None:
            network._log("auto travel: no active sim")
            return False
        client.active_sim.sim_info.send_travel_switch_to_zone_op(zone_id=int(zone_id))
        network._log("lobby: auto travel switch to zone {}".format(zone_id))
        return True
    except Exception as e:
        network._log("auto travel error: {}".format(e))
        return False


def _travel_restore_clock():
    """v9.23: 旅行结束后恢复主机时钟速度（广播 saved speed，全员同步恢复）"""
    try:
        _cb = getattr(network, "_send_clock_broadcast", None)
        if _cb is not None:
            _cb(_travel_saved_speed)
            network._log("lobby: travel clock restored -> {}".format(_travel_saved_speed))
    except Exception as e:
        try:
            network._log("lobby: travel clock restore error: {}".format(e))
        except Exception:
            pass


def auto_report_arrival():
    """v9.22: 进图时自动上报抵达（network._check_lot_status 调用）。

    仅旅行锁定中生效——普通进图不上报（否则每次进图都广播 travel_arrived 刷屏）。
    原实现只有手动 mp_travel_arrived 命令，对局中没人记得输 → 到达确认形同虚设。
    """
    if not _travel_active:
        return False
    report_travel_arrived()
    return True


def toggle_travel_auto_ack():
    """v9.22: 切换客机自动确认旅行（启动器按钮 / mp_travel_auto 命令）"""
    global _travel_auto_ack
    _travel_auto_ack = not _travel_auto_ack
    _notify("旅行自动确认: {}".format("开" if _travel_auto_ack else "关"))
    _write_state_file()
    return _travel_auto_ack


def host_start_travel():
    """房主发起旅行（广播 travel_req，要求成员确认）"""
    global _travel_pending, _travel_acks, _travel_active, _travel_arrived, _travel_go_ts
    if not network._is_host:
        _notify("只有房主能发起旅行同步!")
        return
    _travel_pending = True
    _travel_acks = {0}  # 房主自己已确认
    _travel_active = False  # 等确认后才锁定
    _notify("旅行同步已发起：请所有成员在 10 秒内确认 (mp_travel_ack)")
    network._log("lobby: travel_req broadcast")
    network._broadcast({"type": "travel_req", "ts": time.time()})
    # 10 秒后强制放行（避免卡死）
    import threading
    threading.Timer(10.0, _travel_force_start).start()


def on_travel_ack(player_id):
    """成员确认旅行就绪"""
    global _travel_acks
    _travel_acks.add(player_id)
    _notify("成员 {} 已确认旅行就绪 ({}/{})".format(
        _members.get(player_id, {}).get("name", player_id),
        len(_travel_acks), len(_members)))
    network._log("lobby: travel ack from {}".format(player_id))
    if _travel_acks.issuperset(set(_members.keys())):
        _travel_force_start()


def _travel_force_start():
    """全部确认（或超时）→ 通知所有成员开始加载"""
    global _travel_pending, _travel_active, _travel_arrived, _travel_go_ts
    global _travel_saved_speed, _travel_gen
    if not _travel_pending:
        return
    _travel_pending = False
    _travel_active = True  # 场景切换锁定开始
    _travel_arrived = {0} if network._is_host else set()  # 房主自己视为"出发中"
    _travel_go_ts = time.time()
    _travel_gen += 1  # v9.25: 新旅行代次（作废旧 Timer）
    # v9.23: 旅行期间锁定时钟（广播 PAUSED）——场景加载时客机无法处理游戏
    # tick，若主机时间继续走会造成加载后时间不一致；到齐/超时后恢复原速。
    try:
        from multimod import clock_sync
        sp = clock_sync.get_current_speed()
        _travel_saved_speed = int(sp) if sp is not None else 1
    except Exception:
        _travel_saved_speed = 1
    # v9.23: 广播 PAUSED 锁定时钟（getattr 防御——测试 stub/旧环境无此函数时不炸）
    try:
        _cb = getattr(network, "_send_clock_broadcast", None)
        if _cb is not None:
            _cb(0)
    except Exception as e:
        network._log("lobby: travel clock lock error: {}".format(e))
    # 重置所有成员进图状态（离开旧场景）
    for m in _members.values():
        m["in_lot"] = False
    _notify("✅ 全员确认完毕，可以同时旅行了！")
    network._log("lobby: travel go (gen={})".format(_travel_gen))
    network._broadcast({"type": "travel_go", "ts": _travel_go_ts})
    # v9.25: 分级超时统一走 _start_travel_timers（代次捕获 + 取消旧 Timer）
    _start_travel_timers()
    _write_state_file()


def report_travel_arrived(zone_id=None):
    """本机已到达新场景（游戏内场景切换完成时调用）

    v9.11: 场景切换完成后报告——房主收集全员抵达后广播 travel_all_arrived
    """
    global _members, _travel_arrived
    pid = network._my_player_id
    if pid in _members:
        _members[pid]["in_lot"] = True
    if network._is_host:
        # 房主自己抵达
        _travel_arrived.add(pid)
        network._log("lobby: host arrived zone={}".format(zone_id))
        _broadcast_state()
        _check_travel_all_arrived()
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, {
                "type": "travel_arrived", "player": pid, "zone_id": zone_id})
            network._log("lobby: sent travel_arrived zone={}".format(zone_id))


def on_travel_arrived(player_id, zone_id=None):
    """房主收到成员抵达报告"""
    global _travel_arrived
    if not network._is_host:
        return
    if player_id in _members:
        _members[player_id]["in_lot"] = True
    _travel_arrived.add(player_id)
    _notify("成员 {} 已抵达新场景 ({}/{})".format(
        _members.get(player_id, {}).get("name", player_id),
        len(_travel_arrived), len(_members)))
    network._log("lobby: member {} arrived zone={}".format(player_id, zone_id))
    _broadcast_state()
    _check_travel_all_arrived()


def _check_travel_all_arrived():
    """房主：全员抵达 → 广播 travel_all_arrived（解除旅行锁定）"""
    global _travel_active
    if not _travel_active:
        return
    if _travel_arrived.issuperset(set(_members.keys())):
        _travel_active = False
        _cancel_travel_timers()  # v9.25: 正常结束集中取消
        _notify("✅ 全员已到达新场景，继续游戏！")
        network._log("lobby: travel all arrived")
        network._broadcast({"type": "travel_all_arrived", "ts": time.time()})
        # v9.22: 旅行后全量快照刷新——新场景的位置/资金基准已变，只等各模块
        # 下次变化才广播会长时间不一致（对端看不到彼此在新场景的初始位置）。
        try:
            with network._clients_lock:
                socks = [s for (s, _a) in list(network._clients.values())]
            for s in socks:
                network._send_world_snapshot(s)
        except Exception as e:
            network._log("travel snapshot refresh error: {}".format(e))
        _travel_restore_clock()  # v9.23: 到齐后恢复时钟（快照已对齐新场景）
        _write_state_file()


def _travel_progress_check(final, gen=None):
    """房主：go 后分级检查（v9.22: 30/60s 进度提示 / 90s 超时解除锁定）

    v9.25: gen 代次守卫——Timer 捕获启动时的代次，若新旅行已开始（代次已变）
    则本检查作废（否则旧 90s Timer 会提前解锁新旅行/乱发 travel_missing/
    错乱恢复时钟——连续旅行/快速切场景必现）。gen=None 表示直接调用
    （测试/手动），作用于当前代次。
    """
    global _travel_active
    if gen is not None and gen != _travel_gen:
        network._log("lobby: stale travel timer ignored (gen {} != {})".format(gen, _travel_gen))
        return
    if not _travel_active:
        return
    # v9.20.5: 成员字典可能缺 player_id 字段（旧状态文件/异常写入）——
    # 用字典 key 作为权威 pid，避免 KeyError 打死定时器线程。
    missing = [m.get("name", "玩家{}".format(pid))
               for pid, m in list(_members.items())
               if m.get("player_id", pid) not in _travel_arrived]
    if not missing:
        return
    if not final:
        _notify("⏳ 旅行加载中：等待 {} ({}s)".format(
            ", ".join(missing), int(time.time() - _travel_go_ts)))
        network._log("lobby: travel progress missing: {}".format(missing))
        _write_state_file()
        return
    _travel_active = False
    _cancel_travel_timers()  # v9.25: 终局集中取消
    _notify("⚠️ 旅行超时：{} 未到达，已解除锁定".format(", ".join(missing)))
    network._log("lobby: travel missing: {}".format(missing))
    network._broadcast({"type": "travel_missing", "missing": missing})
    _travel_restore_clock()  # v9.23: 超时解锁也要恢复时钟
    _write_state_file()


def on_travel_all_arrived(data):
    """客户端收到全员抵达 → 解除旅行锁定"""
    global _travel_active
    _travel_active = False
    _notify("✅ 全员已到达新场景，继续游戏！")
    network._log("lobby: travel_all_arrived received")


def on_travel_missing(data):
    """客户端收到旅行超时提示"""
    global _travel_active
    _travel_active = False
    missing = data.get("missing", [])
    _notify("⚠️ 部分成员未到达：{}，已解除锁定".format(", ".join(missing)))
    network._log("lobby: travel_missing received")


def is_travel_active():
    """旅行锁定查询（sync.py 场景切换期间暂停位置同步）"""
    return _travel_active


def on_travel_req(data):
    """客户端收到旅行请求 → 自动确认（v9.22 默认）或提示手动确认"""
    global _travel_auto_ack
    if _travel_auto_ack:
        if network._client_socket is not None:
            network._send_json(network._client_socket,
                               {"type": "travel_ack", "ts": time.time()}, prio=0)
        _notify("🧳 房主发起共同旅行：已自动确认 ✅")
        network._log("lobby: travel_req auto-acked")
    else:
        _notify("房主要求同步旅行：请输入 mp_travel_ack 确认")
        network._log("lobby: travel_req received (manual ack mode)")


def on_travel_go(data):
    """客户端收到放行 → 提示可以旅行了 + 进入锁定"""
    global _travel_active
    _travel_active = True
    _notify("✅ 全员就绪，请与房主同时旅行！")
    network._log("lobby: travel_go received")


def on_ready(player_id, ready):
    """客户端准备/取消 → 更新状态 + 广播"""
    if player_id in _members:
        _members[player_id]["ready"] = bool(ready)
        network._log("lobby: {} ready={}".format(_members[player_id]["name"], ready))
        _broadcast_state()


def on_in_lot(player_id, in_lot):
    """客户端报告进图/离开 → 更新状态 + 广播"""
    if player_id in _members:
        _members[player_id]["in_lot"] = bool(in_lot)
        network._log("lobby: {} in_lot={}".format(_members[player_id]["name"], in_lot))
        _broadcast_state()


def all_clients_ready():
    """所有非房主成员是否都已准备"""
    clients = [m for m in _members.values() if not m["is_host"]]
    if not clients:
        return True  # 无客户端
    return all(m["ready"] for m in clients)


def all_clients_in_lot():
    """所有非房主成员是否都已进入家庭地段"""
    clients = [m for m in _members.values() if not m["is_host"]]
    if not clients:
        return True
    return all(m["in_lot"] for m in clients)


# ============ 存档同步流程 ============
def host_start_save_sync(filename=None):
    """房主开始存档同步（要求所有非房主已准备）

    v6.2: filename 指定后直接发送存档文件（SimSync 模式）；
    不指定时仍走旧确认流程（只广播 save_sync_req）
    """
    global _save_sync_phase, _save_sync_acks
    if not all_clients_ready():
        _notify("等待所有成员准备就绪后才能同步存档!")
        network._log("lobby: save sync blocked, not all ready")
        return False
    if filename:
        # 直接发送存档文件（SimSync 模式）
        ok, desc = host_send_save_file(filename)
        if not ok:
            _notify("存档发送失败: {}".format(desc))
        else:
            # v9.20.1: 发送成功后直接授予开始权（文件已真正同步）
            global _start_granted
            _save_sync_phase = "done"
            _start_granted = True
            _broadcast_state()
        return ok
    # v9.20.1: 无 filename → 自动选 Saves 目录最新存档发送（修复"显示同步但存档没同步"）
    auto_file = _pick_latest_save()
    if auto_file:
        ok, desc = host_send_save_file(auto_file)
        if ok:
            _save_sync_phase = "done"
            _start_granted = True
            _broadcast_state()
        else:
            _notify("存档发送失败: {}".format(desc))
        return ok
    # 完全无存档 → 旧确认流程（退化）
    _save_sync_phase = "waiting_ack"
    _save_sync_acks = set()
    network._broadcast({"type": "save_sync_req"})
    _notify("已发送存档同步请求，等待成员确认...")
    network._log("lobby: save sync requested")
    _broadcast_state()
    return True


def on_save_sync_ack(player_id):
    """客户端确认收到同步请求"""
    global _save_sync_phase, _start_granted
    if _save_sync_phase == "waiting_ack":
        _save_sync_acks.add(player_id)
        network._log("lobby: save sync ack from pid={} ({}/{})".format(
            player_id, len(_save_sync_acks), len([m for m in _members if not _members[m]["is_host"]])))
        # 所有客户端都确认 → 完成
        clients = [m for m in _members.values() if not m["is_host"]]
        if clients and len(_save_sync_acks) >= len(clients):
            _save_sync_phase = "done"
            _start_granted = True
            network._broadcast({"type": "save_sync_done"})
            _notify("存档同步完成! 所有成员现在可以开始游戏")
            network._log("lobby: save sync done, start granted")
            _broadcast_state()


def host_start_game():
    """房主开始游戏（要求：存档同步完成 + 所有成员进图）"""
    if not _start_granted:
        _notify("先完成存档同步后才能开始游戏!")
        network._log("lobby: start blocked, no save sync")
        return False
    if not all_clients_in_lot():
        _notify("等待所有成员进入家庭后再开始游戏!")
        network._log("lobby: start blocked, not all in lot")
        return False
    network._broadcast({"type": "start_game"})
    # v9.20.1: 同时广播时钟正常速度——客机被 clock hook 拦截无法自己解除暂停，
    # 必须收到主机 clock 广播才会 apply_remote_clock（修复"主机开始了客机仍暂停"）
    try:
        network._send_clock_broadcast(1)
    except Exception as e:
        network._log("lobby: start clock broadcast error: {}".format(e))
    _notify("游戏开始!")
    network._log("lobby: game started")
    return True


# ============ 本地状态上报 ============
def report_my_ready(ready):
    """本机准备/取消（客机 → 发给主机；主机 → 自己更新）"""
    global _members
    pid = network._my_player_id
    if pid in _members:
        _members[pid]["ready"] = bool(ready)
    if network._is_host:
        network._log("lobby: host ready={}".format(ready))
        _broadcast_state()
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, {"type": "ready", "ready": bool(ready)}, prio=0)
            network._log("lobby: sent ready={}".format(ready))


def report_my_in_lot(in_lot):
    """本机进图状态（客机 → 发给主机；主机 → 自己更新 + 检查门槛）"""
    global _members
    pid = network._my_player_id
    if pid in _members:
        _members[pid]["in_lot"] = bool(in_lot)
    if network._is_host:
        network._log("lobby: host in_lot={}".format(in_lot))
        _broadcast_state()
        # 进图门槛：房主进入家庭地段时检查成员是否全进
        if in_lot and not all_clients_in_lot():
            missing = [m["name"] for m in _members.values() if not m["is_host"] and not m["in_lot"]]
            _notify("等待成员进入家庭: {}".format(", ".join(missing)))
            network._log("lobby: host in lot, waiting: {}".format(missing))
    else:
        if network._client_socket is not None:
            network._send_json(network._client_socket, {"type": "in_lot", "in_lot": bool(in_lot)})
            network._log("lobby: sent in_lot={}".format(in_lot))


# ============ 消息处理（network._process_incoming 分发）============
def process_message(data, sender_pid=None):
    """处理房间系统消息"""
    global _members, _save_sync_phase, _start_granted
    mtype = data.get("type")
    try:
        if mtype == "hello":
            # 主机收到客户端 hello → 验证密码 → 加入房间
            name = data.get("name", "玩家")
            ip = data.get("ip", "")
            password = data.get("password", "")
            if sender_pid is not None:
                # v9.12: 协议版本校验（研究: MCP handshake / QUIC RFC 9368）
                # 客户端协议版本不兼容 → 拒绝（防旧 mod 连新 host 导致消息错乱）
                try:
                    client_ver = int(data.get("proto_version", 0) or 0)
                except (TypeError, ValueError):
                    client_ver = 0
                if client_ver != network.PROTO_VERSION:
                    network._log("lobby: version mismatch client={} host={} from {}".format(
                        client_ver, network.PROTO_VERSION, ip))
                    sock = network._clients[sender_pid][0]
                    network._send_json(sock, {"type": "version_mismatch",
                                              "client_ver": client_ver,
                                              "host_ver": network.PROTO_VERSION})
                    return
                # 私密房间密码验证（十轮研究: flackr/lobby private with codes）
                # v9.25: hmac.compare_digest 防时序侧信道
                if ROOM_VISIBILITY == "private" and not _hmac_mod.compare_digest(
                    str(password or ""), str(ROOM_PASSWORD)
                ):
                    network._log("lobby: join rejected (bad password) from {}".format(ip))
                    sock = network._clients[sender_pid][0]
                    network._send_json(sock, {"type": "join_rejected", "reason": "房间密码错误"})
                    return
                on_hello(sender_pid, name, ip)
                # v9.16: 握手密钥派生（研究: HKDF RFC 5869）
                # host 收到 client_nonce + 自己的 host_nonce → 派生会话密钥 → 回发 host_nonce
                # v9.22: host_nonce 改按连接查询（_conn_nonces）——原用模块级全局
                # network._my_nonce，两客机并发握手时被后连的覆盖 → 先连的派生错 key
                try:
                    c_nonce = str(data.get("client_nonce", "")).encode()
                    h_nonce = network._conn_nonces.get(sender_pid)
                    if c_nonce and h_nonce:
                        network._hmac_keys[sender_pid] = network._derive_hmac_key(ROOM_PASSWORD, c_nonce, h_nonce)
                        network._log("HMAC key derived (host, {}B)".format(len(network._hmac_keys[sender_pid])))
                except Exception as e:
                    network._log("hmac derive error: {}".format(e))
                # 回复 welcome（含自己的 player_id 和当前房间状态）
                if network._client_socket is None and sender_pid in network._clients:
                    sock = network._clients[sender_pid][0]
                    _h_nonce = network._conn_nonces.get(sender_pid) or b""
                    network._send_json(sock, {"type": "welcome", "player_id": sender_pid,
                                              "host_nonce": _h_nonce.decode("utf-8")})
                    network._send_json(sock, {"type": "lobby", "members": list(_members.values()),
                                              "save_sync_phase": _save_sync_phase,
                                              "start_granted": _start_granted,
                                              "room_code": ROOM_CODE})
                    # v9.19: world_snapshot 在 HMAC key 派生后发送（握手完成后第一帧）
                    try:
                        network._send_world_snapshot(sock)
                    except Exception as e:
                        network._log("world_snapshot send error: {}".format(e))
                    # v9.21 P1: 新成员加入 → 重置位置基准，下一包发绝对坐标。
                    # 否则新客机只收到 delta 而无基准，会一直丢弃（"delta without
                    # base"）→ 中途加入的人永远看不到别人移动。
                    try:
                        from multimod import sync
                        sync.reset_pos_baseline("member joined pid={}".format(sender_pid))
                    except Exception as e:
                        network._log("pos baseline reset error: {}".format(e))
            return

        if mtype == "join_rejected":
            # 客户端被拒绝加入（密码错误）
            _notify("加入被拒绝: {}".format(data.get("reason", "")))
            _members = {}
            _write_state_file()
            network._log("lobby: join rejected")
            return

        if mtype == "version_mismatch":
            # v9.12: 客户端协议版本不兼容（研究: MCP/QUIC 版本协商）
            _notify("⚠️ 协议版本不兼容：你的 mod v{} 与房主 v{} 不匹配，请双方更新到同一版本".format(
                data.get("client_ver", "?"), data.get("host_ver", "?")))
            _members = {}
            _write_state_file()
            network._log("lobby: version mismatch received")
            return

        if mtype == "welcome":
            # 客户端收到主机分配 player_id
            network._my_player_id = int(data.get("player_id", 0))
            # v9.16: 握手密钥派生（研究: HKDF RFC 5869）
            # client 收到 host_nonce + 自己的 client_nonce → 派生会话密钥
            try:
                h_nonce = str(data.get("host_nonce", "")).encode()
                c_nonce = network._my_nonce
                if h_nonce and c_nonce:
                    network._peer_nonce = h_nonce
                    network._hmac_keys[network._my_player_id] = network._derive_hmac_key(ROOM_PASSWORD, c_nonce, h_nonce)
                    network._log("HMAC key derived (client, {}B)".format(len(network._hmac_keys[network._my_player_id])))
            except Exception as e:
                network._log("hmac derive error: {}".format(e))
            network._log("lobby: got player_id={}".format(network._my_player_id))
            # 把本机加入本地成员列表（显示自己）
            _members[network._my_player_id] = {
                "player_id": network._my_player_id,
                "name": _get_player_name(),
                "ip": "",
                "ready": False,
                "in_lot": False,
                "is_host": False,
                "online": True,
                "last_seen": time.time(),
            }
            # 向主机上报自己的进图状态（若已在家庭地段）
            _write_state_file()
            start_heartbeat()  # M3d 增强: 客户端开始心跳
            # v9.21 P1: 客机（含重连）握手完成 → 重置位置基准。
            # 重连后本机 _last_broadcast_pos 仍是旧值 → 只会发 delta，
            # 而房主端的基准可能已随断线失效 → 双向位置都不同步。
            try:
                from multimod import sync
                sync.reset_pos_baseline("client welcome pid={}".format(network._my_player_id))
            except Exception as e:
                network._log("pos baseline reset error: {}".format(e))
            return

        if mtype == "lobby":
            # 客户端收到主机广播的房间状态 → 更新本地副本
            _members = {}
            for m in data.get("members", []):
                _members[int(m["player_id"])] = m
            _save_sync_phase = data.get("save_sync_phase", "idle")
            _start_granted = data.get("start_granted", False)
            # v9.22: 镜像主机侧旅行状态板（客机本地无 _travel_arrived 数据）
            global _travel_board
            _travel_board = data.get("travel")
            _write_state_file()
            network._log("lobby: received state, {} members".format(len(_members)))
            return

        if mtype == "ready":
            # 主机收到客户端准备状态
            if network._is_host and sender_pid is not None:
                on_ready(sender_pid, data.get("ready", False))
            return

        if mtype == "in_lot":
            # 主机收到客户端进图状态
            if network._is_host and sender_pid is not None:
                on_in_lot(sender_pid, data.get("in_lot", False))
            return

        if mtype == "travel_ack":
            # v9.25: 房主收到客机旅行确认（此前缺失——auto-ack 白发, 每局等 10s 超时兜底）
            if network._is_host and sender_pid is not None:
                on_travel_ack(sender_pid)
            return

        if mtype == "travel_arrived":
            # v9.11: 房主收到成员抵达报告
            if network._is_host and sender_pid is not None:
                on_travel_arrived(sender_pid, data.get("zone_id"))
            return

        if mtype == "travel_all_arrived":
            # v9.11: 客户端收到全员抵达
            if not network._is_host:
                on_travel_all_arrived(data)
            return

        if mtype == "travel_missing":
            # v9.11: 客户端收到旅行超时提示
            if not network._is_host:
                on_travel_missing(data)
            return

        if mtype == "heartbeat":
            # 主机收到客户端心跳
            if network._is_host and sender_pid is not None:
                on_heartbeat(sender_pid)
            return

        if mtype == "leave":
            # 主机收到成员离开
            if network._is_host and sender_pid is not None:
                on_leave(sender_pid)
            return

        if mtype == "kicked":
            # 客户端被房主踢出
            _notify("你已被移出房间: {}".format(data.get("reason", "")))
            _members = {}
            _write_state_file()
            network._log("lobby: kicked from room")
            return

        if mtype == "save_chunk":
            # 客户端收到存档块（SimSync 模式）
            on_save_chunk(data)
            return

        if mtype == "save_resend_req":
            # v9.5: 房主收到客户端重传请求 → 重发缺失块（研究: MAVLink FTP）
            if network._is_host:
                filename = data.get("filename", "")
                missing = data.get("missing", [])
                _resend_save_chunks(filename, missing)
            return

        if mtype == "save_chunk_done":
            # 客户端收完所有块 → 写入
            on_save_chunk_done(data)
            return

        if mtype == "save_sync_req":
            # 客户端收到同步请求 → 自动确认（存档由房主提供）
            _notify("房主请求同步存档，正在确认...")
            if network._client_socket is not None:
                network._send_json(network._client_socket, {"type": "save_sync_ack", "ok": True})
            network._log("lobby: save sync ack sent")
            return

        if mtype == "save_sync_ack":
            # 主机收到客户端确认
            if network._is_host and sender_pid is not None:
                on_save_sync_ack(sender_pid)
            return

        if mtype == "save_sync_done":
            # 客户端收到同步完成 → 获得开始权
            _save_sync_phase = "done"
            _start_granted = True
            _write_state_file()
            _notify("存档同步完成! 可以开始游戏")
            network._log("lobby: save sync done (client)")
            return

        if mtype == "start_game":
            # 客户端收到开始指令
            _notify("房主已开始游戏!")
            network._log("lobby: game start received (client)")
            # v9.20.1: 应用主机时钟（解除暂停）——修复"主机开始客机仍暂停"
            try:
                from multimod import clock_sync
                speed = data.get("speed", 1)
                clock_sync.apply_remote_clock(speed)
                network._log("lobby: client clock applied speed={}".format(speed))
            except Exception as e:
                network._log("lobby: client clock apply error: {}".format(e))
            return
    except Exception as e:
        network._log("lobby process error: {}".format(e))


# ============ 命令 ============
@sims4.commands.Command('mp_ready', command_type=sims4.commands.CommandType.Live)
def mp_ready(_connection=None):
    """标记自己已准备"""
    report_my_ready(True)
    _safe_output(_connection, "✅ 已准备")


@sims4.commands.Command('mp_unready', command_type=sims4.commands.CommandType.Live)
def mp_unready(_connection=None):
    """取消准备"""
    report_my_ready(False)
    _safe_output(_connection, "已取消准备")


@sims4.commands.Command('mp_syncsave', command_type=sims4.commands.CommandType.Live)
def mp_syncsave(filename=None, _connection=None):
    """房主：同步存档（需所有非房主已准备）

    mp_syncsave               → 旧确认流程
    mp_syncsave Slot_1.save   → 直接发送指定存档文件（SimSync 模式）
    """
    if not network._is_host:
        _safe_output(_connection, "只有房主可以同步存档")
        return
    ok = host_start_save_sync(filename)
    _safe_output(_connection, "已请求同步存档" if ok else "同步存档失败（成员未全准备）")


@sims4.commands.Command('mp_saves', command_type=sims4.commands.CommandType.Live)
def mp_saves(_connection=None):
    """列出 Saves 目录的存档文件（房主选择要发送的存档）"""
    output = sims4.commands.CheatOutput(_connection)
    try:
        os.makedirs(SAVES_DIR, exist_ok=True)
        files = sorted(os.listdir(SAVES_DIR))
        saves = [f for f in files if f.endswith(".save") and not f.endswith(".bak")]
        output("Saves 目录存档 ({}):".format(len(saves)))
        for s in saves[:20]:
            size = os.path.getsize(os.path.join(SAVES_DIR, s)) / 1024
            output("  {} ({:.0f}KB)".format(s, size))
    except Exception as e:
        output("读取存档目录失败: {}".format(e))
    network._log("mp_saves listed")


@sims4.commands.Command('mp_start', command_type=sims4.commands.CommandType.Live)
def mp_start(_connection=None):
    """房主：开始游戏（需存档同步完成 + 所有成员进图）"""
    if not network._is_host:
        _safe_output(_connection, "只有房主可以开始游戏")
        return
    ok = host_start_game()
    _safe_output(_connection, "游戏开始!" if ok else "无法开始（未同步存档或成员未进图）")


@sims4.commands.Command('mp_lobby', command_type=sims4.commands.CommandType.Live)
def mp_lobby(_connection=None):
    """查看房间状态"""
    output = sims4.commands.CheatOutput(_connection)
    output("=== 房间状态 ({}人) 房间码: {} ===".format(len(_members), ROOM_CODE or "无"))
    for pid, m in list(_members.items()):
        output("  [{}] {} 准备={} 进图={} 在线={} {}".format(
            "房主" if m.get("is_host") else "成员", m.get("name", "玩家{}".format(pid)),
            "✅" if m.get("ready") else "❌", "✅" if m.get("in_lot") else "❌",
            "🟢" if m.get("online") else "⚫",
            "(本机)" if m.get("player_id", pid) == network._my_player_id else ""))
    output("存档同步: {} | 开始权: {}".format(_save_sync_phase, _start_granted))
    network._log("mp_lobby: {} members".format(len(_members)))


@sims4.commands.Command('mp_leave', command_type=sims4.commands.CommandType.Live)
def mp_leave(_connection=None):
    """离开房间"""
    leave_room()
    _safe_output(_connection, "已离开房间")


@sims4.commands.Command('mp_code', command_type=sims4.commands.CommandType.Live)
def mp_code(_connection=None):
    """查看房间码"""
    output = sims4.commands.CheatOutput(_connection)
    output("房间码: {}".format(ROOM_CODE or "无（仅房主有）"))
    network._log("mp_code: {}".format(ROOM_CODE))


@sims4.commands.Command('mp_kick', command_type=sims4.commands.CommandType.Live)
def mp_kick(player_id=None, _connection=None):
    """房主踢人: mp_kick <player_id>"""
    if not network._is_host:
        _safe_output(_connection, "只有房主可以踢人")
        return
    try:
        pid = int(player_id)
    except Exception:
        _safe_output(_connection, "用法: mp_kick <player_id>（mp_lobby 查看 ID）")
        return
    ok = kick_member(pid)
    _safe_output(_connection, "已踢出" if ok else "踢人失败（ID 无效或为房主）")


@sims4.commands.Command('mp_promote', command_type=sims4.commands.CommandType.Live)
def mp_promote(player_id=None, _connection=None):
    """房主转交身份: mp_promote <player_id>（标记，需重建房间生效）"""
    if not network._is_host:
        _safe_output(_connection, "只有房主可以转交身份")
        return
    try:
        pid = int(player_id)
    except Exception:
        _safe_output(_connection, "用法: mp_promote <player_id>")
        return
    ok = promote_member(pid)
    _safe_output(_connection, "已转交" if ok else "转交失败")


@sims4.commands.Command('mp_travel', command_type=sims4.commands.CommandType.Live)
def mp_travel(_connection=None):
    """房主发起旅行同步（双端确认，防黑屏）: mp_travel"""
    if not network._is_host:
        _safe_output(_connection, "只有房主可以发起旅行同步")
        return
    host_start_travel()
    _safe_output(_connection, "旅行同步已发起，等待成员确认...")


@sims4.commands.Command('mp_travel_ack', command_type=sims4.commands.CommandType.Live)
def mp_travel_ack(_connection=None):
    """成员确认旅行就绪: mp_travel_ack"""
    if network._is_host:
        _safe_output(_connection, "房主无需确认")
        return
    if not network._client_socket:
        _safe_output(_connection, "未连接房间")
        return
    network._send_json(network._client_socket, {"type": "travel_ack", "ts": time.time()}, prio=0)
    _safe_output(_connection, "已确认旅行就绪，等待全员...")


@sims4.commands.Command('mp_travel_arrived', command_type=sims4.commands.CommandType.Live)
def mp_travel_arrived(zone_id="0", _connection=None):
    """到达新场景后报告（v9.11: 全员抵达确认，防黑屏/无限加载）: mp_travel_arrived"""
    report_travel_arrived(zone_id=int(zone_id) if str(zone_id).isdigit() else zone_id)
    _safe_output(_connection, "已报告抵达新场景，等待全员...")


@sims4.commands.Command('mp_travel_auto', command_type=sims4.commands.CommandType.Live)
def mp_travel_auto(_connection=None):
    """切换客机自动确认旅行（v9.22，默认开）: mp_travel_auto"""
    state = toggle_travel_auto_ack()
    _safe_output(_connection, "旅行自动确认: {}".format("开" if state else "关"))


@sims4.commands.Command('mp_follow_travel', command_type=sims4.commands.CommandType.Live)
def mp_follow_travel(_connection=None):
    """切换游戏内旅行自动跟随（v9.24，默认开）: mp_follow_travel

    开启时主机在游戏里旅行（手机选地点/点地图）会被自动检测，
    成员自动跟随到同一地点，全程无需任何命令。
    """
    global _follow_travel
    _follow_travel = not _follow_travel
    _notify("游戏内旅行跟随: {}".format("开" if _follow_travel else "关"))
    _write_state_file()
    _safe_output(_connection, "游戏内旅行跟随: {}".format("开" if _follow_travel else "关"))


def _safe_output(_connection, msg):
    try:
        if _connection is not None:
            sims4.commands.CheatOutput(_connection)(msg)
    except Exception:
        pass
    network._log("lobby cmd: {}".format(msg))


# ============ v9.19: 房间状态重置（第二次连接前清理残留） ============
def reset_room_state():
    """完全重置房间状态（双端都调用）——解决第二次连接状态残留问题"""
    global _members, ROOM_CODE, ROOM_VISIBILITY, ROOM_PASSWORD
    global _save_sync_phase, _start_granted
    global _travel_active, _travel_arrived, _travel_board, _travel_pending, _travel_acks
    global _travel_gen
    _members = {}
    ROOM_CODE = ""
    ROOM_VISIBILITY = "public"
    ROOM_PASSWORD = ""
    _save_sync_phase = "idle"
    _start_granted = False
    # v9.22: 旅行状态一并清理（否则二次联机残留"旅行中"锁死位置同步）
    _travel_active = False
    _travel_arrived = set()
    _travel_board = None
    _travel_pending = False
    _travel_acks = set()
    # v9.25: 取消挂着的旅行 Timer + 代次自增作废游离定时器
    _travel_gen += 1
    _cancel_travel_timers()
    # v9.24: 主机 zone 基线重置（新会话首次进图重新建基线，不算旅行）
    try:
        network._last_zone_id = None
    except Exception:
        pass
    _write_state_file()
    network._log("lobby: room state reset")


@sims4.commands.Command('mp_reset', command_type=sims4.commands.CommandType.Live)
def mp_reset(_connection=None):
    """重置房间状态（用于第二次联机前清理残留）"""
    reset_room_state()
    _safe_output(_connection, "房间状态已重置")
