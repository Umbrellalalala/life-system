"""生成应用图标 icon.ico（渐变紫圆角底 + 白色勾选）。"""
import os
from PIL import Image, ImageDraw

SIZE = 1024
RADIUS = int(SIZE * 0.22)

# 1. 垂直渐变背景（亮紫 -> 深紫）
top = (150, 134, 255)      # #9686ff
bottom = (104, 82, 235)    # #6852eb
col = Image.new("RGB", (1, SIZE))
for y in range(SIZE):
    t = y / SIZE
    r = int(top[0] + (bottom[0] - top[0]) * t)
    g = int(top[1] + (bottom[1] - top[1]) * t)
    b = int(top[2] + (bottom[2] - top[2]) * t)
    col.putpixel((0, y), (r, g, b))
grad = col.resize((SIZE, SIZE))

# 2. 圆角透明底
icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
mask = Image.new("L", (SIZE, SIZE), 0)
dm = ImageDraw.Draw(mask)
dm.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=RADIUS, fill=255)
icon.paste(grad, (0, 0), mask)

# 3. 白色勾选符号（圆润端点）
d = ImageDraw.Draw(icon)
pts = [(280, 525), (455, 700), (770, 350)]
w = 92
d.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
for p in pts:
    x, y = p
    d.ellipse([x - w // 2, y - w // 2, x + w // 2, y + w // 2],
              fill=(255, 255, 255, 255))

# 4. 输出多尺寸 ICO
out_dir = os.path.join(os.path.dirname(__file__), "assets")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "icon.ico")

base = icon.resize((256, 256), Image.LANCZOS)
base.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written:", out)
