"""从 public/logo.jpg 生成 PWA 图标（中心补白成正方形）。

用法（在项目根目录）：
    .venv/Scripts/python.exe frontend/scripts/gen_icons.py
"""

from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parent.parent / "public" / "logo.jpg"
OUT_DIR = Path(__file__).resolve().parent.parent / "public" / "icons"
BG = (249, 246, 242)  # --color-bg
SIZES = [192, 512]
MASKABLE_FILL = 0.78  # maskable 图标安全区：内容需留边，避免被系统蒙版裁掉


def square_canvas(img: Image.Image, fill: float) -> Image.Image:
    side = max(img.width, img.height) / fill
    canvas = Image.new("RGB", (int(side), int(side)), BG)
    x = int((side - img.width) / 2)
    y = int((side - img.height) / 2)
    canvas.paste(img, (x, y))
    return canvas


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC)
    src.thumbnail((1024, 1024), Image.LANCZOS)

    for size in SIZES:
        square_canvas(src, 1.0).resize((size, size), Image.LANCZOS).save(
            OUT_DIR / f"icon-{size}.png"
        )
        square_canvas(src, MASKABLE_FILL).resize((size, size), Image.LANCZOS).save(
            OUT_DIR / f"icon-maskable-{size}.png"
        )

    # Windows 快捷方式/任务栏用的 .ico，一次写全常用尺寸，缺尺寸会糊
    square_canvas(src, 1.0).save(
        OUT_DIR / "moz.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    names = ", ".join(sorted(p.name for p in OUT_DIR.glob("*.png")) + ["moz.ico"])
    print(f"logo {src.size[0]}x{src.size[1]} -> {names}")


if __name__ == "__main__":
    main()
