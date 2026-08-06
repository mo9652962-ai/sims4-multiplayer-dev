# -*- coding: utf-8 -*-
"""Sims4Multiplayer v9.19 — 本地操作过滤器（参考 S4MP client_config.py LOCAL_ONLY_OPS）

概念（S4MP 反编译确认）:
  LOCAL_ONLY_OPS: 纯本地显示/UI操作，不同步（避免浪费带宽+干扰客机）
  BUY_ALLOWED_CLIENT_OPS: Buy 模式下允许客户端执行的操作
  LOCAL_ONLY_MSG_IDS: 仅本地处理的消息

SimSync 适配:
  我们不走 EA protobuf Operation 枚举（那是引擎内部），而是按语义分类：
  - sync 模块的 position/mood/money/interaction 中，部分事件只需本地生效
  - 在对应模块的 collect/broadcast 处加过滤
"""

# ============ 本地事件列表（不需要同步给客机） ============
# 格式: (模块, 事件类型/方法名) —— 匹配到就跳过广播

LOCAL_ONLY_EVENTS = [
    # 心情同步：Flirty/Playful 等计算型心情不应广播（客机自行计算）
    # 但基础心情（Happy/Sad）可以广播 —— 这里先不加，等实测反馈

    # 交互同步：以下交互类型仅本地生效
    ("interaction", "phone"),         # 手机操作（刷社交/看通知——纯本地）
    ("interaction", "computer"),      # 电脑操作（浏览网页——纯本地）
    ("interaction", "book"),          # 看书（进度本地算）
    ("interaction", "tv"),            # 看电视（频道选择本地）
    ("interaction", "radio"),         # 听音乐

    # v9.20: 从 S4MP client_config LOCAL_ONLY_OPS 完整列表映射（xdis 反汇编 0.74.1）
    # S4MP 原列表: BOOK_VIEW / LIVE_DRAG_START/END/CANCEL / FOCUS / HOVERTIP_CREATED /
    #   MSG_PHONE_MENU_CREATE / MSG_PIE_MENU_CREATE / MSG_SHOW_SIM_PROFILE /
    #   MSG_SHOW_SMALL_BUSINESS_CONFIGURATOR / MSG_MANAGE_EMPLOYEES_DIALOG /
    #   MSG_OBJECT_IS_INTERACTABLE / COMMUNITY_POLICY_BOARD / DYNAMIC_SIGN_VIEW
    # 语义映射（我们按模块+事件分类）：
    ("interaction", "focus"),         # 聚焦/视角切换（纯视觉）
    ("interaction", "drag"),          # 拖拽操作（LIVE_DRAG_START/END/CANCEL——纯视觉）
    ("interaction", "hover"),         # 悬停提示（HOVERTIP_CREATED——纯本地）
    ("interaction", "sim_profile"),   # 查看小人档案（MSG_SHOW_SIM_PROFILE——UI 弹窗）
    ("interaction", "pie_menu"),      # 右键菜单生成（MSG_PIE_MENU_CREATE——纯本地 UI）
    ("interaction", "phone_menu"),    # 手机菜单生成（MSG_PHONE_MENU_CREATE）
    ("interaction", "interactable"),  # 可交互检测（MSG_OBJECT_IS_INTERACTABLE——本地判断）

    # Buy 同步：以下操作仅 BUILD 模式（不做同步）
    # BUILD 模式已经整体跳过，这里是 BUY 模式内的纯本地操作
    ("buy", "wallpaper"),             # 换壁纸（视觉）
    ("buy", "floor"),                 # 换地板（视觉）
    ("buy", "color_swatch"),          # 换颜色（纯视觉）
    ("buy", "drag"),                  # 拖动家具微调（LIVE_DRAG 系列——纯视觉）

    # v9.20: S4MP UI 类本地消息
    ("ui", "business_dialog"),        # 生意管理弹窗（MSG_MANAGE_*_DIALOG——纯本地 UI）
    ("ui", "community_policy"),       # 社区公告板（COMMUNITY_POLICY_BOARD）
    ("ui", "dynamic_sign"),           # 动态标牌（DYNAMIC_SIGN_VIEW）

    # 位置同步：以下情况不广播位置
    ("sync", "sitting"),              # 坐下（位置由家具决定，客机可自行推断）
]

# ============ 本地事件快速查询表 ============
_LOCAL_SET = {event for event in LOCAL_ONLY_EVENTS}


def is_local_only(module, event_type=None):
    """检查是否本地事件（不需要同步）
    
    Args:
        module: 模块名 (如 "interaction", "buy", "sync")
        event_type: 可选的事件子类型 (如 "phone", "computer")
    
    Returns:
        True 如果应该跳过广播
    """
    if (module, event_type) in _LOCAL_SET:
        return True
    if event_type and (module, event_type) in _LOCAL_SET:
        return True
    return False


def filter_local_only(events, module):
    """过滤事件列表，移除纯本地事件
    
    Args:
        events: 事件列表，每项是 (event_type, event_data) 或带 event_type 字段的 dict
        module: 模块名
    
    Returns:
        过滤后的事件列表
    """
    filtered = []
    for evt in events:
        if isinstance(evt, dict):
            etype = evt.get("event_type") or evt.get("type")
        elif isinstance(evt, (tuple, list)) and len(evt) >= 1:
            etype = evt[0] if isinstance(evt[0], str) else None
        else:
            etype = None
        if is_local_only(module, etype):
            continue
        filtered.append(evt)
    return filtered
