"""純 Pillow 的 Code 39 條碼繪製:給測試與之後的合成帳單產生器用(不依賴任何條碼套件)。

用法(tools/ 在 sys.path 裡時):
    from code39 import draw_code39, stack
    bar = draw_code39("1407086CY", module=2, height=60)          # 白底黑條的 RGB 影像,不含人眼可讀文字
    page = stack([bar, draw_code39("1557341899384519")], gap=24)   # 由上而下排列,間距 24 像素

Code 39(ISO/IEC 16388)重點:
- 每個字元 9 個單元(5 條 4 空,條空交錯、從條開始),其中 3 個是寬的;字元之間隔一個窄空白。
- 前後各加起訖字元 `*`;資料本身不可含 `*`。可用字元:0-9、A-Z、空白與 - . $ / + %。
- 窄寬比建議 1:2~1:3;兩端空白(quiet zone)至少 10 倍窄條寬。規格本身不要求檢查碼。
- 台灣超商代收條碼用 Code 39(統一超商規格:品質 C 級以上、高度約 0.8~1.4 公分、窄寬比 2:5、
  左右留白約 2 公分、起訖用 `*`),三段由上而下排列;各段內容見 src/verify/billcode.py。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

# 每個字元的 9 個單元:n 窄、w 寬;依序 條、空、條、空、條、空、條、空、條(每個字元恰有 3 個寬)
PATTERNS: dict[str, str] = {
    "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw", "5": "wnnwwnnnn",
    "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn", "0": "nnnwwnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
    "$": "nwnwnwnnn", "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn",
}
START_STOP = "*"


def _wide_px(module: int, ratio: float) -> int:
    """寬條(空)的像素寬:窄條 × 窄寬比,四捨五入,且一定比窄條寬(窄寬比 2.5、模組 2 → 5,即統一超商建議的 2:5)。"""
    return max(module + 1, int(module * ratio + 0.5))


def _elements(text: str) -> list[tuple[bool, bool]]:
    """資料 → [(是不是條, 是不是寬)];含前後的 `*` 與字元之間的窄空白。"""
    if START_STOP in text:
        raise ValueError("Code 39 的資料不可含 *(起訖字元由程式加上)")
    bad = sorted({ch for ch in text if ch not in PATTERNS})
    if bad:
        raise ValueError(f"Code 39 不能表示這些字元:{''.join(bad)}")
    elements: list[tuple[bool, bool]] = []
    for i, ch in enumerate(START_STOP + text + START_STOP):
        if i:
            elements.append((False, False))   # 字元之間的窄空白
        elements.extend((j % 2 == 0, w == "w") for j, w in enumerate(PATTERNS[ch]))
    return elements


def code39_width(text: str, module: int = 2, ratio: float = 2.5, quiet: int = 10) -> int:
    """畫出來的影像寬度(像素),含左右各 quiet 個模組的留白。"""
    wide = _wide_px(module, ratio)
    return sum(wide if is_wide else module for _, is_wide in _elements(text)) + 2 * quiet * module


def draw_code39(text: str, module: int = 2, height: int = 80, ratio: float = 2.5, quiet: int = 10) -> Image.Image:
    """畫一條 Code 39:白底黑條的 RGB 影像。

    text:資料(不含起訖 `*`)。module:窄條寬(像素,整數)。height:條高(像素)。
    ratio:寬條與窄條的比例(規格 2~3;預設 2.5)。quiet:左右各留幾個模組寬的空白(規格至少 10)。
    要模擬解析度較低的照片,畫好後自己縮小(Image.resize)即可。
    """
    if module < 1 or height < 1:
        raise ValueError("module 與 height 必須是正整數")
    wide = _wide_px(module, ratio)
    image = Image.new("RGB", (code39_width(text, module, ratio, quiet), height), "white")
    draw = ImageDraw.Draw(image)
    x = quiet * module
    for is_bar, is_wide in _elements(text):
        w = wide if is_wide else module
        if is_bar:
            draw.rectangle((x, 0, x + w - 1, height - 1), fill="black")
        x += w
    return image


def stack(images: list[Image.Image], gap: int = 24, margin: int = 0) -> Image.Image:
    """把幾條條碼由上而下排列(靠左對齊、白底),間距 gap 像素、四周多留 margin 像素;帳單的三段條碼就是這樣排的。"""
    if not images:
        raise ValueError("沒有影像可排列")
    width = max(im.width for im in images) + 2 * margin
    height = sum(im.height for im in images) + gap * (len(images) - 1) + 2 * margin
    page = Image.new("RGB", (width, height), "white")
    y = margin
    for im in images:
        page.paste(im, (margin, y))
        y += im.height + gap
    return page
