"""產生合成的測試發票/收據影像與標準答案(供沒有真實掃描檔時做流程驗證與評測)。

用法(專案根目錄執行):
    python tools/make_sample.py                  # 預設 20 張,seed 42,可重現
    python tools/make_sample.py --count 30 --seed 7 --force
    python tools/make_sample.py --out data/samples/另一組
    python tools/make_sample.py --einvoice       # 看有:合成電子發票證明聯(含規格 QR)
    python tools/make_sample.py --legacy         # 只重產 git 追蹤的單張範例

輸出(預設 data/samples/synthetic/,--out 可改):
    sNN_類型[_劣化].png   合成樣本(約 35% 帶模糊/缺角/旋轉/低解析劣化)
    labels.csv            標準答案(utf-8-sig,欄位與 tools/evaluate.py 一致)
    合成發票範例.png       原始單張範例(向下相容,亦列入標準答案)
    目的地已有 labels.csv 就不寫任何檔,要加 --force 才覆寫:data/samples/labels.csv 是真實樣本的
    標準答案(含個資),不能被合成樣本蓋掉。
    --einvoice 只寫 data/samples/einvoice/(eNN_電子發票[_劣化].png/.jpg 與其 labels.csv),
    不會動到上面的評測樣本。
    --legacy 只重產 git 追蹤的 data/samples/合成發票範例.png,不寫標準答案。

設計要點:
- 檔名保留「收據」「模糊」關鍵字,讓 MockAnalyzer 的檔名規則(收據→收據、模糊→低信心)
  在 mock 模式 demo 時仍然成立
- 部分樣本日期印民國年(如 115/07/04),標準答案一律填西元,驗證提示詞的民國換算要求
- 劣化樣本由本工具自行渲染,ground truth 完整已知——這是合成樣本相對真實掃描的優勢
- 電子發票的左側 QR 依財政部條碼規格組成(src.verify.einvoice.build_left_qr),店名虛構(含「示範」),
  統編用演算法產生「通過檢查碼」的假號碼,加密驗證資訊為隨機字串(沒有營業人金鑰,本來就驗不了)
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

BASE = Path(__file__).resolve().parent.parent
SAMPLES_DIR = BASE / "data" / "samples"
SYNTHETIC_DIR = SAMPLES_DIR / "synthetic"
EINVOICE_DIR = SAMPLES_DIR / "einvoice"
LABEL_FIELDS = ["檔名", "doc_type", "date", "vendor", "amount", "invoice_number"]

sys.path.insert(0, str(BASE))

# 中文字型候選(依序嘗試):Windows 微軟正黑體放第一,既有評測樣本在 Windows 上重產結果不變;
# macOS 用 PingFang TC(新版系統放在 AssetsV2 內)或 Heiti TC。(路徑樣式, 字型家族名稱)
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/msjh.ttc", "Microsoft JhengHei"),
    ("/System/Library/Fonts/PingFang.ttc", "PingFang TC"),
    ("/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc", "PingFang TC"),
    ("/System/Library/Fonts/STHeiti Medium.ttc", "Heiti TC"),
    ("/System/Library/Fonts/STHeiti Light.ttc", "Heiti TC"),
]
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_font_choice: tuple[str, int] | None | bool = False   # False = 尚未搜尋;None = 找不到


def _find_font() -> tuple[str, int] | None:
    """找第一個存在的中文字型,回傳 (路徑, .ttc 內的索引)。"""
    for pattern, family in _FONT_CANDIDATES:
        for path in sorted(glob.glob(pattern)) or ([pattern] if Path(pattern).exists() else []):
            for index in range(24):   # .ttc 可能收多個家族(PingFang 有 HK/MO/TC/SC)
                try:
                    name = ImageFont.truetype(path, 12, index=index).getname()[0]
                except OSError:
                    break
                if name == family:
                    return path, index
    return None


def _font(size: int):
    global _font_choice
    if _font_choice is False:
        _font_choice = _find_font()
        if _font_choice is None:
            print("警告:找不到中文字型(微軟正黑體 / PingFang TC / Heiti TC),"
                  "改用 PIL 預設字型,中文會顯示成方塊", file=sys.stderr)
    if size not in _font_cache:
        if _font_choice:
            _font_cache[size] = ImageFont.truetype(_font_choice[0], size, index=_font_choice[1])
        else:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


# 商家池:全名(印在影像上)——evaluation.py 的 vendor 比對是正規化後雙向包含。
# 店名與分店名一律虛構、帶「示範」(比照 EINVOICE_STORES),確保不是真實商家
VENDORS = [
    ("晨光示範超市股份有限公司", "站前店"),
    ("青禾示範便利商店股份有限公司", "河濱門市"),
    ("藍鵲示範量販股份有限公司", "市場店"),
    ("暖陽示範便利商店股份有限公司", "公園門市"),
    ("山嵐示範量販股份有限公司", "老街分公司"),
    ("月見示範藥妝股份有限公司", "廣場店"),
    ("星芒示範電器股份有限公司", "大道門市"),
    ("書香示範書店股份有限公司", "綠園書店"),
    ("小巷示範雜貨股份有限公司", "巷口店"),
    ("紙鳶示範文具股份有限公司", "轉角店"),
]

ITEMS = [
    "鮮乳", "吐司", "雞蛋", "礦泉水", "咖啡", "泡麵", "衛生紙", "洗衣精",
    "蘋果", "香蕉", "原子筆", "筆記本", "電池", "牙膏", "麥片", "醬油",
]


@dataclass
class SampleSpec:
    """一張合成樣本的完整規格(同時是渲染輸入與標準答案)。"""

    filename: str
    doc_type: str            # 發票 / 收據
    doc_date: date
    vendor: str
    branch: str
    items: list[tuple[str, int, int]]   # (品名, 數量, 小計)
    invoice_number: str | None          # 收據為 None
    roc_date: bool                      # 是否以民國年印出
    degrade: str | None                 # None/模糊/缺角/旋轉/低解析

    @property
    def amount(self) -> int:
        return sum(sub for _, _, sub in self.items)

    def date_text(self) -> str:
        if self.roc_date:
            return f"{self.doc_date.year - 1911}/{self.doc_date.month:02d}/{self.doc_date.day:02d}"
        return self.doc_date.isoformat()


def _rand_spec(idx: int, rng: random.Random) -> SampleSpec:
    doc_type = "發票" if rng.random() < 0.55 else "收據"
    vendor, branch = rng.choice(VENDORS)
    doc_date = date(2025, 8, 1) + timedelta(days=rng.randrange(340))
    n_items = rng.randint(2, 4)
    items = []
    for name in rng.sample(ITEMS, n_items):
        qty = rng.randint(1, 3)
        unit = rng.randint(20, 500)
        items.append((name, qty, qty * unit))
    invoice_number = None
    if doc_type == "發票":
        letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
        invoice_number = f"{letters}-{rng.randrange(10_000_000, 100_000_000)}"

    # 約 35% 樣本帶一種劣化;約 30% 印民國年
    degrade = rng.choice(["模糊", "缺角", "旋轉", "低解析"]) if rng.random() < 0.35 else None
    roc_date = rng.random() < 0.30

    suffix = f"_{degrade}" if degrade else ""
    filename = f"s{idx:02d}_{doc_type}{suffix}.png"
    return SampleSpec(filename, doc_type, doc_date, vendor, branch,
                      items, invoice_number, roc_date, degrade)


def render_invoice(spec: SampleSpec) -> Image.Image:
    """電子發票證明聯版型(與原始範例同構)。"""
    img = Image.new("RGB", (480, 640), "white")
    d = ImageDraw.Draw(img)
    roc = spec.doc_date.year - 1911
    period_start = spec.doc_date.month if spec.doc_date.month % 2 == 1 else spec.doc_date.month - 1
    lines = [
        ("電子發票證明聯", _font(34), 40),
        (f"{roc}年{period_start:02d}-{period_start + 1:02d}月", _font(26), 95),
        (spec.invoice_number or "", _font(34), 135),
        (f"{spec.date_text()} {random.Random(spec.filename).randint(8, 21):02d}:"
         f"{random.Random(spec.filename + 'm').randint(0, 59):02d}:18", _font(20), 195),
        (f"隨機碼 {random.Random(spec.filename + 'r').randint(1000, 9999)}"
         f"    總計 {spec.amount}", _font(20), 230),
        (f"賣方 {fake_tax_id(random.Random(spec.filename + 's'))}", _font(20), 265),
        (spec.vendor, _font(26), 315),
        (spec.branch, _font(20), 360),
    ]
    y = 420
    for name, qty, sub in spec.items:
        lines.append((f"{name} x{qty}        {sub}", _font(20), y))
        y += 35
    lines.append(("-" * 28, _font(20), y))
    lines.append((f"合計金額: {spec.amount} 元", _font(26), y + 35))
    for text, font, ypos in lines:
        d.text((40, ypos), text, fill="black", font=font)
    return img


def render_receipt(spec: SampleSpec) -> Image.Image:
    """免用統一發票收據版型(感熱紙風格,窄幅)。"""
    img = Image.new("RGB", (420, 680), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 36), "免用統一發票收據", fill="black", font=_font(30))
    d.text((36, 100), spec.vendor, fill="black", font=_font(24))
    d.text((36, 140), spec.branch, fill="black", font=_font(18))
    d.text((36, 185), f"日期:{spec.date_text()}", fill="black", font=_font(20))
    d.line((30, 225, 390, 225), fill="black", width=1)
    y = 250
    for name, qty, sub in spec.items:
        d.text((36, y), f"{name}  x{qty}", fill="black", font=_font(20))
        d.text((300, y), str(sub), fill="black", font=_font(20))
        y += 38
    d.line((30, y + 6, 390, y + 6), fill="black", width=1)
    d.text((36, y + 30), f"合計金額 {spec.amount} 元", fill="black", font=_font(26))
    d.text((36, y + 90), "此據  收訖", fill="black", font=_font(18))
    return img


def apply_degrade(img: Image.Image, kind: str, rng: random.Random) -> Image.Image:
    """套用劣化效果。缺角只遮品項區(不遮日期/金額/類型),ground truth 仍可辨識。"""
    if kind == "模糊":
        return img.filter(ImageFilter.GaussianBlur(radius=1.8))
    if kind == "缺角":
        d = ImageDraw.Draw(img)
        w, h = img.size
        # 撕角:右下角一塊白色多邊形 + 灰邊,蓋到部分品項行
        tear = [(w, h), (w - rng.randint(140, 200), h), (w, h - rng.randint(160, 220))]
        d.polygon(tear, fill="#f2f2f2", outline="#bbbbbb")
        return img
    if kind == "旋轉":
        rotated = img.rotate(rng.uniform(3.0, 6.0) * rng.choice([-1, 1]),
                             expand=True, fillcolor="white", resample=Image.BILINEAR)
        noise = Image.effect_noise(rotated.size, 40).convert("RGB")
        return Image.blend(rotated, noise, alpha=0.12)
    if kind == "低解析":
        w, h = img.size
        small = img.resize((w // 2, h // 2), Image.BILINEAR)
        return small.resize((w, h), Image.BILINEAR)
    return img


# 原始單張範例的店名也虛構(含「示範」);統編用 fake_tax_id 以固定種子產生,每次重產都一樣
LEGACY_VENDOR, LEGACY_BRANCH = "晨光示範超市股份有限公司", "站前店"
_LEGACY_TAX_SEED = 0


def make_legacy_sample(out_dir: Path) -> tuple[str, dict]:
    """重現原始單張範例(向下相容 README/使用手冊的指令說明)。"""
    img = Image.new("RGB", (480, 640), "white")
    d = ImageDraw.Draw(img)
    lines = [
        ("電子發票證明聯", _font(34), 40),
        ("115年07-08月", _font(26), 95),
        ("AB-12345678", _font(34), 135),
        ("2026-07-04 13:25:18", _font(20), 195),
        ("隨機碼 5847    總計 350", _font(20), 230),
        (f"賣方 {fake_tax_id(random.Random(_LEGACY_TAX_SEED))}", _font(20), 265),
        (LEGACY_VENDOR, _font(26), 315),
        (LEGACY_BRANCH, _font(20), 360),
        ("鮮乳 x2        180", _font(20), 420),
        ("吐司 x1        170", _font(20), 455),
        ("----------------------------", _font(20), 490),
        ("合計金額: 350 元", _font(26), 525),
    ]
    for text, font, y in lines:
        d.text((40, y), text, fill="black", font=font)
    name = "合成發票範例.png"
    img.save(out_dir / name)
    return name, {
        "檔名": name, "doc_type": "發票", "date": "2026-07-04",
        "vendor": LEGACY_VENDOR, "amount": 350, "invoice_number": "AB12345678",
    }


# --- 看有:合成電子發票證明聯(含符合財政部規格的左右 QR 與 Code 39) ---

# 虛構店名:一律帶「示範」,確保不是真實商家
EINVOICE_STORES = [
    ("星芒示範商行", "信義店"),
    ("青禾示範超市", "北港門市"),
    ("藍鵲示範書店", "中和店"),
    ("暖陽示範早餐坊", "新莊店"),
    ("山嵐示範文具行", "竹北店"),
    ("月見示範藥妝", "東區店"),
]
EINVOICE_DEGRADES = [None, "模糊", "旋轉", "褪色", "壓縮"]
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

# Code 39 條碼圖樣:9 個元素(條、空交錯),n 窄、w 寬;證明聯一維條碼只用到數字與大寫字母
_CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
}


def fake_tax_id(rng: random.Random) -> str:
    """產生通過統編檢查碼的假號碼(僅供合成樣本,不對應任何登記中的營業人)。"""
    from src.verify.checks import tax_id_checksum_ok

    while True:
        candidate = f"{rng.randrange(100_000_000):08d}"
        if tax_id_checksum_ok(candidate):
            return candidate


@dataclass
class EInvoiceSpec:
    """一張合成電子發票證明聯的完整規格(渲染輸入 + 標準答案)。"""

    filename: str
    store: str
    branch: str
    issued: datetime
    invoice_number: str        # 10 碼,不含連字號
    random_code: str
    seller_tax_id: str
    items: list[tuple[str, int, int]]   # (品名, 數量, 單價)
    encrypted: str             # 24 碼,隨機
    degrade: str | None = None
    printed_total: int | None = None    # 與 QR 不同時,模擬「印刷/模型讀到的金額 ≠ QR」

    @property
    def total(self) -> int:
        return sum(qty * unit for _, qty, unit in self.items)

    @property
    def period(self) -> str:
        start = self.issued.month if self.issued.month % 2 == 1 else self.issued.month - 1
        return f"{self.issued.year - 1911}年{start:02d}-{start + 1:02d}月"

    @property
    def period_code(self) -> str:
        """一維條碼的年期別:民國年 3 碼 + 期別雙數月 2 碼。"""
        end = self.issued.month + (self.issued.month % 2)
        return f"{self.issued.year - 1911:03d}{end:02d}"

    def qr_texts(self) -> tuple[str, str]:
        """左 QR:77 碼 + 營業人自行使用區 + 品目筆數 + UTF-8 參數 + 第一個品項;右 QR:「**」+ 其餘品項。"""
        from src.verify.einvoice import build_left_qr

        n = len(self.items)
        entries = [f"{name}:{qty}:{unit}" for name, qty, unit in self.items]
        sales = round(self.total / 1.05)   # 未稅銷售額(應稅 5%)
        left = build_left_qr(self.invoice_number, self.issued.date(), self.random_code, sales,
                             self.total, "00000000", self.seller_tax_id, self.encrypted,
                             tail=f":**********:{n}:{n}:1:{entries[0]}")
        return left, "**" + ":".join(entries[1:])


def _rand_einvoice(idx: int, rng: random.Random, degrade: str | None) -> EInvoiceSpec:
    store, branch = rng.choice(EINVOICE_STORES)
    issued = datetime(2026, 1, 1, 8) + timedelta(days=rng.randrange(260), minutes=rng.randrange(780),
                                                 seconds=rng.randrange(60))
    items = []
    for name in rng.sample(ITEMS, rng.randint(2, 4)):
        items.append((name, rng.randint(1, 3), rng.randint(15, 300)))
    letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
    suffix = f"_{degrade}" if degrade else ""
    ext = ".jpg" if degrade == "壓縮" else ".png"
    return EInvoiceSpec(
        filename=f"e{idx:02d}_電子發票{suffix}{ext}",
        store=store, branch=branch, issued=issued,
        invoice_number=f"{letters}{rng.randrange(10_000_000, 100_000_000)}",
        random_code=f"{rng.randrange(10_000):04d}",
        seller_tax_id=fake_tax_id(rng),
        items=items,
        encrypted="".join(rng.choice(_B64) for _ in range(22)) + "==",
        degrade=degrade,
    )


def _qr_pair(left: str, right: str, box: int) -> tuple[Image.Image, Image.Image]:
    """左右 QR 用同一版本(大小一致);左方依規格至少 V6、容錯 Level L。"""
    import qrcode

    def make(text: str, version: int | None):
        code = qrcode.QRCode(version=version, error_correction=qrcode.constants.ERROR_CORRECT_L,
                             box_size=box, border=4)
        code.add_data(text.encode("utf-8"))
        code.make(fit=version is None)
        return code

    version = max(6, make(left, None).version, make(right, None).version)
    return tuple(make(t, version).make_image(fill_color="black", back_color="white").convert("RGB")
                 for t in (left, right))


def _draw_code39(d: ImageDraw.ImageDraw, text: str, center_x: int, y: int, height: int,
                 narrow: int = 1):
    """畫置中的 Code 39(寬窄比 3:1);19 碼 + 起訖符號在 narrow=1 時約 336px 寬。"""
    wide = narrow * 3
    chars = f"*{text}*"
    x = center_x - len(chars) * (3 * wide + 7 * narrow) // 2
    for ch in chars:
        for i, element in enumerate(_CODE39[ch]):
            width = wide if element == "w" else narrow
            if i % 2 == 0:
                d.rectangle([x, y, x + width - 1, y + height], fill="black")
            x += width
        x += narrow   # 字元間隔


def render_einvoice(spec: EInvoiceSpec) -> Image.Image:
    """電子發票證明聯版型(5.7 公分感熱紙比例):抬頭、期別、字軌、一維條碼、左右 QR、交易明細。"""
    printed_total = spec.printed_total if spec.printed_total is not None else spec.total
    img = Image.new("RGB", (600, 1180), "white")
    d = ImageDraw.Draw(img)

    def center(text: str, font, y: int):
        w = d.textlength(text, font=font)
        d.text(((600 - w) / 2, y), text, fill="black", font=font)

    center(spec.store, _font(34), 30)
    center("電子發票證明聯", _font(44), 85)
    center(spec.period, _font(40), 150)
    center(f"{spec.invoice_number[:2]}-{spec.invoice_number[2:]}", _font(44), 205)
    d.text((50, 275), spec.issued.strftime("%Y-%m-%d %H:%M:%S"), fill="black", font=_font(24))
    d.text((50, 312), f"隨機碼:{spec.random_code}", fill="black", font=_font(24))
    d.text((320, 312), f"總計:{printed_total}", fill="black", font=_font(24))
    d.text((50, 349), f"賣方:{spec.seller_tax_id}", fill="black", font=_font(24))
    _draw_code39(d, f"{spec.period_code}{spec.invoice_number}{spec.random_code}", 300, 395, 60)

    left, right = _qr_pair(*spec.qr_texts(), box=4)
    img.paste(left, (50, 480))
    img.paste(right, (550 - right.width, 480))
    y = 480 + left.height + 20
    d.text((50, y), f"{spec.branch}  退貨請憑電子發票證明聯正本", fill="black", font=_font(20))

    y += 50
    d.line((40, y, 560, y), fill="black", width=1)
    y += 15
    for name, qty, unit in spec.items:
        d.text((50, y), f"{name} x{qty}", fill="black", font=_font(24))
        d.text((430, y), f"{qty * unit}", fill="black", font=_font(24))
        y += 36
    d.line((40, y + 6, 560, y + 6), fill="black", width=1)
    d.text((50, y + 20), f"合計金額: {printed_total} 元", fill="black", font=_font(30))
    return img.crop((0, 0, 600, min(img.height, y + 90)))   # 感熱紙依內容長度裁切


def degrade_einvoice(img: Image.Image, kind: str | None, rng: random.Random) -> Image.Image:
    """電子發票的劣化:模糊、旋轉 ±7°、低對比(感熱紙褪色);JPEG 壓縮在存檔時處理。"""
    if kind == "模糊":
        return img.filter(ImageFilter.GaussianBlur(radius=1.2))
    if kind == "旋轉":
        rotated = img.rotate(7 * rng.choice([-1, 1]), expand=True, fillcolor="white",
                             resample=Image.BILINEAR)
        noise = Image.effect_noise(rotated.size, 30).convert("RGB")
        return Image.blend(rotated, noise, alpha=0.08)
    if kind == "褪色":
        faded = ImageEnhance.Brightness(ImageEnhance.Contrast(img).enhance(0.35)).enhance(1.3)
        tint = Image.new("RGB", faded.size, (250, 244, 225))   # 感熱紙泛黃
        return Image.blend(faded, tint, alpha=0.25)
    return img


def save_einvoice(img: Image.Image, spec: EInvoiceSpec, out_dir: Path) -> Path:
    path = out_dir / spec.filename
    if spec.degrade == "壓縮":
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=20)
        path.write_bytes(buf.getvalue())
    else:
        img.save(path)
    return path


EINVOICE_LABEL_FIELDS = ["檔名", "doc_type", "date", "vendor", "amount", "invoice_number",
                         "seller_tax_id", "random_code", "period", "qr_total", "printed_total",
                         "degrade", "note"]


def make_einvoices(out_dir: Path, count: int = 10, seed: int = 42) -> list[dict]:
    """產生 count 張合成電子發票證明聯(輪流套用劣化)+ 1 張「印刷金額 ≠ QR」樣本,寫出 labels.csv。

    標準答案的 amount 一律以 QR 為準;金額不符樣本的印刷值記在 printed_total,
    模擬模型照印刷讀到錯的金額,用來展示驗證攔截。
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = [_rand_einvoice(i + 1, rng, EINVOICE_DEGRADES[i % len(EINVOICE_DEGRADES)])
             for i in range(count)]
    mismatch = _rand_einvoice(count + 1, rng, None)
    mismatch.filename = f"e{count + 1:02d}_電子發票_金額不符.png"
    mismatch.printed_total = mismatch.total + rng.choice([90, 180, 270])
    specs.append(mismatch)

    rows = []
    for spec in specs:
        img = degrade_einvoice(render_einvoice(spec), spec.degrade, rng)
        save_einvoice(img, spec, out_dir)
        printed = spec.printed_total if spec.printed_total is not None else spec.total
        rows.append({
            "檔名": spec.filename, "doc_type": "發票", "date": spec.issued.date().isoformat(),
            "vendor": spec.store, "amount": spec.total, "invoice_number": spec.invoice_number,
            "seller_tax_id": spec.seller_tax_id, "random_code": spec.random_code,
            "period": spec.period, "qr_total": spec.total, "printed_total": printed,
            "degrade": spec.degrade or "",
            "note": "印刷金額與 QR 不符(模擬模型誤讀),標準答案以 QR 為準" if printed != spec.total else "",
        })
    with open(out_dir / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EINVOICE_LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生合成發票/收據樣本與標準答案")
    parser.add_argument("--count", type=int, default=None,
                        help="樣本張數(預設 20;--einvoice 時預設 10,另加 1 張金額不符)")
    parser.add_argument("--seed", type=int, default=42, help="隨機種子(固定可重現,預設 42)")
    parser.add_argument("--out", type=Path, default=None,
                        help="合成發票/收據的輸出資料夾(預設 data/samples/synthetic/)")
    parser.add_argument("--force", action="store_true", help="輸出資料夾已有 labels.csv 時照樣覆寫")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--einvoice", action="store_true",
                      help="只產生合成電子發票證明聯到 data/samples/einvoice/")
    mode.add_argument("--legacy", action="store_true",
                      help="只重產 git 追蹤的單張範例 data/samples/合成發票範例.png")
    args = parser.parse_args(argv)
    if args.out is not None and (args.einvoice or args.legacy):
        parser.error("--einvoice、--legacy 的輸出位置固定,不能和 --out 一起用")

    if args.einvoice:
        rows = make_einvoices(EINVOICE_DIR, count=args.count or 10, seed=args.seed)
        print(f"已產生 {len(rows)} 張合成電子發票(seed={args.seed})到 {EINVOICE_DIR}")
        print(f"  劣化:{'、'.join(sorted({r['degrade'] for r in rows if r['degrade']}))};"
              f"金額不符樣本 {sum(1 for r in rows if r['note'])} 張")
        return 0
    if args.legacy:
        name, _ = make_legacy_sample(SAMPLES_DIR)
        print(f"已重產單張範例:{SAMPLES_DIR / name}")
        return 0

    out_dir = args.out or SYNTHETIC_DIR
    labels_csv = out_dir / "labels.csv"
    # 先檢查再寫:被擋下時資料夾裡什麼都不動(連影像也不寫)
    if labels_csv.exists() and not args.force:
        print(f"{labels_csv} 已經存在,沒有覆寫(可能是真實樣本的標準答案)。"
              "確定要重產請加 --force,或用 --out 換一個資料夾。", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    _, legacy_row = make_legacy_sample(out_dir)
    rows.append(legacy_row)

    stats = {"發票": 0, "收據": 0, "劣化": 0, "民國年": 0}
    for idx in range(1, (args.count or 20) + 1):
        spec = _rand_spec(idx, rng)
        img = render_invoice(spec) if spec.doc_type == "發票" else render_receipt(spec)
        if spec.degrade:
            img = apply_degrade(img, spec.degrade, rng)
            stats["劣化"] += 1
        if spec.roc_date:
            stats["民國年"] += 1
        stats[spec.doc_type] += 1
        img.save(out_dir / spec.filename)
        rows.append({
            "檔名": spec.filename,
            "doc_type": spec.doc_type,
            "date": spec.doc_date.isoformat(),   # 標準答案一律西元
            "vendor": spec.vendor,
            "amount": spec.amount,
            "invoice_number": (spec.invoice_number or "").replace("-", ""),
        })

    with open(labels_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"已產生 {len(rows)} 張樣本(seed={args.seed}):")
    print(f"  發票 {stats['發票']} 張、收據 {stats['收據']} 張"
          f"(另含向下相容範例 1 張)")
    print(f"  劣化樣本 {stats['劣化']} 張、民國年日期 {stats['民國年']} 張")
    print(f"標準答案:{labels_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
