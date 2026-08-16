# 《已知问题与改进清单》

> 基准代码：v9.22（PROTO_VERSION=2）。审查范围：`src/multimod/*.py` 全部 16 个模块、
> `launcher.py`、`room_protocol.py`，共约 8600 行。
> 行号为当前代码基准，套用补丁前请先核对上下文。
> 严重级别：**P0** = 安全漏洞或功能完全失效；**P1** = 功能错误/数据风险；
> **P2** = 健壮性/体验/资源问题。

统计：P0 × 16，P1 × 28，P2 × 40+。修复顺序建议见文末。

---

## A. network.py（游戏内协议层）

### N-1【P1】QoS 优先级是死参数，`_send_batch` 无调用方

- 位置：`network.py:714`（`_send_json(sock, payload, prio=1)` 签名接受 prio 但函数体从未使用）、`network.py:750`（`_send_batch` 定义后全工程零调用）。
- 影响：PROTOCOL.md 宣称的"消息优先级/低优批处理"实际未生效；位置消息（prio=2）无法合批，帧头开销占比高。
- 修复（最小可用版：prio=0 直发，prio=2 进 100ms 合批器；完整 QoS 见路线图 v9.80）：

```python
# network.py 新增（v9.23）
_pending_low = {}                  # id(sock) -> (sock, [payload, ...])
_pending_low_lock = threading.Lock()

def _flush_low_prio():
    """100ms 冲刷一次低优消息（真调用 _send_batch——它已存在但从未被用）"""
    with _pending_low_lock:
        items = list(_pending_low.values())
        _pending_low.clear()
    for sock, payloads in items:
        try:
            _send_batch(sock, payloads)
        except Exception:
            pass

# _process_alarm_callback / _on_tick_callback 里追加：
def _process_alarm_callback(_alarm_handle):
    _process_incoming()
    _send_ping()
    _check_lot_status()
    _check_launcher_cmd()
    _check_heartbeats()
    _flush_low_prio()              # v9.23: 低优先级合批冲刷
    return True

# _send_json 改为（保持签名不变）：
def _send_json(sock, payload, prio=1):
    if prio >= 2:
        with _pending_low_lock:
            _pending_low.setdefault(id(sock), []).append((sock, payload))
        return True
    ...  # 原直发逻辑不变
```

> 注：`_pending_low` 需存 `(sock, payload)` 元组而非裸 payload，避免冲刷时
> 拿不到 socket；连接断开时在 `_drop_send_lock(sock)` 里同步清该 sock 的待发队列。

### N-2【P1】welcome 双发导致客机重复派生密钥、双起心跳线程

- 位置：`network.py:1037`（建连即发 welcome#1）+ `lobby.py:1255`（hello 校验通过后再发 welcome#2）。
- 影响：每次加入客机起 2 个心跳线程（叠加 M-1 泄漏）；密钥重复派生本身幂等无直接危害，但放大了 S-1 的排障噪音。
- 修复：welcome 只发一次（建连时，这是 client 派生 key 的唯一 nonce 来源），lobby 侧删掉第二次：

```python
# lobby.py process_message 的 "welcome" 分支保持不变（两次都会走同一逻辑，幂等），
# 但 on_hello 中删除第二份 welcome：
#   network._send_json(sock, {"type": "welcome", "player_id": sender_pid, ...})  ← 删除
# on_hello 里只保留 lobby + world_snapshot：
    network._send_json(sock, {"type": "lobby", "members": list(_members.values()), ...})
    _send_world_snapshot(sock)
```

### N-3【P0】HOST_ONLY_TYPES 遗漏 3 类主机下行消息

- 位置：`network.py:90-95`。`lobby`、`travel_req`、`save_resend_req` 不在名单。
- 影响：恶意客机可发 `lobby` 直接毒化主机成员表（伪造全员 ready / 抹掉成员，见 S-5）、发 `travel_req` 干扰旅行状态机、发 `save_resend_req` 触发 S-6 任意文件读。
- 修复（白名单补齐 + handler 自卫双保险，后者见 S-5/M-12）：

```python
HOST_ONLY_TYPES = frozenset([
    "welcome", "kicked", "start_game", "clock", "save_sync_req",
    "save_chunk", "save_chunk_done", "save_sync_done", "travel_go",
    "travel_all_arrived", "travel_missing", "host_migrated",
    "version_mismatch", "join_rejected", "world_snapshot",
    "lobby", "travel_req", "save_resend_req",   # v9.23: 补齐遗漏
])
```

### N-4【P2】无客机时自连接被放行

- 位置：`network.py:1141-1148`——`if addr[0] == "127.0.0.1" and not ALLOW_SELF: if len(_clients) > 0: close`。
- 影响：条件写反了语义——只有"已有客机"才拒绝自连接；空房间时本机连自己会被接纳，产生幽灵成员。
- 修复：

```python
if addr[0] == "127.0.0.1" and not ALLOW_SELF:
    _log("self-connection rejected (set SIMSYNC_ALLOW_SELF=1 for virtual tests)")
    try:
        conn.close()
    except Exception:
        pass
    continue
```

### N-5【P2】未认证帧仍走 pickle.loads（白名单收窄但未根除）

- 位置：`network.py:945-958`。规范 §2.2 已声明此为 v2 结构性限制，根治在协议 v3。
- 缓解修复（未认证阶段先试 JSON，仅 JSON 解析失败才落到 pickle——把"预期类型"帧的反序列化面从任意 pickle 缩到 JSON）：

```python
if _key is None:
    _allowed = _PRE_AUTH_CLIENT_TYPES if is_client else _PRE_AUTH_HOST_TYPES
    if frame_len > _PRE_AUTH_MAX_FRAME:
        _log("pre-auth frame dropped ({}B)".format(frame_len))
        continue
    # v9.23: 未认证阶段优先按 JSON 解析（hello/welcome 等白名单消息字段全是标量，
    # JSON 可表达；pickle 仅作为旧对端兼容回退）
    msg = None
    try:
        msg = json.loads(frame_data.decode("utf-8"))
    except Exception:
        msg = pickle.loads(frame_data)     # 兼容仍发 pickle 握手帧的旧端
    _t = msg.get("type") if isinstance(msg, dict) else None
    if _t not in _allowed:
        _log("pre-auth frame dropped (type={})".format(_t))
        continue
```

### N-6【P2】UDP 发现的启停标志 host/client 共用

- 位置：`network.py:819/853`——`_discovery_running` 同一全局变量。
- 影响：先 `start_client_discovery()` 再 `_start_host_discovery()`（或反之）时第二个直接 return，广播/监听缺失。
- 修复：拆成 `_disc_tx_running` / `_disc_rx_running` 两个标志（改动机械，略）。

### N-7【P2】`_discovered_rooms` 无容量与过期清理

- 位置：`network.py:896`——读取时 20s TTL 过滤，但字典本身只增不减。
- 修复：

```python
def get_discovered_rooms():
    now = time.time()
    for ip in list(_discovered_rooms.keys()):          # v9.23: 顺手清理过期项
        if now - _discovered_rooms[ip].get("ts", 0) >= 20:
            _discovered_rooms.pop(ip, None)
    return {ip: v for ip, v in _discovered_rooms.items() if now - v.get("ts", 0) < 20}
```

### N-8【P1】mood_sync：房主的心情永远不广播

- 位置：`mood_sync.py:113`——`if mood is not None and network._client_socket is not None:`。
- 影响：房主进程的 `_client_socket` 恒为 None（房主是被连接方），条件永假 → 房主 mood 从不同步，客机只能互相同步心情。
- 修复：

```python
# mood_sync.py _mood_loop 中：
-            if mood is not None and network._client_socket is not None:
+            can_send = (network._is_host and len(network._clients) > 0) or \
+                       (not network._is_host and network._client_socket is not None)
+            if mood is not None and can_send:
```

### N-9【P2】sim_pos 增量量化误差无限累加，无绝对坐标重同步

- 位置：`sync.py:308-320`（`delta_q` 发送端）/ 接收端累加逻辑。delta 量化到 0.01m，每帧最多 0.005m 舍入误差，0.2s 一帧时理论上每分钟最多漂移 ~1.5m。
- 影响：长时间联机后远端 sim 位置逐渐偏离真实位置；旅行后仅靠 world_snapshot 重置一次。
- 修复（每 150 帧或对端请求时发绝对坐标；完整纠偏机制在 v9.70 反作弊的 correction_req 一并实现）：

```python
# sync.py 发送侧，payload 构造处：
global _pos_seq, _pos_frame_count
_pos_frame_count = getattr(sys.modules[__name__], "_pos_frame_count", 0) + 1
force_absolute = (_pos_frame_count % 150 == 0)   # v9.23: 每 150 帧绝对重同步
if _last_broadcast_pos is None or _pos_seq > 1 and not force_absolute:
    ...  # delta 模式
else:
    ...  # 绝对模式（首包/重置/周期重同步共用此分支）
```

---

## B. lobby.py（房间/大厅逻辑层）

### S-1【P0】私密房 HMAC 会话密钥派生不对称——握手后所有帧互丢

- 位置：`lobby.py:1247`（host 用真实 `ROOM_PASSWORD` 派生）vs `lobby.py:1303`（client 用本模块全局 `ROOM_PASSWORD`，客机上恒为 `""`）。
- 触发：房主建带密码的私密房，客机输入正确密码加入。双方各自派生出**不同** key → welcome 之后每帧 HMAC mismatch 被静默丢弃——位置/金钱/聊天全部失效，无任何报错。
- 修复：client 侧必须用**自己握手中实际发送的密码**派生（即启动器配置里的密码，与 hello 中 `password` 字段同源）：

```python
# lobby.py process_message 的 "welcome" 分支（约 L1293-1330）内：
-        network._hmac_keys[network._my_player_id] = network._derive_hmac_key(
-            ROOM_PASSWORD, c_nonce, h_nonce)
+        # v9.23 S-1 修复: 客机用握手时实际发送的密码派生（hello 的 password
+        # 与此必须同源——都来自 mp_launcher_config.json），而非本模块全局
+        # ROOM_PASSWORD（该全局只在房主路径赋值，客机恒为空串）
+        _pw = ""
+        try:
+            _cfg = network._load_launcher_config() or {}
+            _pw = _cfg.get("password", "")
+        except Exception:
+            pass
+        network._hmac_keys[network._my_player_id] = network._derive_hmac_key(
+            _pw, c_nonce, h_nonce)
```

### S-2【P0】自动重连的 hello 缺 `client_nonce`、密码硬编码空串

- 位置：`lobby.py:591-593`。
- 影响：重连后主机不派生新 key（旧 key 已在断开时清理）→ 客机对主机帧验签用旧 nonce 派生的 key → 主机→客机所有帧被拒（"重连上了但不同步"）；私密房则直接 `join_rejected`。
- 修复：重连握手与首次连接使用同一套完整字段：

```python
# lobby.py schedule_reconnect_with_backoff 的重连循环内（约 L588-594）：
-                    net._send_json(sock, {"type": "hello", "name": _get_player_name(),
-                                          "password": "",
-                                          "proto_version": net.PROTO_VERSION})
+                    # v9.23 S-2 修复: 重连握手必须带 client_nonce + 真实密码，
+                    # 与 network._client_thread 的首次 hello 完全一致
+                    net._my_nonce = net._gen_nonce()
+                    _pw = ""
+                    try:
+                        _cfg = net._load_launcher_config() or {}
+                        _pw = _cfg.get("password", "")
+                    except Exception:
+                        pass
+                    net._send_json(sock, {
+                        "type": "hello", "name": _get_player_name(),
+                        "password": _pw,
+                        "proto_version": net.PROTO_VERSION,
+                        "client_nonce": net._my_nonce.decode("utf-8")}, prio=0)
```

### S-3【P0】主机迁移不清理旧客户端时代的网络残留

- 位置：`lobby.py:682-726`（`_become_new_host`）。
- 影响：竞选赢家（通常是 pid=1）残留 `_hmac_keys[1]` 旧 key；新主机分配 pid=1 给首个重连者时，用旧 key 验其无签名 hello → 静默丢弃 → 该客机永远完不成握手。`_client_socket` 残留还会吞掉新成员的 welcome 分支。
- 修复：

```python
# _become_new_host() 启动服务线程之前追加：
    import network as net
    with net._clients_lock:
        net._clients.clear()
        net._sock_pid_index.clear()
        net._hmac_keys.clear()          # v9.23 S-3: 旧客机时代的会话密钥必须清空
        net._conn_nonces.clear()
        net._released_pids = []
        old_cli = net._client_socket
        net._client_socket = None       # 客机残留连接引用
    if old_cli is not None:
        try:
            old_cli.close()
        except Exception:
            pass
    net._next_player_id = 1
```

### S-4【P0】迁移后非赢家重连目标仍是旧主机 IP

- 位置：`lobby.py:739-740`（`_schedule_reconnect` 用 `_last_known_host_ip`——旧主机）。
- 影响：迁移成功的标志事件（新房主已监听）永远不会被其他客机连上 → 反复退避 → 再次触发竞选 → 死循环。
- 修复：竞选中把赢家地址写进 claim，非赢家从 claim 读新主机 IP：

```python
# claim 写入处（on_host_disconnect 内）追加自己的候选地址：
claim = {"pid": network._my_player_id,
         "name": _get_player_name(),
         "ts": time.time(),
         "addrs": _collect_local_addrs()}      # v9.23 S-4: 带上本机地址

# 结算处（_become_new_host 赢家判定后）读赢家 claim：
winner = min(candidates)
winner_addrs = claims[winner].get("addrs") or []
# 赢家自己在 start() 后应立即开 UDP 发现广播（已有 _start_host_discovery），
# 非赢家 _schedule_reconnect 改为：
host_ip = winner_addrs[0] if winner_addrs else (_last_known_host_ip or "")
# 并在 connect 失败 3 次后调用 network.get_discovered_rooms() 按 room_code
# 找新房主（UDP 发现代码已存在，补一个按房间码匹配的分支）
```

### S-5【P0】客机可下发 `lobby` 毒化主机成员表

- 位置：`lobby.py:1332-1344`（handler 无来源自卫）+ N-3（白名单遗漏）。
- 影响：一帧伪造 `lobby`（全员 ready=True）即可绕过存档同步与开始门槛，或把任意成员从表中抹掉。
- 修复（白名单见 N-3；handler 侧再加一层防御纵深）：

```python
# lobby.py process_message：
     if mtype == "lobby":
+        if network._is_host:            # v9.23 S-5: host 不接受下行 lobby
+            network._log("dropped lobby msg on host side")
+            return
         _members = {}
         for m in _members_list:
-            _members[int(m["player_id"])] = m
+            try:                        # v9.23: 单条损坏不再整体清零
+                _members[int(m["player_id"])] = m
+            except (KeyError, TypeError, ValueError):
+                continue
```

### S-6【P0】`save_resend_req` 路径穿越——任意文件读取并广播外泄

- 位置：`lobby.py:394-415`（`os.path.join(SAVES_DIR, filename)`，filename 来自网络）。
- 影响：客机发 `filename="..\\..\\secret.txt"` → 主机读出内容 base64 广播给所有客机。
- 修复（与 S-7 共用一个文件名净化函数）：

```python
# lobby.py 模块级新增：
import re
_SAVE_NAME_RE = re.compile(r"^Slot_\d{8}\.save$")

def _safe_save_name(filename):
    """v9.23 S-6/S-7: 存档文件名白名单——只接受 Slot_XXXXXXXX.save，
    且 basename 校验防穿越（os.path.join 不消解 '..'）"""
    if not isinstance(filename, str):
        return None
    base = os.path.basename(filename.replace("\\", "/"))
    if base != filename or not _SAVE_NAME_RE.match(base):
        network._log("rejected unsafe save filename: {!r}".format(filename))
        return None
    return base

# _resend_save_chunks（L398 附近）与 on_save_chunk/on_save_chunk_done（L488 附近）：
filename = _safe_save_name(data.get("filename"))
if filename is None:
    return
```

### S-7【P0】`save_chunk` 路径穿越——恶意主机可写客机任意路径

- 位置：`lobby.py:488-492`（`dest = os.path.join(SAVES_DIR, filename)` + 写盘）。
- 影响：加入恶意主机的客机被写任意文件（攻击面：公开房 + UDP 自动发现 = 零门槛成为主机）。
- 修复：同 S-6 的 `_safe_save_name`，并加 realpath 双保险：

```python
filename = _safe_save_name(data.get("filename"))
if filename is None:
    return
dest = os.path.join(SAVES_DIR, filename)
if os.path.realpath(dest)[:len(os.path.realpath(SAVES_DIR)) + 1] \
        != os.path.realpath(SAVES_DIR) + os.sep:      # v9.23: realpath 限定目录
    return
```

### S-8【P1】密码/版本被拒后连接不断开、无失败限制——可无限暴力猜密码

- 位置：`lobby.py:1224-1237`（拒绝仅回消息即 return）。
- 修复：

```python
# lobby.py 模块级：
_hello_fail_counts = {}          # sender_pid -> 次数
_HELLO_FAIL_LIMIT = 5

# process_message 的 "hello" 分支，两个拒绝点统一改为：
def _reject_hello(sender_pid, payload):
    network._log("lobby: hello rejected from pid={}".format(sender_pid))
    entry = network._clients.get(sender_pid)
    if entry is not None:
        network._send_json(entry[0], payload)
    _hello_fail_counts[sender_pid] = _hello_fail_counts.get(sender_pid, 0) + 1
    if _hello_fail_counts[sender_pid] >= _HELLO_FAIL_LIMIT:
        network._log("lobby: too many rejected hellos, closing pid={}".format(sender_pid))
        if entry is not None:
            try:
                entry[0].close()          # recv 线程会走统一清理路径
            except Exception:
                pass
# hello 校验通过处：_hello_fail_counts.pop(sender_pid, None)
```

### H-1【P1】存档缺块重传是死局 + 计数 off-by-one + 只补前 20 块

- 位置：`lobby.py:451-463`（客户端请求后 return）、`lobby.py:394-415`（重发后不补 `save_chunk_done`）、`lobby.py:456-457`（先自增再读，实际最多重传 2 次）、`missing[:20]`。
- 影响：任何一块丢失 → 重传补齐后无人触发拼装写盘 → 存档同步永久卡死。
- 修复（两端）：

```python
# ① 主机 _resend_save_chunks 末尾补发收尾（带 sha256）：
    network._send_json(sock, {"type": "save_chunk_done", "filename": filename,
                              "total_bytes": total_bytes, "sha256": _sha})
# ② 客户端 on_save_chunk 里，收到任何块后先自检是否已齐：
def on_save_chunk(data):
    ...
    cache["chunks"][index] = chunk
    if len(cache["chunks"]) >= cache["total"]:
        _assemble_and_write(filename, cache)     # v9.23: 收齐即收尾，不等 done
    # save_chunk_done 到达时也调用同一 _assemble_and_write（幂等：写盘前查已写标记）
# ③ 计数修正：
retries = cache.get("retries", 0)
cache["retries"] = retries + 1
if retries < 3:
    ...
    missing_all = sorted(set(range(total)) - set(cache["chunks"]))
    network._send_json(network._client_socket, {
        "type": "save_resend_req", "filename": filename,
        "missing": missing_all, "total": total})   # v9.23: 不再截断 [:20]
```

### H-2【P1】主机"发送完成"即授予开始权，不等客机校验/写盘

- 位置：`lobby.py:1103-1116`。
- 修复：改 waiting_ack 状态机（消息字段 `save_sync_ack {ok, filename}` 已存在，客机 L502 已在发——主机侧只需真的等）：

```python
# host_start_save_sync 尾部改为：
_save_sync_phase = "waiting_ack"
_save_sync_acks = set()
_broadcast_state()

# 新增（或复用）ack 收集器，process_message 的 "save_sync_ack" 分支：
def on_save_sync_ack(sender_pid, data):
    global _save_sync_phase, _start_granted
    if _save_sync_phase != "waiting_ack":
        return
    if data.get("ok") is False:
        _notify("客机 {} 存档校验失败，已回滚".format(sender_pid))
        _save_sync_phase = "idle"                  # 回滚，允许重发
        _broadcast_state()
        return
    _save_sync_acks.add(sender_pid)
    alive = [pid for pid, m in _members.items()
             if not m.get("is_host") and m.get("online", True)]
    if set(alive) and _save_sync_acks >= set(alive):
        _save_sync_phase = "done"
        _start_granted = True
        network._broadcast({"type": "save_sync_done"})
        _broadcast_state()
```

### H-3【P1】`_travel_force_start` check-then-act 竞态；Timer 线程直呼游戏 UI

- 位置：`lobby.py:900-921`；调用源 10s Timer 线程与主线程 `on_travel_ack` 并发。
- 修复：旅行状态代次 + 锁（与 H-4 同一补丁，见下）。

### H-4【P1】旅行 Timer 句柄被丢弃——旧旅行定时器打断新旅行

- 位置：`lobby.py:885、918-920`。
- 修复（H-3/H-4 合并：代次 + 句柄保存 + UI 调用回主线程）：

```python
# lobby.py 模块级：
_travel_lock = threading.Lock()
_travel_gen = 0                       # 旅行代次
_travel_timers = []                   # 活跃 Timer 句柄

def _cancel_travel_timers():
    for t in _travel_timers:
        try:
            t.cancel()
        except Exception:
            pass
    del _travel_timers[:]

def _travel_force_start(gen=None):
    _notify_q = []
    with _travel_lock:
        if gen is not None and gen != _travel_gen:
            return                    # v9.23 H-4: 旧代次定时器作废
        global _travel_pending, _travel_active
        if not _travel_pending:
            return
        _travel_pending = False
        _travel_active = True
        ...
    _notify_q_run_on_main(...)        # 见 H-10 的通知队列
```

```python
# host_start_travel 开头：
with _travel_lock:
    _travel_gen += 1
    gen = _travel_gen
    _cancel_travel_timers()           # v9.23 H-4: 取消上一轮全部定时器
    _travel_pending = True
    _travel_acks = {0}
network._broadcast({"type": "travel_req", "ts": time.time()})
t1 = threading.Timer(10.0, _travel_force_start, args=(gen,)); t1.start()
_travel_timers.append(t1)
# 30/60/90s 定时器同理带 gen 参数，_travel_progress_check 校验代次
```

### H-5【P1】`promote_member` 后原房主被心跳检查误标掉线

- 位置：`lobby.py:820`（`_members[0]["is_host"] = False`）+ `lobby.py:288-296`（心跳检查跳过 `is_host` 成员）。
- 修复（最小）：心跳检查跳过条件改为"本机自己"，promote 的完整语义留给 v9.40 权限分级：

```python
# check_heartbeats 内：
-        if m.get("is_host"):
+        if pid == 0 or pid == network._my_player_id:   # v9.23 H-5: 按 pid 跳过本机/房主位
             continue
```

### H-6【P1】`kick_member` 不关闭被踢者连接——踢人形同虚设

- 位置：`lobby.py:793-809`。
- 修复：

```python
    if player_id in network._clients:
        sock = network._clients[player_id][0]
        network._send_json(sock, {"type": "kicked", "reason": "房主将你移出房间"})
        try:
            sock.close()                      # v9.23 H-6: 真正断开
        except Exception:
            pass
        with network._clients_lock:
            network._clients.pop(player_id, None)
            network._sock_pid_index.pop(id(sock), None)
            network._hmac_keys.pop(player_id, None)
            network._conn_nonces.pop(player_id, None)
        network._release_pid(player_id)
    _members.pop(player_id, None)
    _broadcast_state()
```

### H-7【P2】hello 处理中 `network._clients[sender_pid][0]` 无守卫

- 位置：`lobby.py:1227、1235`。消息排队期间连接断开 → KeyError 被吞（S-8 修复代码中的 `_reject_hello` 已内含 `.get` 守卫；version_mismatch 分支同样改 `.get`）。

### H-8【P1】`host_start_save_sync` / `host_start_game` 无角色自卫，启动器命令桥可让客机执行主机流程

- 位置：`lobby.py:1086、1148`；入口 `network.py:565-570`（`_check_launcher_cmd` 无检查调用）。
- 影响：客机误点启动器"同步存档"→ 客机向主机灌存档帧 + 本地假状态 done。
- 修复：

```python
def host_start_save_sync(filename=None):
    global _save_sync_phase, _save_sync_acks
    if not network._is_host:            # v9.23 H-8: 命令桥也走角色校验
        _notify("只有房主可以同步存档")
        return False
    ...

def host_start_game():
    if not network._is_host:
        _notify("只有房主可以开始游戏")
        return False
    ...
```

### H-9【P1】`_members` 多线程无锁并发读写

- 位置：定义 `lobby.py:42`；跨线程写点：recv 线程（L228）、Timer 线程（L905-911）、settle 线程（L692-703）；读点：`_broadcast_state`（L191）等。
- 影响：`dictionary changed size during iteration` 被吞 → 状态广播丢失、各端房间视图分叉。
- 修复：模块级成员锁 + 全部读写持锁快照：

```python
_members_lock = threading.Lock()

def _members_snapshot():
    with _members_lock:
        return list(_members.values())

# _broadcast_state / _write_state_file 里 list(_members.values()) 全部换 _members_snapshot()；
# check_heartbeats / on_client_disconnect / on_hello 等改写处用 with _members_lock: 包裹。
```

### H-10【P1】`_notify`（游戏 UI API）从工作线程直呼

- 位置：`lobby.py:588、640、676、719、912、997-1003、227`（reconnect/settle/Timer/recv 线程）。
- 修复：通知队列，主线程 alarm/on_tick 冲刷：

```python
# network.py 新增：
_notify_q = queue.Queue()

def notify_async(text):
    """线程安全的游戏内通知（入队，由主线程冲刷）"""
    _notify_q.put(text)

def _flush_notify():
    while True:
        try:
            text = _notify_q.get_nowait()
        except queue.Empty:
            break
        _notify(text)

# _process_alarm_callback / _on_tick_callback 里追加 _flush_notify()
# lobby.py 所有工作线程里的 _notify(...) 改为 network.notify_async(...)
```

### M-1【P1】每次 welcome 新起一个永不退出的心跳线程

- 位置：`lobby.py:1321`（welcome handler 调 `start_heartbeat()`）+ L306-317（`while True` 无停止条件）；welcome 双发（N-2）使首次加入即起 2 个。
- 修复（N-2 去掉双发后仍需防重入）：

```python
_heartbeat_running = False

def start_heartbeat():
    global _heartbeat_running
    if _heartbeat_running:              # v9.23 M-1: 防重入
        return
    _heartbeat_running = True
    def _loop():
        while _heartbeat_running:
            ...
    threading.Thread(target=_loop, daemon=True).start()
```

### M-2【P2】主机迁移竞选可脑裂

- 位置：`lobby.py:623-638、669`。各客机本地 `_members` 视图不一致 → `min(candidates)` 选出不同赢家。
- 修复方向：lobby 广播带 `generation`（v10 `RoomSnapshot.generation` 已定义），结算前校验各 claim 携带的 generation 一致；不一致延长 settle 等待。属 v9.40 范畴，当前先在 claim 中记录本机 generation 供未来校验。

### M-3【P2】`_reconnect_in_progress` 无锁 check-then-set

- 位置：`lobby.py:537-540`。修复：

```python
_reconnect_lock = threading.Lock()
...
    if not _reconnect_lock.acquire(blocking=False):    # v9.23 M-3: 原子占位
        network._log("reconnect: loop already running, skip")
        return
    try:
        ...  # 原 _reconnect_in_progress = True 期间的全部逻辑
    finally:
        _reconnect_lock.release()
```

### M-4【P2】重连循环 connect 失败的 socket 不关闭（FD 泄漏）

- 位置：`lobby.py:582-584、600-601`。修复：`try: ... finally: sock.close()`（connect 抛异常时统一在 finally 关闭）。

### M-5【P1】存档整文件读入内存 + 在游戏主线程同步阻塞发送

- 位置：`lobby.py:357-375`（`f.read()` 全量 + 循环广播，无发送超时）。
- 影响：大存档内存峰值 ~2.7×；慢客机打满 TCP 缓冲时 `sendall` 无限阻塞**游戏主线程**。
- 修复：后台线程 + 流式分块：

```python
def host_send_save_file(filepath, filename):
    def _worker():
        sha = hashlib.sha256()
        total = (os.path.getsize(filepath) + CHUNK - 1) // CHUNK
        with open(filepath, "rb") as f:
            for i in range(total):
                chunk = f.read(CHUNK)                 # v9.23 M-5: 流式读
                sha.update(chunk)
                network._broadcast({"type": "save_chunk", "filename": filename,
                                    "index": i, "total": total,
                                    "data": base64.b64encode(chunk).decode("ascii")})
                time.sleep(0.01)
        network._broadcast({"type": "save_chunk_done", "filename": filename,
                            "total_bytes": os.path.getsize(filepath),
                            "sha256": sha.hexdigest()})
    threading.Thread(target=_worker, daemon=True).start()   # 不阻塞主线程
```

### M-6【P2】`_recv_save_cache` 无上限无过期

- 位置：`lobby.py:390、425-428`。修复：限制文件数 ≤4、总字节 ≤512MB，超限丢最旧；成功/失败收尾后必 pop。

### M-7【P2】`_write_state_file` 每次都 `_collect_local_addrs()`（DNS/UDP 系统调用）

- 位置：`lobby.py:155、106-134`。修复：地址列表缓存 60s（模块级 `_addrs_cache/_addrs_ts`）；`_collect_local_addrs` 内 UDP socket 的异常路径补 `finally: s.close()`。

### M-8【P1】离线成员不移除，同步/开始门槛被卡死；`_save_sync_acks` 不随成员变动校正

- 位置：`lobby.py:281-298、1069-1082、1130-1145`。
- 修复：

```python
def all_clients_ready():
    others = [m for pid, m in _members.items()
              if pid != 0 and m.get("online", True)]    # v9.23 M-8: 排除离线成员
    if not others:
        return False
    return all(m.get("ready") for m in others)

# all_clients_in_lot() 同样加 online 过滤。
# on_client_disconnect 内：_save_sync_acks.discard(player_id)，
# 并在 waiting_ack 阶段有新成员加入时向其补发 save_sync_req。
```

### M-9【P2】`on_hello` 不幂等——重连者 ready/in_lot 被重置

- 位置：`lobby.py:252-264`。修复：pid 已存在时保留原 ready/in_lot 只刷新 name/last_seen；`ip` 改取 `network._clients[pid][1][0]`（socket 对端地址，不信自报）。席位保留机制（v9.40）会替换此补丁。

### M-10【P2】`leave_room`/`reset_room_state` 清理不彻底

- 位置：`lobby.py:767-779、1617-1635`。修复：leave 时关 `_client_socket`、置 `_heartbeat_running=False`、清 `_recv_save_cache`；reset 追加清 `_reconnect_lock` 持有状态、`_chat_history`、travel 定时器（`_cancel_travel_timers()`）。

### M-11【P2】旅行中途加入的成员被算进"全员抵达"

- 位置：`lobby.py:967`。修复：`travel_go` 时快照成员集 `_travel_go_members = set(_members.keys())`，抵达判定 `issuperset(_travel_go_members)`。

### M-12【P0】`travel_missing`/`travel_req` 可由客机下发（提前解锁旅行锁）

- 位置：白名单遗漏（N-3 修复已补）+ handler 自卫。修复（handler 侧）：

```python
def on_travel_missing(data):
    global _travel_active
    if network._is_host:                # v9.23 M-12: 客机不发 travel_missing 到 host
        network._log("dropped travel_missing from client")
        return
    _travel_active = False
    ...
# on_travel_req 同理：if network._is_host: return
```

### M-13【P2】存档备份每次覆盖唯一 `.bak`

- 位置：`lobby.py:489-490`。修复：`dest + ".{:%Y%m%d%H%M%S}.bak".format(time.localtime())`，保留最近 3 份（多余的删除）。

### M-14【P0→缓解】未认证 pickle 反序列化面

- 与 N-5 同一问题（规范 §2.2 已声明），N-5 的 JSON 优先补丁即缓解方案；根治在协议 v3。

### L-1【P2】密码明文比较与传输

- 位置：`lobby.py:79、233-237`。缓解：比较用 `hmac.compare_digest(password, ROOM_PASSWORD)`（防时序侧信道）；传输层加密留给 v3。文档明示"LAN 玩具级安全"。

### L-2【P2】`os.replace` 的 Windows 回退分支是死代码且非原子

- 位置：`lobby.py:173-182`。修复：Python 3.7 的 Windows `os.replace` 本身可用，删除 `os.remove+os.rename` 回退分支。

---

## C. launcher.py / room_protocol.py（启动器层）

### A-1~A-5【P0】类体中 5 个方法重复定义，后者静默覆盖前者——启动器↔mod 桥全部失效

- 位置（死代码版 → 生效版）：
  - `_send_chat`：929-959 → 1218-1226（聊天桥死，本地插入 2s 后被 `_chat_poll` 覆盖消失）
  - `_toggle_ready`：806 → 1383（mod 大厅模式下点击无任何效果）
  - `_leave_room`：811 → 1457
  - `_sync_save`：816 → 1398
  - `_start_game`：827 → 1420（mod 模式下直接拉起游戏进程 → **游戏双开**）
- 根因：v9.18 引入启动器房间模式时新增了同名方法，未合并旧 mod 桥逻辑。
- 修复（统一模式：有 room 对象走协议，否则回退 mod 命令桥；以 `_toggle_ready` 与 `_start_game` 为例，其余同理）：

```python
def _toggle_ready(self):
    if getattr(self, "room_client", None):            # 启动器房间模式
        self.room_client.set_ready(True)
        return
    if getattr(self, "room_server", None):
        self.room_server.host_set_ready(True)
        return
    self._write_mp_cmd("mp_ready")                    # mod 大厅模式（恢复 v9.1 桥）
    self._log("已发送准备命令到游戏")

def _start_game(self):
    if getattr(self, "room_server", None):
        self.room_server.host_start_game(game_port=DEFAULT_PORT)
        self._start_game_after_room()
        return
    if getattr(self, "room_client", None):
        return    # 客机由 start_game 广播触发，不主动启动
    if check_game_running():                          # A-12 修复：防双开
        self._log("游戏已在运行（mod 大厅模式）")
        return
    self._write_mp_cmd("mp_start")
```

> `_send_chat` 的合并版：优先 `room_client`（v9.18 协议无聊天消息则本地显示），
> 否则走原 929 版（写 `chat_cmd.txt` + 本地显示 + 更新 lobby state）。

### A-6【P0】RoomClient 回调在网络线程直接操作 tkinter 控件

- 位置：`launcher.py:1312、1315-1322、1325-1334`。
- 影响：`RuntimeError: main thread is not in main loop` 或 Tcl 崩溃。
- 修复：

```python
# 所有 RoomClient/RoomServer 回调一律调度回主线程：
def _on_save_received(self, data):
    self.after(0, lambda: self._log("📦 存档已接收: {} ({:.1f}MB)".format(
        data.get("filename"), data.get("size", 0) / 1048576.0)))
    self.after(0, lambda: self._apply_received_save(data))
```

### A-7【P2】监控线程读 tkinter StringVar

- 位置：`launcher.py:1686、1695`。修复：主线程定时（如 `_auto_refresh_room` 内）把 `port_var.get()`/`host_ip_var.get()` 快照到 `self._port_snapshot` 普通属性，监控线程只读快照。

### A-8【P1】`check_port_open` 失败分支 socket 泄漏

- 位置：`launcher.py:105-114`。监控线程每 2s 探测不可达对端 → 每次泄漏一个 fd。
- 修复：

```python
def check_port_open(host, port, timeout=1.5):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        return True, "端口开放"
    except Exception as e:
        return False, str(e)
    finally:
        if s is not None:
            try:
                s.close()                     # A-8: 失败路径也关闭
            except Exception:
                pass
```

### A-9【P1】房主同步存档在 GUI 线程同步执行——界面冻结无进度

- 位置：`launcher.py:1407-1415` → `room_protocol.py:250-275`（整读 + 逐块 + 每块 sleep(0.01)，100MB 档 ≈ 16s 白屏）。
- 修复：工作线程 + after 回报进度：

```python
def _sync_save(self):
    ...（角色/模式判定同 A-1 合并版）
    filepath, filename = self.room_server._pick_latest_save()   # 房主侧
    def _worker():
        total = (os.path.getsize(filepath) + SAVE_CHUNK_SIZE - 1) // SAVE_CHUNK_SIZE
        def _prog(i):
            self.after(0, lambda: (self.progress.configure(
                value=100.0 * i / max(1, total)),
                self._log("📦 存档同步 {}/{}".format(i, total))))
        ok, msg = self.room_server.host_start_save_sync(
            filepath, filename, progress_cb=_prog)
        self.after(0, lambda: self._log("✅" if ok else "❌" + msg))
    threading.Thread(target=_worker, daemon=True).start()

# room_protocol.host_start_save_sync 加可选 progress_cb=None 参数，循环内调用。
```

### A-10【P2】加入房间/扫描/UPnP 均阻塞主线程

- 位置：`launcher.py:1336`（connect 8s 超时）、`1352`（discover 2s）、`1516-1540`。修复：同 A-9 模式包 daemon 线程 + `after` 回调。

### A-11【P1】建房/加入失败后 room 对象残留 → 假房主/假成员

- 位置：`launcher.py:1275-1290、1311-1339`（先赋值后失败不回滚）；1276 的 `if not start()` 是死分支（start 抛异常而非返回 False）。
- 修复：

```python
def _create_room(self):
    ...
    srv = RoomServer(host_name=name, port=7660, password=pwd)
    try:
        srv.start()
    except Exception as e:
        self._log("❌ 房间服务启动失败: {}".format(e))
        try:
            srv.stop()
        except Exception:
            pass
        return                       # A-11: 不赋值 self.room_server
    self.room_server = srv
# _join_room 同理：connect 成功且收到 joined 后才赋值（配合 A-13/A-18 的回调）。
```

### A-12【P1】`_start_game_after_room` 不查游戏是否已运行 → 双开

- 位置：`launcher.py:1444-1453`。修复：开头 `if check_game_running(): self._log("游戏已在运行"); return`（A-1 合并版已含）。

### A-13【P1】connect 成功 ≠ 加入成功；被拒后连接不断开不回滚

- 位置：`launcher.py:1336-1340` + `room_protocol.py:414-415`。修复：rejected 回调里 `self.after(0, ...)`：`self.room_client.disconnect(); self.room_client = None` + 错误提示 + 重置房间页。

### A-14【P1】空房间码可绕过校验

- 位置：`room_protocol.py:170-172`——`if code and code != self.room_code`。
- 修复：`if code != self.room_code:`（码必填）。

### A-15【P1】私密房间允许空密码

- 位置：`launcher.py:1272-1275`。修复：选私密且密码为空时拒绝创建并聚焦密码框；密码框加 4-8 位长度校验。

### A-16【P2】`_room_poll` 无限轮询链叠加

- 位置：`launcher.py:1374-1381`（每次建房/加入再起一条 500ms 链，永不停止）。修复：模块级单例链——`self._room_poll_started` 标志，只在首次启动；`_leave_room` 不需要取消（内部已有 room 对象判空）。

### A-17【P2】启动器房间模式下 QR 读错数据源、用错端口

- 位置：`launcher.py:775-780`。修复：`room_code = self.room_server.room_code if self.room_server else state.get("room_code","")`；QR 端口用 `ROOM_PORT (7660)` 而非游戏端口。

### A-18【P1】客户端未注册 `disconnected` 回调——房主退出后成员端 UI 假活

- 位置：`launcher.py:1311-1335`。修复：`self.room_client.on("disconnected", lambda d: self.after(0, self._on_host_room_lost))`，处理器中提示"房主已离开/掉线"、禁用操作按钮、`self.room_client = None`。

### A-19【P2】遍历 `srv.members` 无锁

- 位置：`launcher.py:661-664、1387`。修复：RoomServer 增加 `def snapshot(self): with self._lock: return {"members": [dict(m) for m in self.members.values()], "state": self.state}`，启动器只调 snapshot。

### A-20【P2】`_refresh_room` 直接覆写服务端状态机

- 位置：`launcher.py:565-571`。修复：删除 `self.room_server.state = ROOM_SYNCED` 的外部写入，状态流转由 RoomServer 依据 ack 自治（配合 C 端 H-2 的 ack 状态机）。

### A-21【P2】离开房间与 game_start 广播竞态

- 位置：`launcher.py:1457-1474` vs `1315-1322`。修复：`_leave_room` 先置 `self._left_room = True`；`_start_game_after_room` 首行 `if getattr(self, "_left_room", False): return`。

### A-22【P1】SHA256 校验失败被静默吞掉——带坏档开局

- 位置：`room_protocol.py:446-449` emit 无人接；启动器未注册 `save_error`。
- 修复：注册回调并阻断：

```python
self.room_client.on("save_error", lambda d: self.after(0, self._on_save_failed))
def _on_save_failed(self):
    self._log("❌ 存档 SHA256 校验失败，已丢弃，请联系房主重发")
    self._save_blocked = True          # _start_game_after_room 检查后拒绝启动
```

### A-23【P1】坏块静默跳过 → 同步挂死

- 位置：`room_protocol.py:425-434`。修复：done 兜底完整性检查：

```python
elif mtype == "save_sync_done":
    total = getattr(self, "_save_meta", {}).get("total", 0)
    got = len(self._save_chunks)
    if total and got < total:                        # A-23: 兜底自检
        missing = sorted(set(range(total)) - set(self._save_chunks))
        self._send({"type": "save_resend_req", "missing": missing[:64],
                    "total": total})
        self._save_done_pending = True               # 补齐后再组装
        return
    self._assemble_save()
    self._emit("save_done", msg)
# save_chunk 分支里补齐 total 且 _save_done_pending 时调用 _assemble_save()。
# RoomServer._dispatch 增加 "save_resend_req" 处理（当前只处理了 save_sync_ack）。
```

### A-24【P1】客户端写存档文件名未净化 + 游戏运行中覆写

- 位置：`room_protocol.py:458-472`。修复：`filename = os.path.basename(filename)` + `Slot_\d{8}\.save` 白名单 + 写盘前 `check_game_running()` 则提示用户退出后重试（或写暂存区由用户确认）。

### A-25【P2】无成员时同步也"成功"并推进状态

- 位置：`room_protocol.py:250-275`。修复：`if not self._clients: return False, "无成员，无需同步"`。

### A-26【P2】同步期间成员取消准备把状态打回 WAITING

- 位置：`room_protocol.py:210-218` 与 260-273 状态互踩。修复：`_update_state` 在 `state in (ROOM_SYNCING, ROOM_SYNCED)` 时忽略 ready 重算。

### A-27【P1】`_write_mp_cmd` 非原子写，与 mod 500ms 轮询竞争丢命令

- 位置：`launcher.py:838-846`（直接覆盖写）。修复：

```python
def _write_mp_cmd(self, cmd):
    payload = {"ts": time.time(), "cmd": cmd}
    tmp = CMD_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, CMD_PATH)              # A-27: 原子替换，mod 不会读到半截 JSON
```

### A-28【P2】对 `mp_lobby_state.json` 的读-改-写竞争（死代码模式）

- 位置：`launcher.py:944-952`（已被覆盖的死版本）。修复：删除死代码；若恢复聊天功能，改单向 `chat_cmd.txt` 由 mod 回写，杜绝启动器改写 mod 拥有的文件。

### A-29【P2】密码明文落盘（settings.json + mp_launcher_config.json）

- 位置：`launcher.py:1186、1237`。修复：settings 不存密码（每次会话内存传递）；mod 配置文件保留（mod 侧需要），但在设置页明示"密码以明文保存在本机配置中"。

### A-30【P2】端口输入无校验

- 位置：`launcher.py:1230、1686`。修复：输入框 validatecommand 限数字 + 失焦校验 1-65535，错误时红框提示而非笼统"写配置失败"。

### A-31【P2】带宽监控线程永不停止

- 位置：`launcher.py:727-751`。修复：`_leave_room` 与关窗回调（A-34）置 `self._bw_running = False`；`import psutil` 移入 try 并在 ImportError 时安全退出循环。

### A-32【P2】`_auto_minimize` 无限重试链

- 位置：`launcher.py:1476-1486`。修复：计数上限 12 次（1 分钟）后放弃并提示"未检测到游戏进程"。

### A-33【P2】UDP 发现监听器重建竞争

- 位置：`room_protocol.py:510-548`。修复：`RoomServer.stop()` 中保存并 close listener socket（当前 stop 不关它），立即唤醒阻塞的 recvfrom；listener 循环加 room_code 一致性校验（响应前比对当前 `room_server.room_code`）。

### A-34【P2】关窗无清理协议

- 位置：`launcher.py` 无 `WM_DELETE_WINDOW` 处理。修复：

```python
self.protocol("WM_DELETE_WINDOW", self._on_close)
def _on_close(self):
    self._monitor_running = False
    self._bw_running = False
    try:
        if getattr(self, "room_server", None):
            self.room_server.stop()
        if getattr(self, "room_client", None):
            self.room_client.disconnect()
    finally:
        self.destroy()
```

### A-35【P2】ctypes 单实例锁的 GetLastError 不可靠

- 位置：`launcher.py:1756-1769`。修复：`kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)` + `ctypes.get_last_error()`。

### A-36【P2】`_stun_public_ip` 名不副实——把内网 IP 当公网展示

- 位置：`launcher.py:1584-1596`。修复（短期）：文案改为"本机局域网地址（仅同网络可连）"；（v9.90）接入真 STUN（RFC 3489，向 stun.l.google.com:19302 发 Binding Request 解 XOR-MAPPED-ADDRESS）。

### A-37【P2】主题切换只改色表不刷新控件

- 位置：`launcher.py:1558-1564`。修复：维护需刷新控件引用列表，切换时逐个 `configure`；或提示"重启启动器生效"。

### A-38【P2】端口探测对 mod 产生幽灵连接

- 位置：`launcher.py:1688、1697`（每 2s 真实建连即断）。修复：探测连接建立后立即发送一行 `{"type":"probe"}`——mod 侧 `_recv_loop` 收到未知类型只记日志且无会话副作用（现状已如此）；或降频到 10s/仅状态翻转时探测。

### A-39【P2】`_scan_rooms` 用 dict 相等挑"第一条"

- 位置：`launcher.py:1363`。修复：`for i, r in enumerate(rooms): if i == 0: 自动填充`。

### A-40【P2】版本号漂移

- 位置：`launcher.py:27 APP_VERSION="9.22.0"`、docstring "v6.1"、`room_protocol.py` docstring "v9.18"、README 记 v9.22。
- 修复：docstring 统一引用 `APP_VERSION`（`from launcher import APP_VERSION` 或启动时注入），发布清单加"版本一致性检查"（grep 旧版本号）。

### A-41【P2】防火墙规则缺 7660/7661

- 位置：`launcher.py:1502-1506`（只放行 7655）。修复：一键防火墙补 `netsh advfirewall firewall add rule ... localport=7660 protocol=TCP` 与 `localport=7661 protocol=UDP`。

---

## D. 同步模块（sync/stat/mood/money/inventory/relationship/buy/interaction）

除 N-8（mood 房主不广播，P1）外，审查结论：

### D-1【P2】同步模块普遍缺少"本端身份"校验

- 位置：`money_sync.py:70`、`stats_sync.py:121` 等接收端只验 type 不验发送者。
- 影响：与 HOST_ONLY 反向的问题——任何客机都能广播 `money_sync`/`stats_sync` 改所有端的金钱/需求（v9.70 反作弊的经济校验为此设计）。短期修复：客机侧收到**非房主**发来的 `money_sync`（`sender_pid not in (None, 0)`）时忽略并记日志（money 应当只由房主权威广播；互报场景在 v9.70 的收敛模型中解决）。

```python
# money_sync.process_message 开头：
def process_message(data, sender_pid=None):
    if data.get("type") != "money_sync":
        return
    if not network._is_host and sender_pid not in (None, 0):
        network._log("money_sync from non-host pid={}, dropped".format(sender_pid))
        return            # v9.23 D-1: 金钱以房主为权威源
    ...
```
> 注：`_process_incoming` 分发处需把 `sender_pid` 透传（当前多数模块没接）。

### D-2【P2】interaction/inventory/relationship/object_pos 的 ts 仅日志用

- 现状：接收端不校验时间戳与序号（sim_pos 有 seq 但接收端未强制单调）。重放防护归入 v9.70（`MessageEnvelope.seq` 强制单调）；短期不做改动，避免破坏现有互报语义。

---

## 修复优先顺序（建议）

1. **第一批（v9.23，P0 全清）**：S-1、S-2（重连/私密房协议正确性）→ N-3、S-5、S-6、S-7、M-12（安全面）→ A-1~A-6、A-27（启动器桥复活）→ S-3、S-4（迁移）。
2. **第二批（v9.24）**：H-1、H-2（存档同步收尾）→ H-3/H-4、H-9、H-10（线程模型）→ M-1、M-5、M-8、N-2、N-8。
3. **第三批（v9.25+，随 v9.30 里程碑）**：其余 P2 与体验项。
4. 每批修复后跑 `tools/run_all_virtual_tests.py` 全套 + 双机真机回归（真机验证是当前最大空白——虚拟测试 17 套件全绿但真机 0 次）。
