# -*- coding: utf-8 -*-
"""生成启动器深色渐变背景（游戏风格：深炭黑 + 霓虹绿/电光蓝光晕）"""
from PIL import Image, ImageDraw

W, H = 960, 700

# 基础深色
img = Image.new("RGBA", (W, H), (13, 13, 13, 255))
d = ImageDraw.Draw(img)

# 左上角霓虹绿光晕
for i in range(120, 0, -1):
    alpha = int(18 * (1 - i / 120))
    x0, y0 = -100 + i * 4, -100 + i * 4
    d.ellipse([x0, y0, x0 + 320, y0 + 320], fill=(0, 255, 133, alpha))

# 右下角电光蓝光晕
for i in range(140, 0, -1):
    alpha = int(16 * (1 - i / 140))
    x0, y0 = W - 220 + i * 4, H - 220 + i * 4
    d.ellipse([x0, y0, x0 + 360, y0 + 360], fill=(30, 144, 255, alpha))

# 顶部细霓虹线（科技感）
d.rectangle([0, 0, W, 2], fill=(0, 255, 133, 200))

# 网格点阵（游戏 HUD 感）
for x in range(0, W, 48):
    for y in range(0, H, 48):
        d.ellipse([x, y, x+2, y+2], fill=(255, 255, 255, 14))

img.save(r"D:\Sims4-Multiplayer-Dev\assets\bg_dark.png")
print("背景已生成: assets/bg_dark.png", img.size)
