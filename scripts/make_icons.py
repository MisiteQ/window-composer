"""从 window-composer.png 生成飞牛 FPK 需要的各尺寸图标。

  python3 scripts/make_icons.py

产出（覆盖写入）：
  packaging/fnos/ICON.PNG            64x64   应用中心列表图标
  packaging/fnos/ICON_256.PNG        256x256 应用详情/桌面图标
  packaging/fnos/app/ui/images/icon_64.png
  packaging/fnos/app/ui/images/icon_256.png

为什么要有这个脚本：源图是"圆角方块 + 四周黑底"的展示图，直接缩放会把
黑边带进图标，在应用中心里看着像贴了一圈黑框。这里先按"非黑像素"求包围盒
裁掉黑边，再补成正方形居中缩放，保证 64/256 两个尺寸视觉一致。

依赖 Pillow（仅打包机上需要，不进镜像、不进 NAS）：
  pip install pillow
"""
import os
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 打包机环境问题
    sys.exit("需要 Pillow：pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "window-composer.png")

PKG = os.path.join(ROOT, "packaging", "fnos")
UI_IMAGES = os.path.join(PKG, "app", "ui", "images")

# 判定"黑边"的阈值：三个通道都低于该值才算背景
_BLACK = 16


def _content_bbox(img: Image.Image):
    """返回非黑内容的最小包围盒 (left, top, right, bottom)。"""
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    step = max(1, min(w, h) // 512)  # 大图抽样，避免 100 万次像素访问
    left, top, right, bottom = w, h, 0, 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            if r > _BLACK or g > _BLACK or b > _BLACK:
                if x < left:
                    left = x
                if y < top:
                    top = y
                if x > right:
                    right = x
                if y > bottom:
                    bottom = y
    if right <= left or bottom <= top:
        return (0, 0, w, h)  # 全黑/纯黑图，原样返回
    return (left, top, right + 1, bottom + 1)


def _square(img: Image.Image) -> Image.Image:
    """把图片补成正方形（居中，用边缘像素外扩）并裁掉黑边。"""
    img = img.convert("RGBA")
    bbox = _content_bbox(img)
    img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    # 以四角像素为基准色填充补边，避免出现突兀的黑角
    bg = img.getpixel((0, 0))
    canvas = Image.new("RGBA", (side, side), bg)
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def main() -> int:
    if not os.path.isfile(SRC):
        sys.exit(f"找不到源图：{SRC}")
    for d in (PKG, UI_IMAGES):
        os.makedirs(d, exist_ok=True)

    with Image.open(SRC) as raw:
        base = _square(raw)
        print(f"源图 {raw.size[0]}x{raw.size[1]} → 裁边后合成 {base.size[0]}x{base.size[1]}")

        targets = [
            (os.path.join(PKG, "ICON.PNG"), 64),
            (os.path.join(PKG, "ICON_256.PNG"), 256),
            (os.path.join(UI_IMAGES, "icon_64.png"), 64),
            (os.path.join(UI_IMAGES, "icon_256.png"), 256),
        ]
        for path, size in targets:
            icon = base.resize((size, size), Image.LANCZOS)
            icon.save(path, "PNG", optimize=True)
            print(f"  → {os.path.relpath(path, ROOT)}  {size}x{size}")

    # 自检：飞牛要求必须是 PNG 且尺寸精确
    for path, size in targets:
        with Image.open(path) as im:
            assert im.format == "PNG", f"{path} 不是 PNG"
            assert im.size == (size, size), f"{path} 尺寸 {im.size} != {size}"
    print("图标校验通过：PNG 格式 + 64/256 尺寸")
    return 0


if __name__ == "__main__":
    sys.exit(main())
