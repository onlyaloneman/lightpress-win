# -*- coding: utf-8 -*-
"""生成应用图标 app.ico（圆角渐变底 + 双向压缩箭头）"""
from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# 圆角矩形 + 竖向渐变
grad = Image.new("RGB", (1, S))
top, bot = (0x4E, 0x7C, 0xFF), (0x2A, 0x4F, 0xD6)
for y in range(S):
    t = y / (S - 1)
    grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
grad = grad.resize((S, S))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=112, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
cx, cy = S / 2, S / 2
white = (255, 255, 255, 255)

# 上下两支箭头对压（箭杆 + 箭头），中间留出压缩间隙
shaft_w = 46
head_w = 132
head_h = 74
shaft_len = 86
gap = 30

# 上箭头：杆在上，头朝下
top_shaft = [cx - shaft_w, cy - gap - head_h - shaft_len, cx + shaft_w, cy - gap - head_h]
d.rounded_rectangle(top_shaft, radius=shaft_w // 2, fill=white)
d.polygon([(cx - head_w / 2, cy - gap - head_h), (cx + head_w / 2, cy - gap - head_h),
           (cx, cy - gap)], fill=white)

# 下箭头：杆在下，头朝上
bot_shaft = [cx - shaft_w, cy + gap + head_h, cx + shaft_w, cy + gap + head_h + shaft_len]
d.rounded_rectangle(bot_shaft, radius=shaft_w // 2, fill=white)
d.polygon([(cx - head_w / 2, cy + gap + head_h), (cx + head_w / 2, cy + gap + head_h),
           (cx, cy + gap)], fill=white)

img.save("app_icon_512.png")
img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon ok")
