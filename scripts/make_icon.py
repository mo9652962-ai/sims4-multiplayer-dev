# -*- coding: utf-8 -*-
"""生成启动器图标：深色背景 + 双小人 + 霓虹绿/电光蓝"""
from PIL import Image, ImageDraw

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 背景：圆角深色方块
d.rounded_rectangle([8, 8, SIZE-8, SIZE-8], radius=48, fill=(13, 13, 13, 255),
                    outline=(0, 255, 133, 255), width=6)

# 左小人（霓虹绿）：头 + 身体
gx, gy = 90, 100
d.ellipse([gx-28, gy-40, gx+28, gy+16], fill=(0, 255, 133, 255))   # 头
d.rounded_rectangle([gx-34, gy+20, gx+34, gy+110], radius=20, fill=(0, 255, 133, 255))  # 身体

# 右小人（电光蓝）：头 + 身体
bx, by = 168, 100
d.ellipse([bx-28, by-40, bx+28, by+16], fill=(30, 144, 255, 255))
d.rounded_rectangle([bx-34, by+20, bx+34, by+110], radius=20, fill=(30, 144, 255, 255))

# 中间的连接线（表示联机）
d.line([gx+30, gy+60, bx-30, by+60], fill=(255, 255, 255, 255), width=8)

# 顶部光环（游戏感）
d.ellipse([SIZE//2-70, 30, SIZE//2+70, 170], outline=(255, 255, 255, 40), width=3)

# 保存多尺寸 ico + png
img.save(r"D:\Sims4-Multiplayer-Dev\assets\launcher_icon.png")
img.save(r"D:\Sims4-Multiplayer-Dev\assets\launcher_icon.ico",
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("图标已生成: assets/launcher_icon.png + .ico")
