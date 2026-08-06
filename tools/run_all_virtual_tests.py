#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键跑全部虚拟回归测试（Sims4 联机 mod）

用法:
    python tools/run_all_virtual_tests.py          # 跑全部（v92→v97）
    python tools/run_all_virtual_tests.py v94 v95  # 只跑指定套件

返回码: 0 = 全过, 1 = 有失败
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

# 套件顺序 = 依赖顺序（端到端 → 传输层 → 协议语义 → 生命周期 → 纯逻辑 → 内存/GUI）
SUITES = [
    ("virtual_test_v92.py", "端到端 TCP 通信 + 聊天闭环"),
    ("virtual_test_v93.py", "传输层 压力/帧边界/并发"),
    ("virtual_test_v94.py", "协议语义 Delta/重连/心跳/存档/旅行"),
    ("virtual_test_v95.py", "生命周期 房间/迁移/断线重连/缺块重传"),
    ("virtual_test_v96.py", "启动器纯逻辑/消息分发/异常边界"),
    ("virtual_test_v97.py", "内存稳定/GUI 构建/配置往返/fuzz"),
    ("virtual_test_v98.py", "UDP 发现/时钟同步/房间码/大存档"),
    ("virtual_test_v99.py", "同步模块行为/插值数学/设置/AI去重"),
    ("virtual_test_v910.py", "STUN/旅行确认/断点续传/幂等防御"),
    ("virtual_test_v911.py", "场景切换/旅行抵达报告/锁定/超时"),
    ("virtual_test_v912.py", "协议增强: 版本协商/player_id复用/位置量化"),
    ("virtual_test_v913.py", "协议增强: 消息批处理/协议目录"),
    ("virtual_test_v914.py", "连接健康: RTT测量/健康评分/自适应频率"),
    ("virtual_test_v915.py", "协议增强: CRC帧校验/消息优先级"),
    ("virtual_test_v916.py", "跨网安全: HMAC消息签名/握手密钥交换"),
    ("virtual_test_v917.py", "深度同步: 交互队列/背包/关系/Buy家具"),
    ("virtual_test_v918.py", "启动器房间: 建房/加入/准备/存档同步/开始游戏"),
]


def run_suite(name, label, timeout=120):
    print("\n" + "=" * 60)
    print("▶ {} ({})".format(label, name))
    print("=" * 60)
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(TOOLS, name)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("  ⚠ 超时 ({}s)——可能卡在无限循环函数，检查测试是否起线程+定时停止".format(timeout))
        return False
    dt = time.time() - t0
    # 输出关键行（结果 + 失败项）
    for line in (r.stdout or "").splitlines():
        if "结果:" in line or "❌" in line or "[A]" in line or "[B]" in line or "[C]" in line or "[D]" in line or "[E]" in line:
            print("  " + line)
    if r.returncode != 0:
        print("  ✗ 失败 ({}s, rc={})".format(round(dt, 1), r.returncode))
        return False
    print("  ✓ 通过 ({}s)".format(round(dt, 1)))
    return True


def main():
    args = sys.argv[1:]
    if args:
        suites = [s for s in SUITES if any(a in s[0] for a in args)]
        if not suites:
            print("未匹配到套件。可用: {}".format(" ".join(s[0] for s in SUITES)))
            return 1
    else:
        suites = SUITES

    print("Sims4 联机 mod 虚拟回归测试（{} 套件）".format(len(suites)))
    print("项目根: {}".format(ROOT))
    print("注意: 需要 python 环境能 import socket/pickle/struct（无需游戏 mod）")

    # 清理状态文件（防串）
    state = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts",
                         "The Sims 4", "Mods", "mp_lobby_state.json")
    if os.path.exists(state):
        try:
            os.remove(state)
            print("\n已清理状态文件: {}".format(state))
        except OSError:
            pass

    results = []
    for name, label in suites:
        results.append((name, run_suite(name, label)))

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    ok = 0
    for name, passed in results:
        print("  {} {}".format("✓" if passed else "✗", name))
        if passed:
            ok += 1
    print("\n结果: {}/{} 套件通过".format(ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
