"""產生網頁圖示(v5 車站時刻表):深墨底上一塊月台黃翻牌,牌面中間一道接縫,牌上一個深墨的「看」。

字形來自 Noto Sans TC(SIL Open Font License 1.1,允許把字畫進圖片),預設用 Windows 內建的
C:/Windows/Fonts/NotoSansTC-VF.ttf(可變字型,預設是最細,這裡設成 800)。先畫 4 倍大再縮小,邊緣才平順。
顏色同 app.css:深墨 #14212B、月台黃 #F6C400、接縫 #C79F00。
輸出到 web/static/:
- icon-512.png、icon-192.png:手機主畫面、安裝成 App(manifest.webmanifest 的 "any");四周留深墨邊。
- icon-maskable-512.png:Android 會把圖示裁成圓形或圓角(manifest 的 "maskable"),牌縮到邊長的 56%,
  整塊牌都在中間 80% 的安全圈裡,牌角不會被切掉。
- 只放一個「看」字:主畫面與分頁圖示太小,放不下「看」「有」兩塊牌(頂列的字標才是兩塊)。
- icon-180.png:iPhone 主畫面(apple-touch-icon)。
- favicon-32.png:瀏覽器分頁;牌幾乎滿版,小小的也看得出黃色。
用法(在 repo 根目錄):.venv/Scripts/python.exe tools/make_icons.py [字型路徑]
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "static"
FONT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Windows\Fonts\NotoSansTC-VF.ttf")
INK, SIGNAL, SEAM = "#14212B", "#F6C400", "#C79F00"
SCALE = 4
SIZES = (("icon-512.png", 512, .68), ("icon-192.png", 192, .68), ("icon-maskable-512.png", 512, .56),
         ("icon-180.png", 180, .78), ("favicon-32.png", 32, .94))


def icon(size: int, tile: float) -> Image.Image:
    """size:輸出的邊長;tile:黃牌佔邊長的比例。"""
    big = size * SCALE
    img = Image.new("RGB", (big, big), INK)
    draw = ImageDraw.Draw(img)
    side = round(big * tile)
    x0 = y0 = (big - side) // 2
    draw.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=round(side * .12), fill=SIGNAL)
    seam = max(SCALE, round(side * .02))     # 接縫畫在字的後面,不切過字
    draw.rectangle((x0, big // 2 - seam // 2, x0 + side, big // 2 - seam // 2 + seam), fill=SEAM)
    font = ImageFont.truetype(str(FONT), round(side * .72))
    font.set_variation_by_axes([800])
    left, top, right, bottom = draw.textbbox((0, 0), "看", font=font)
    draw.text(((big - (right - left)) / 2 - left, (big - (bottom - top)) / 2 - top), "看", font=font, fill=INK)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    for name, size, tile in SIZES:
        icon(size, tile).save(OUT / name, optimize=True)
        print(OUT / name)


if __name__ == "__main__":
    main()
