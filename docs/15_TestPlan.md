# 15 — 测试计划

> SimSync 测试策略：虚拟测试为主（mock 游戏 API）+ 真机验证为辅

## 测试金字塔

```
        ╱ 真机验证 ╲          ← 双机实测（2 台电脑 LAN）
       ╱─────────────╲
      ╱  虚拟测试 16 套 ╲       ← mock 游戏 API，不依赖游戏
     ╱───────────────────╲
    ╱  单元测试（模块级）    ╲     ← 测试单个函数/类
   ╱─────────────────────────╲
```

## 虚拟测试套件（16 套，260+ 断言）

| 套件 | 版本 | 测试内容 | 断言 |
|:-----|:-----|:---------|:----:|
| v92 | 初始协议 | 连接/断开/消息收发/心跳 | 15 |
| v93 | 帧协议 | CRC 校验/自定义帧格式/长度编码 | 12 |
| v94 | 局域网发现 | UDP 广播/房间发现/响应 | 10 |
| v95 | 位置同步 | Sim 位置读写/插值/广播 | 18 |
| v96 | 需求同步 | 需求值读写/阈值过滤/双向修正 | 14 |
| v97 | 心情同步 | 心情读取/序列化/广播 | 8 |
| v98 | 金钱同步 | 家庭资金读写/广播 | 10 |
| v99 | 时钟同步 | 时间速度/暂停/广播 | 12 |
| v910 | 大厅管理 | 成员列表/房间码/准备状态 | 16 |
| v911 | 聊天系统 | 消息收发/历史记录 | 10 |
| v912 | 优先级队列 | 3 级优先级/批处理/顺序 | 14 |
| v913 | 协议目录 | 消息类型覆盖/CRC 校验/分发 | 14 |
| v914 | 帧协议升级 | 12 字节头/3 级优先级通道 | 14 |
| v915 | 存档同步 | 文件分块/SHA256/缺块重传 | 18 |
| v916 | HMAC 安全 | 消息签名/密钥交换/防重放 | 22 |
| v917 | 深度同步 | 交互队列/背包/关系/Buy 家具 | 31 |
| v918 | 启动器房间 | 建房/加入/准备/存档同步/100 轮循环 | 23 |

**总计**：260+ 断言，全部 mock 模式（不依赖游戏/S4MP，纯 Python 标准库）

### 运行方式

```bash
# 单个套件
python tools/virtual_test_v917.py

# 全部套件
python tools/run_all_virtual_tests.py
```

### 测试原则

1. **不依赖游戏进程**：所有 Sim/Object/Manager 用 Fake 实现（mock 模式）
2. **不依赖网络**：使用 localhost TCP 模拟真实连接
3. **真实协议**：测试走完整的帧协议（CRC+HMAC），不是 mock 网络层
4. **100 轮压力**：启动器房间测试包含 100 次建房-加入-离开循环
5. **回归保护**：每个新版本加一个测试套件，历史套件不过不发版

## 真机验证（待做）

| 测试 | 内容 | 验收标准 |
|:-----|:-----|:---------|
| 双机连接 | 两台电脑 LAN 连接 | 双方看到对方加入 |
| 位置同步 | 一方移动，另一方看到 | 位置误差 < 0.5 格 |
| 交互同步 | 一方做饭，另一方看到 | 对端小人执行相同交互 |
| 背包同步 | 一方给物品，另一方收到 | 双方背包一致 |
| 关系同步 | 一方提升关系，另一方看到 | 关系分数一致 |
| 存档同步 | 同步存档后开始游戏 | SHA256 一致，游戏正常 |
| 启动器房间 | 建房→加入→准备→同步存档→开始游戏 | 完整流程无卡顿 |
| 断线重连 | 拔网线 30s 后恢复 | 自动重连 + 状态对齐 |

## 已知测试盲区

- **push_super_affordance 真机执行**：虚拟测试只验证了 API 存在，真机执行效果待验证
- **player_try_add_object 真机写入**：FakeInventory 模拟了 append，真机是否写入成功待验证
- **set_relationship_score 双向一致性**：两端关系是否同步更新待验证
- **set_location 家具 EndLocation**：Buy 模式下的坐标变换是否准确待验证

## Mock 基础设施

```python
# tools/virtual_test_v917.py 中的 Fake 实现
class FakeSim:
    def push_super_affordance(self, *args): pass
    def __init__(self, sim_id): self.id = sim_id; self.inventory_component = FakeInventory()

class FakeInventory:
    def __init__(self): self._items = []
    def player_try_add_object(self, obj): self._items.append(obj)
    def try_remove_object_by_id(self, obj_id): ...

class FakeSimInfo:
    def get_sim_instance(self): return self._sim or FakeSim(self.id)

class FakeRelationshipTracker:
    def __init__(self): self._scores = {}; self._bits = {}
    def get_relationship_score(self, sim_id): return self._scores.get(sim_id, 0)
    def set_relationship_score(self, sim_id, value): self._scores[sim_id] = value
    def add_relationship_bit(self, sim_id, bit, force_add=False): ...
```

## CI/CD（未来）

- GitHub Actions：推送后自动跑全部虚拟测试
- 每夜构建：打包 mod + 启动器
- 发布前检查：全部 16 套件通过 + 版本号统一