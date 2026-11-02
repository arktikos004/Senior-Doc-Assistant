"""產生多版型的合成評測集(EV-SET):影像 + labels.jsonl 標準答案。

用法(專案根目錄):
    python tools/make_eval_set.py                        # 輸出到 data/samples/eval/(已被 .gitignore 忽略)
    python tools/make_eval_set.py --out <資料夾>
    python tools/make_eval_set.py --per-template 10 --seed 7
    python tools/make_eval_set.py --templates bill_gas --per-template 20   # 只產生指定版型(擴充實例用)

設計要點:
- 依「版型」切 dev / test:調提示詞只看 dev,報成績用 test;同一版型換數值不算獨立的泛化證據。
- 13 個版型:dev 7 個(bill_power、bill_water、notice_receipt_days、notice_fixed_date、med_bag_a、receipt_a、
  einvoice_a)、test 6 個(bill_telecom、bill_gas、notice_service_next_day、med_bag_b、receipt_b、einvoice_b)。
  電子發票的版面是財政部規定的,只有一種:einvoice_a/b 只差店名池與數值,為了「dev 與 test 的版型集合不相交」才分成兩個名稱。
- 每個版型預設 6 組數值;每組一張 clean + 一張降質(blur、tilt、glare、crop、dark 依序輪用);
  einvoice_b 每組另加一張 QR 汙損到解不開的 qr_damaged。
- 固定 seed、同 seed 重跑結果逐位元相同:每個版型用自己的亂數流依序抽數值,每個條件用自己的亂數流降質,
  所以第 k 組數值只由 (seed, 版型, k) 決定,--per-template 加大只會多出新的組,不會改動前面的組。
- 標準答案由這裡選定的數值直接寫出,不從影像或模型讀回,公文期限也不呼叫 src.dates.resolve_deadline
  (tests/test_make_eval_set.py 另外檢查兩邊一致)。電子發票畫完會用 src.verify.einvoice 確認 QR 解得開/解不開
  與標準答案一致;寫出前每筆標準答案都過 validate_label(labels.jsonl 契約)。
- 電費、水費、瓦斯費印有超商代收三段條碼(電信費沒有,兩種情況 test 都有);金額同標準答案,代收期限依版型
  晚於繳費期限 0、30、3 天。clean 影像畫完用 src.verify.billcode 確認解得開且與規格一致;降質後解不解得開
  不寫進標準答案(那是核對程式的表現,由評測量)。
- 全部內容虛構:機構、公司、診所用「青禾」地名,人名、地址、電話用「○」遮蔽,每張影像都有「範例」浮水印
  (make_demo_docs._watermark);只能放合成資料,不得混進真實文件。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

TOOLS_DIR = Path(__file__).resolve().parent
BASE = TOOLS_DIR.parent
for _path in (str(BASE), str(TOOLS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import code39  # noqa: E402  帳單的超商代收三段條碼
import make_demo_docs  # noqa: E402  浮水印與字型
import make_sample  # noqa: E402  電子發票、收據的產生方式

DEFAULT_OUT = BASE / "data" / "samples" / "eval"
DEFAULT_SEED = 42
DEFAULT_PER_TEMPLATE = 6

CONDITIONS = ("blur", "tilt", "glare", "crop", "dark")     # 降質條件,依組別序號輪用
ALL_CONDITIONS = ("clean",) + CONDITIONS + ("qr_damaged",)
SPLITS = ("dev", "test")
LABEL_DOC_TYPES = ("發票", "收據", "帳單", "公文", "藥袋")
BILL_KINDS = ("電費", "水費", "瓦斯", "電信", "其他")        # 同 src/models.py 的 bill_kind
MED_TIMINGS = ("早", "中", "晚", "睡前")                      # 同 src/models.py 的 timing

# 所有文件日期(出帳日、發文日、調劑日、開立日)都落在這段:不晚於今天,才不會被日期合理性檢查判錯;
# 繳費期限與公文期限可以晚於這段(最晚約 11 月中),它們不受「不晚於今天」限制,只要不早於開立日、不超過 2 年
DATE_FIRST, DATE_LAST = date(2026, 7, 1), date(2026, 9, 30)

CROP_MIN, CROP_MAX = 0.05, 0.08      # crop 條件:裁掉一側的比例
TILT_MIN, TILT_MAX = 5.0, 8.0        # tilt 條件:傾斜角度(度)


# --- 民國年寫法 ---

# 帳單常見的民國年寫法:115年10月20日、115年10月20日(補零)、115/10/20、115.10.20
BILL_STYLES = ("cn", "slash", "cn_pad", "dot")


def roc(day: date, style: str) -> str:
    """西元日期 → 印在文件上的民國年字串(style 見各分支)。"""
    y = day.year - 1911
    if style == "cn":
        return f"{y}年{day.month}月{day.day}日"
    if style == "cn_pad":
        return f"{y}年{day.month:02d}月{day.day:02d}日"
    if style == "slash":
        return f"{y}/{day.month:02d}/{day.day:02d}"
    if style == "dot":
        return f"{y}.{day.month:02d}.{day.day:02d}"
    if style == "full":
        return f"中華民國{y}年{day.month}月{day.day}日"
    if style == "full_pad":
        return f"中華民國{y}年{day.month:02d}月{day.day:02d}日"
    raise ValueError(f"不認得的民國年寫法:{style}")


def money(amount: int) -> str:
    """千分位金額,例 1286 → 1,286。"""
    return f"{amount:,}"


# --- 字型與版面小工具 ---

INK = (20, 20, 20)
GREY = (70, 70, 70)
LIGHT = (130, 130, 130)
RED = (160, 0, 0)
PAPER = (252, 252, 250)
WHITE = (255, 255, 255)

# 楷體、明體讓公文與收據看起來不同於帳單;找不到就退回 make_demo_docs 的字型(換機器只影響字體,不影響標準答案)
_FONT_FILES = {
    "kai": ("C:/Windows/Fonts/kaiu.ttf",),
    "ming": ("C:/Windows/Fonts/mingliu.ttc",),
}


@lru_cache(maxsize=None)
def font(size: int, kind: str = "sans") -> ImageFont.ImageFont:
    for path in _FONT_FILES.get(kind, ()):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return make_demo_docs._font(size)


def check_fonts() -> None:
    """找不到繁中字型時影像上的中文會變成方塊,這樣的評測集沒有意義:產生前先擋下來。"""
    if not any(Path(p).exists() for p in make_demo_docs._FONT_CANDIDATES):
        raise RuntimeError("找不到繁中字型(微軟正黑體、PingFang、Noto Sans CJK 都沒有),影像上的中文會是方塊,不產生")


@dataclass(frozen=True)
class Mark:
    """影像上的一個關鍵欄位:印了什麼字、在哪裡。測試用它對照「印出來的字」與標準答案;key 為 "qr" 的是電子發票的 QR 位置。"""

    key: str
    text: str
    box: tuple[int, int, int, int]      # 左、上、右、下(像素)


@dataclass
class Rendered:
    """一張尚未加浮水印、尚未降質的版面。"""

    image: Image.Image
    marks: list[Mark]
    content: tuple[int, int, int, int]  # 非底色內容的外框:crop 只裁外框以外的留白
    bg: tuple[int, int, int] = PAPER    # 紙的底色:傾斜後四角補這個顏色
    texts: tuple[str, ...] = ()         # 版面上印的每一行字(含沒有 key 的),測試檢查「沒有多印不該印的東西」用

    def printed(self, key: str) -> list[str]:
        """某個欄位印出來的字(折行的欄位會有多行,依序排列)。"""
        return [m.text for m in self.marks if m.key == key]

    def boxes(self, key: str) -> list[tuple[int, int, int, int]]:
        return [m.box for m in self.marks if m.key == key]


def content_box(img: Image.Image, bg: tuple[int, int, int], tolerance: int = 24) -> tuple[int, int, int, int]:
    """影像裡和底色不同的像素的外框(左、上、右、下,右下為開區間)。"""
    diff = np.abs(np.asarray(img.convert("RGB")).astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > tolerance
    cols, rows = np.nonzero(diff.any(axis=0))[0], np.nonzero(diff.any(axis=1))[0]
    if not len(cols):
        raise ValueError("版面是空白的")
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


class Page:
    """一張紙:畫字、畫框,並記下關鍵欄位的位置。"""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int] = PAPER):
        self.bg = bg
        self.img = Image.new("RGB", (width, height), bg)
        self.d = ImageDraw.Draw(self.img)
        self.marks: list[Mark] = []
        self.texts: list[str] = []

    def text(self, x: int, y: int, s: str, size: int = 36, fill=INK, kind: str = "sans", bold: bool = False,
             key: str | None = None, anchor: str = "la") -> tuple[int, int, int, int]:
        """畫一行字,回傳字的外框(左、上、右、下)。key 有值就記成關鍵欄位。"""
        f, stroke = font(size, kind), (1 if bold else 0)
        self.d.text((x, y), s, font=f, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=fill)
        box = tuple(int(v) for v in self.d.textbbox((x, y), s, font=f, anchor=anchor, stroke_width=stroke))
        self.texts.append(s)
        if key:
            self.marks.append(Mark(key, s, box))
        return box

    def text_fit(self, x: int, y: int, s: str, size: int, max_width: int, min_size: int = 20, **kwargs):
        """畫一行字;太寬就把字級縮小到放得進 max_width(表格的欄位用),不折行。"""
        while size > min_size and self.d.textlength(s, font=font(size, kwargs.get("kind", "sans"))) > max_width:
            size -= 1
        return self.text(x, y, s, size, **kwargs)

    def wrap(self, s: str, size: int, kind: str, width: int, first: int | None = None) -> list[str]:
        """依像素寬度折行:優先在逗號、句號之後換行(整個子句放得下就不拆開,期限原文才會印在同一行),
        單一子句比一行還長才逐字折。first 是第一行可用的寬度(前面有標題字時比較窄)。"""
        f = font(size, kind)

        def fits(text: str, limit: int) -> bool:
            return self.d.textlength(text, font=f) <= limit

        lines, cur, limit = [], "", first if first is not None else width
        for clause in (c for c in re.split(r"(?<=[,;。,;])", s) if c):
            if fits(cur + clause, limit):
                cur += clause
                continue
            if cur:                              # 放不下:這一行先收起來,子句移到下一行
                lines.append(cur)
                cur, limit = "", width
            if fits(clause, limit):
                cur = clause
                continue
            for ch in clause:                    # 單一子句比一行還長:逐字折
                if cur and not fits(cur + ch, limit):
                    lines.append(cur)
                    cur, limit = "", width
                cur += ch
        return lines + [cur]

    def paragraph(self, x: int, y: int, s: str, size: int, width: int, kind: str = "sans", gap: int = 14,
                  fill=INK, key: str | None = None, first: int | None = None, x_first: int | None = None) -> int:
        """畫一段會折行的字,回傳下一行的 y。x_first 是第一行的起點(其餘行從 x 起,縮排或懸掛用)。"""
        for i, line in enumerate(self.wrap(s, size, kind, width, first)):
            self.text(x_first if (i == 0 and x_first is not None) else x, y, line, size, fill, kind, key=key)
            y += size + gap
        return y

    def done(self) -> Rendered:
        return Rendered(self.img, self.marks, content_box(self.img, self.bg), self.bg, tuple(self.texts))


def mask_name() -> str:
    return "○○○"


MASK_PHONE = "○○-○○○○-○○○○"
MASK_ADDRESS = "青禾市○○區○○路○○號"


# --- 降質:blur、tilt、glare、crop、dark(模擬拍照/掃描的狀況;除了 crop,都對加完浮水印的影像做) ---

def degrade_blur(img: Image.Image, rng: random.Random, rendered: Rendered) -> Image.Image:
    """高斯模糊:半徑隨影像寬度縮放,字仍看得出來。"""
    return img.filter(ImageFilter.GaussianBlur(radius=img.width * rng.uniform(0.0014, 0.0019)))


def degrade_tilt(img: Image.Image, rng: random.Random, rendered: Rendered) -> Image.Image:
    """傾斜 ±5–8°(整張放大不裁切,四角補紙色)。"""
    angle = rng.choice([-1, 1]) * rng.uniform(TILT_MIN, TILT_MAX)
    return img.rotate(angle, expand=True, resample=Image.BICUBIC, fillcolor=rendered.bg)


def degrade_dark(img: Image.Image, rng: random.Random, rendered: Rendered) -> Image.Image:
    """偏暗:亮度降到 38–50%(光線不足的室內)。"""
    return ImageEnhance.Brightness(img).enhance(rng.uniform(0.38, 0.5))


GLARE_PEAK = 0.65     # 亮斑中心把字洗掉的程度:字變淡但仍看得到(整個抹掉的話,標準答案就讀不出來了)


def degrade_glare(img: Image.Image, rng: random.Random, rendered: Rendered) -> Image.Image:
    """亮斑:一塊高斯形的反光,中心落在有字的地方(紙面是近白色,蓋在空白處看不出差別)。

    可能蓋到關鍵欄位,這就是這個條件要測的;只有 QR 要避開,電子發票的 QR 標準答案是「解得開」。
    """
    w, h = img.size
    r = 0.14 * max(w, h)
    ys, xs = np.nonzero(np.asarray(rendered.image.convert("L")) < 128)      # 字與線條的像素
    if len(xs) == 0:
        raise ValueError("版面上沒有字,放不了亮斑")
    ok = np.ones(len(xs), bool)
    for x0, y0, x1, y1 in rendered.boxes("qr"):
        dist = np.hypot(np.maximum.reduce([x0 - xs, np.zeros_like(xs), xs - x1]),
                        np.maximum.reduce([y0 - ys, np.zeros_like(ys), ys - y1]))
        ok &= dist >= 0.8 * r
    candidates = np.nonzero(ok)[0] if ok.any() else np.arange(len(xs))
    pick = candidates[rng.randrange(len(candidates))]
    cx, cy = float(xs[pick]), float(ys[pick])
    sx, sy = r / 2 * rng.uniform(0.8, 1.25), r / 2 * rng.uniform(0.8, 1.25)
    gx = ((np.arange(w) - cx) ** 2 / (2 * sx * sx))[None, :]
    gy = ((np.arange(h) - cy) ** 2 / (2 * sy * sy))[:, None]
    alpha = (GLARE_PEAK * np.exp(-(gx + gy)))[:, :, None].astype(np.float32)
    base = np.asarray(img.convert("RGB")).astype(np.float32)
    out = base * (1 - alpha) + np.array([255, 252, 245], np.float32) * alpha
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


def degrade_crop(img: Image.Image, rng: random.Random, rendered: Rendered) -> Image.Image:
    """裁掉一側 5–8% 的留白:只挑留白夠寬的一側,內容(含關鍵欄位)完整留著。

    吃的是還沒加浮水印的版面(PRE_WATERMARK):裁完才加,左上角的「範例」標籤就不會被裁掉。
    """
    w, h = img.size
    x0, y0, x1, y1 = rendered.content
    margin = {"left": x0, "top": y0, "right": w - x1, "bottom": h - y1}
    length = {"left": w, "right": w, "top": h, "bottom": h}
    safety = 6      # 內容與新邊界之間至少留幾個像素
    room = {side: min(CROP_MAX * length[side], margin[side] - safety) for side in margin}
    feasible = sorted(side for side in margin if room[side] >= CROP_MIN * length[side])
    if not feasible:
        raise ValueError(f"留白不足 {CROP_MIN:.0%},裁不了:{margin}")
    side = rng.choice(feasible)
    cut = rng.randint(math.ceil(CROP_MIN * length[side]), math.floor(room[side]))
    box = {"left": (cut, 0, w, h), "right": (0, 0, w - cut, h), "top": (0, cut, w, h), "bottom": (0, 0, w, h - cut)}
    return img.crop(box[side])


DEGRADES: dict[str, Callable[[Image.Image, random.Random, Rendered], Image.Image]] = {
    "blur": degrade_blur, "tilt": degrade_tilt, "glare": degrade_glare, "crop": degrade_crop, "dark": degrade_dark,
}
PRE_WATERMARK = frozenset({"crop"})     # 這些條件對還沒加浮水印的版面做,做完才加浮水印;其餘對加好浮水印的影像做


def condition_for(variant: int) -> str:
    """第 variant 組(從 1 起算)的降質條件:blur、tilt、glare、crop、dark 依序輪用。"""
    return CONDITIONS[(variant - 1) % len(CONDITIONS)]


# --- 抽數值的小工具 ---

def random_day(rng: random.Random, first: date = DATE_FIRST, last: date = DATE_LAST) -> date:
    return first + timedelta(days=rng.randrange((last - first).days + 1))


def unique(draw: Callable[[], Any], seen: set, tries: int = 300, key: Callable[[Any], Any] = lambda v: v) -> Any:
    """反覆抽到 key(值) 沒出現過的值才收(最多 tries 次,組數多到抽不出新值就接受重複,不會卡住)。"""
    value = draw()
    for _ in range(tries):
        if key(value) not in seen:
            break
        value = draw()
    seen.add(key(value))
    return value


def shuffled(rng: random.Random, pool: list | tuple) -> list:
    """把整個 pool 洗過一遍;第 k 組取 order[k % len(order)],前 len(pool) 組互不重複。

    洗的是整個 pool、與組數無關,所以 --per-template 加大時前面的組不會變。
    """
    return rng.sample(list(pool), len(pool))


# --- 標準答案的格式與檢查 ---

LABEL_KEYS = ("file", "split", "template", "variant", "condition", "doc_type", "vendor", "date", "amount",
              "invoice_number", "fields", "qr", "barcode")
FIELD_KEYS = {
    "帳單": ("due_date", "bill_kind"),
    "公文": ("subject", "doc_number", "deadline_text", "deadline", "required_actions"),
    "藥袋": ("items",),
    "發票": ("seller_tax_id", "random_code", "period"),
    "收據": (),
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_label(label: Any) -> list[str]:
    """檢查一筆標準答案的欄位是否齊全、型別是否正確;回傳問題清單(空清單 = 沒問題)。

    只檢查契約(與 EV-3WAY 共用的 labels.jsonl 格式),不檢查數值是否合理;數值的合理性由測試另外檢查。
    """
    if not isinstance(label, dict):
        return ["標準答案不是 JSON 物件"]
    problems = [f"缺少欄位 {k}" for k in LABEL_KEYS if k not in label]
    problems += [f"多出欄位 {k}" for k in label if k not in LABEL_KEYS]
    if problems:
        return problems

    split, template, doc_type = label["split"], label["template"], label["doc_type"]
    if split not in SPLITS:
        problems.append(f"split 只能是 dev 或 test:{split!r}")
    if not _text(template):
        problems.append("template 要是非空字串")
    if not _is_int(label["variant"]) or label["variant"] < 1:
        problems.append("variant 要是從 1 起算的整數")
    if label["condition"] not in ALL_CONDITIONS:
        problems.append(f"condition 不在 {ALL_CONDITIONS}:{label['condition']!r}")
    if doc_type not in LABEL_DOC_TYPES:
        problems.append(f"doc_type 不在 {LABEL_DOC_TYPES}:{doc_type!r}")

    file = label["file"]
    if not (_text(file) and file.endswith(".png") and "\\" not in file and not file.startswith("/")
            and ".." not in file.split("/") and file.split("/")[0] == split):
        problems.append(f"file 要是以 {split}/ 開頭的相對 POSIX 路徑(.png):{file!r}")

    if label["vendor"] is not None and not _text(label["vendor"]):
        problems.append("vendor 要是非空字串或 null")
    if not _is_iso_date(label["date"]):
        problems.append(f"date 要是 YYYY-MM-DD:{label['date']!r}")
    if label["amount"] is not None and not (_is_int(label["amount"]) and label["amount"] > 0):
        problems.append(f"amount 要是正整數或 null:{label['amount']!r}")
    number = label["invoice_number"]
    if number is not None and not (isinstance(number, str) and len(number) == 10 and number[:2].isalpha()
                                   and number[:2].isupper() and number[2:].isdigit()):
        problems.append(f"invoice_number 要是 2 碼大寫英文 + 8 碼數字或 null:{number!r}")

    qr = label["qr"]
    if not (isinstance(qr, dict) and list(qr) == ["present", "readable"]
            and all(isinstance(v, bool) for v in qr.values())):
        problems.append("qr 要是 {present: bool, readable: bool}")
    else:
        if qr["present"] != (doc_type == "發票"):
            problems.append("只有電子發票的 qr.present 是 true")
        if qr["present"]:
            if qr["readable"] == (label["condition"] == "qr_damaged"):
                problems.append("qr_damaged 的 readable 要是 false,其他條件的電子發票要是 true")
        else:
            if qr["readable"]:
                problems.append("沒有 QR 時 readable 不能是 true")
            if label["condition"] == "qr_damaged":
                problems.append("qr_damaged 只有電子發票才有")

    barcode = label["barcode"]
    if not (isinstance(barcode, dict) and list(barcode) == ["present"] and isinstance(barcode["present"], bool)):
        problems.append("barcode 要是 {present: bool}")
    elif barcode["present"] and doc_type != "帳單":
        problems.append("只有帳單的 barcode.present 可以是 true")

    fields = label["fields"]
    if doc_type in FIELD_KEYS:
        if not isinstance(fields, dict) or sorted(fields) != sorted(FIELD_KEYS[doc_type]):
            problems.append(f"{doc_type}的 fields 鍵要剛好是 {list(FIELD_KEYS[doc_type])}")
        else:
            problems += _validate_fields(doc_type, fields)
    return problems


def _validate_fields(doc_type: str, fields: dict) -> list[str]:
    problems: list[str] = []
    if doc_type == "帳單":
        if not _is_iso_date(fields["due_date"]):
            problems.append("fields.due_date 要是 YYYY-MM-DD")
        if fields["bill_kind"] not in BILL_KINDS:
            problems.append(f"fields.bill_kind 要是 {BILL_KINDS} 之一")
    elif doc_type == "公文":
        for key in ("subject", "doc_number", "deadline_text"):
            if not _text(fields[key]):
                problems.append(f"fields.{key} 要是非空字串")
        if not _is_iso_date(fields["deadline"]):
            problems.append("fields.deadline 要是 YYYY-MM-DD")
        actions = fields["required_actions"]
        if not (isinstance(actions, list) and actions and all(_text(a) for a in actions)):
            problems.append("fields.required_actions 要是非空的字串清單")
    elif doc_type == "藥袋":
        items = fields["items"]
        if not (isinstance(items, list) and items):
            return ["fields.items 要是非空清單"]
        for i, item in enumerate(items, start=1):
            if not (isinstance(item, dict) and sorted(item) == ["name", "prn", "timing"]):
                problems.append(f"fields.items[{i}] 的鍵要剛好是 name、timing、prn")
                continue
            if not _text(item["name"]):
                problems.append(f"fields.items[{i}].name 要是非空字串")
            if not isinstance(item["prn"], bool):
                problems.append(f"fields.items[{i}].prn 要是 true/false")
            timing = item["timing"]
            if not (isinstance(timing, list) and all(t in MED_TIMINGS for t in timing)
                    and len(set(timing)) == len(timing)):
                problems.append(f"fields.items[{i}].timing 要是 {MED_TIMINGS} 的不重複子集")
    elif doc_type == "發票":
        if not (isinstance(fields["seller_tax_id"], str) and len(fields["seller_tax_id"]) == 8
                and fields["seller_tax_id"].isdigit()):
            problems.append("fields.seller_tax_id 要是 8 位數字")
        if not (isinstance(fields["random_code"], str) and len(fields["random_code"]) == 4
                and fields["random_code"].isdigit()):
            problems.append("fields.random_code 要是 4 位數字")
        if not _text(fields["period"]):
            problems.append("fields.period 要是非空字串")
    return problems


# --- 版型登記 ---

@dataclass(frozen=True)
class Template:
    """一個版型:怎麼抽數值(draw)、怎麼畫(render);標準答案由規格的 truth() 直接給。"""

    name: str
    split: str
    doc_type: str
    draw: Callable[[random.Random, int], list]          # (亂數流, 組數) → 每組一個規格
    render: Callable[[Any], Rendered]
    has_qr: bool = False
    extra_conditions: tuple[str, ...] = ()               # 每組多產生的條件(例如電子發票的 qr_damaged)

    def conditions(self, variant: int) -> list[str]:
        return ["clean", condition_for(variant), *self.extra_conditions]


TEMPLATES: dict[str, Template] = {}


def register(template: Template) -> Template:
    TEMPLATES[template.name] = template
    return template


# --- 帳單:電費、水費(dev),電信費、瓦斯費(test) ---

@dataclass(frozen=True)
class BillSpec:
    vendor: str
    kind: str                                   # bill_kind:電費/水費/瓦斯/電信
    issued: date                                # 出帳日
    due: date                                   # 繳費期限
    amount: int                                 # 本期應繳總金額(一律 ≥ 1,000,印出來有千分位)
    style: str                                  # 民國年寫法(BILL_STYLES)
    usage: int = 0                              # 用量(度、立方公尺):影像上的干擾項,不進標準答案
    period: tuple[date, date] = (date(2026, 1, 1), date(2026, 1, 1))   # 計費期間:干擾項
    meter: date = date(2026, 1, 1)              # 抄表日:干擾項
    items: tuple[tuple[str, int], ...] = ()     # 費用明細(加總 = amount)
    prev: int = 0                               # 上期金額:干擾項
    barcode: tuple[str, str, str] | None = None  # 超商代收三段條碼(沒印的版型為 None);金額 = amount

    def truth(self) -> dict:
        return {"vendor": self.vendor, "date": self.issued.isoformat(), "amount": self.amount,
                "invoice_number": None,
                "fields": {"due_date": self.due.isoformat(), "bill_kind": self.kind}}


BARCODE_ITEMS = {"電費": "6PW", "水費": "6WT", "瓦斯": "6GS"}     # 虛構的代收項目代號(3 碼)


def store_barcode(kind: str, issued: date, due: date, amount: int, variant: int,
                  collect_after: int) -> tuple[str, str, str]:
    """一張合成帳單的三段條碼:代收期限 = 繳費期限 + collect_after 天,第三段的應繳日期 = 繳費期限。

    銷帳編號由代號、出帳日與組別組成(16 碼),不用亂數流:加上條碼不會改動其他欄位抽到的值。
    """
    from src.verify import billcode
    account = f"{BARCODE_ITEMS[kind][1:]}{issued:%Y%m%d}{variant:06d}"
    return billcode.build_store_barcodes(due + timedelta(days=collect_after), BARCODE_ITEMS[kind], account,
                                         amount, pay_date=due)


def draw_store_barcode(p: "Page", segments: tuple[str, str, str], right: int, top: int, height: int = 44,
                       gap: int = 16) -> tuple[int, int, int, int]:
    """從 (right, top) 往左下畫三段條碼(由上而下,窄條 2 像素、窄寬比 2:5),上方印一行說明。

    條碼區記成 key "barcode" 的位置(不印條碼下的數字:號碼只在條碼裡);回傳條碼區外框。
    """
    caption = p.text(right, top, "超商代收專用條碼(範例)", 26, GREY, anchor="ra")
    bars = code39.stack([code39.draw_code39(s, module=2, height=height) for s in segments], gap=gap)
    x0, y0 = right - bars.width, caption[3] + 12
    p.img.paste(bars, (x0, y0))
    box = (x0, y0, x0 + bars.width, y0 + bars.height)
    p.marks.append(Mark("barcode", "store", box))
    return box


def read_store_barcode(image: Image.Image) -> tuple[str, str, str] | None:
    """用系統自己的解碼流程(src.verify.billcode)讀三段條碼;讀不到或校對碼不合回 None。"""
    from src.verify import billcode
    bgr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    found = billcode.parse_store_barcodes(billcode.decode_bill_barcodes(bgr))
    return found.raw if found else None


def _bill_common(rng: random.Random, seen_dates: set) -> tuple[date, date, date]:
    """出帳日(每組不同)、繳費期限(出帳後 14–25 天)、抄表日(出帳前 2–8 天)。"""
    issued = unique(lambda: random_day(rng), seen_dates)
    return issued, issued + timedelta(days=rng.randint(14, 25)), issued - timedelta(days=rng.randint(2, 8))


def _other_amount(rng: random.Random, amount: int) -> int:
    """干擾用的上期金額:和本期不同。"""
    prev = rng.randint(900, 4600)
    return prev if prev != amount else prev + 7


def draw_bill_power(rng: random.Random, n: int) -> list[BillSpec]:
    specs, dates, amounts, start = [], set(), set(), rng.randrange(len(BILL_STYLES))
    for k in range(n):
        issued, due, meter = _bill_common(rng, dates)
        amount = unique(lambda: rng.randint(1000, 4800), amounts)
        specs.append(BillSpec(
            "青禾電力股份有限公司", "電費", issued, due, amount, BILL_STYLES[(k + start) % len(BILL_STYLES)],
            usage=int(amount / rng.uniform(2.8, 4.0)), period=(meter - timedelta(days=59), meter), meter=meter,
            prev=_other_amount(rng, amount),
            barcode=store_barcode("電費", issued, due, amount, k + 1, collect_after=0)))
    return specs


def draw_bill_water(rng: random.Random, n: int) -> list[BillSpec]:
    specs, dates, usages, start = [], set(), set(), rng.randrange(len(BILL_STYLES))
    for k in range(n):
        issued, due, meter = _bill_common(rng, dates)
        usage = unique(lambda: rng.randint(70, 160), usages)
        items = (("基本費", 150), ("用水費", usage * 11), ("污水下水道使用費", usage * 4))
        amount = sum(v for _, v in items)
        specs.append(BillSpec(
            "青禾自來水公司", "水費", issued, due, amount, BILL_STYLES[(k + start) % len(BILL_STYLES)],
            usage=usage, period=(meter - timedelta(days=59), meter), meter=meter, items=items,
            prev=_other_amount(rng, amount),
            barcode=store_barcode("水費", issued, due, amount, k + 1, collect_after=30)))
    return specs


def _telecom_items(rng: random.Random) -> tuple[tuple[str, int], ...]:
    """月租費 + 通話費 + 數據加量包 − 優惠折抵,加總至少 1,000。"""
    while True:
        items = (("月租費", rng.choice([1099, 1299, 1499, 1699])), ("通話費", rng.randrange(0, 310, 10)),
                 ("數據加量包", rng.choice([0, 150, 300])), ("優惠折抵", -rng.choice([0, 100, 200])))
        if sum(v for _, v in items) >= 1000:
            return items


def draw_bill_telecom(rng: random.Random, n: int) -> list[BillSpec]:
    specs, dates, amounts, start = [], set(), set(), rng.randrange(len(BILL_STYLES))
    for k in range(n):
        issued, due, _ = _bill_common(rng, dates)
        items = unique(lambda: _telecom_items(rng), amounts, key=lambda it: sum(v for _, v in it))
        total = sum(v for _, v in items)
        cycle_end = issued.replace(day=1) - timedelta(days=1)     # 計費週期:上個月整月
        specs.append(BillSpec(
            "青禾電信", "電信", issued, due, total, BILL_STYLES[(k + start) % len(BILL_STYLES)],
            period=(cycle_end.replace(day=1), cycle_end), items=items, prev=_other_amount(rng, total)))
    return specs


def draw_bill_gas(rng: random.Random, n: int) -> list[BillSpec]:
    specs, dates, usages, start = [], set(), set(), rng.randrange(len(BILL_STYLES))
    for k in range(n):
        issued, due, meter = _bill_common(rng, dates)
        usage = unique(lambda: rng.randint(32, 90), usages)
        items = (("基本費", 180), ("氣費", usage * 28))
        amount = sum(v for _, v in items)
        specs.append(BillSpec(
            "青禾瓦斯股份有限公司", "瓦斯", issued, due, amount, BILL_STYLES[(k + start) % len(BILL_STYLES)],
            usage=usage, period=(meter - timedelta(days=59), meter), meter=meter, items=items,
            prev=_other_amount(rng, amount),
            barcode=store_barcode("瓦斯", issued, due, amount, k + 1, collect_after=3)))
    return specs


def _bill_notes(page: Page, x: int, y: int, lines: list[str], size: int = 30, gap: int = 26) -> int:
    for line in lines:
        page.text(x, y, line, size, GREY)
        y += size + gap
    return y


def render_bill_power(spec: BillSpec) -> Rendered:
    """電費:標題在左上,欄位一列一列對齊,中間一個大金額框(類似 make_demo_docs 的電費單),右下有超商代收條碼。"""
    p = Page(1240, 1754)
    p.text(130, 170, spec.vendor, 44, GREY, key="vendor")
    p.text(130, 235, "電費通知單", 68, INK, bold=True)
    p.d.line((130, 335, 1110, 335), fill=GREY, width=3)
    rows = [
        ("用戶名稱", mask_name(), None), ("電號", "○○-○○-○○○○-○○-○", None),
        ("計費期間", f"{roc(spec.period[0], spec.style)} 至 {roc(spec.period[1], spec.style)}", None),
        ("抄表日期", roc(spec.meter, spec.style), None), ("出帳日期", roc(spec.issued, spec.style), "date"),
        ("本期用電度數", f"{spec.usage:,} 度", None), ("上期繳費金額", f"{money(spec.prev)} 元", None),
    ]
    y = 370
    for label, value, key in rows:
        p.text(130, y, label, 38, GREY)
        p.text(500, y, value, 38, INK, key=key)
        y += 72
    p.d.rectangle((130, 900, 1110, 1160), outline=INK, width=4)
    p.text(170, 950, "本期應繳總金額", 38)
    p.text(640, 930, f"{money(spec.amount)} 元", 80, RED, key="amount")
    box = p.text(170, 1060, "繳費期限:", 46)
    p.text(box[2] + 8, 1060, roc(spec.due, spec.style), 46, INK, key="due_date")
    _bill_notes(p, 130, 1230, ["繳費方式:超商代收、金融機構、自動扣繳。", "逾期未繳將依規定加收違約金。",
                               f"客服專線:{MASK_PHONE}"])
    draw_store_barcode(p, spec.barcode, 1110, 1440)
    return p.done()


def render_bill_water(spec: BillSpec) -> Rendered:
    """水費:藍色標題帶、左右兩個資料框、收費項目表,繳費期限放在底部的紅框裡,紅框下有超商代收條碼。"""
    p = Page(1240, 1754)
    p.d.rounded_rectangle((130, 160, 1110, 340), radius=14, fill=(22, 84, 140))
    p.text(170, 190, spec.vendor, 44, WHITE, key="vendor")
    p.text(170, 255, "水費通知單", 64, WHITE, bold=True)
    box = p.text(130, 372, "開立日期:", 36, GREY)
    p.text(box[2] + 8, 372, roc(spec.issued, spec.style), 36, INK, key="date")
    p.d.rectangle((130, 450, 600, 810), outline=GREY, width=3)
    p.d.rectangle((640, 450, 1110, 810), outline=GREY, width=3)
    left = [("戶名", [mask_name()]), ("水號", ["○○-○○○○-○"]), ("用水地址", [MASK_ADDRESS])]
    right = [("計費期間", [f"{roc(spec.period[0], spec.style)} 至", roc(spec.period[1], spec.style)]),
             ("抄表日期", [roc(spec.meter, spec.style)]), ("本期用水度數", [f"{spec.usage} 度"])]
    for x, groups in ((160, left), (670, right)):
        y = 472
        for label, values in groups:
            p.text(x, y, label, 30, GREY)
            y += 38
            for value in values:
                p.text(x, y, value, 33)
                y += 46
            y += 18
    p.d.line((130, 850, 1110, 850), fill=INK, width=3)
    p.text(160, 870, "收費項目", 34, GREY)
    p.text(1080, 870, "金額(元)", 34, GREY, anchor="ra")
    y = 940
    for name, value in spec.items:
        p.text(160, y, name, 36)
        p.text(1080, y, money(value), 36, anchor="ra")
        y += 66
    p.d.line((130, y + 6, 1110, y + 6), fill=INK, width=2)
    p.text(160, y + 30, "本期應繳總金額", 40, bold=True)
    p.text(1080, y + 22, money(spec.amount), 56, RED, bold=True, key="amount", anchor="ra")
    p.d.rectangle((130, 1270, 1110, 1400), outline=RED, width=4)
    p.text(170, 1312, "繳費期限", 40, RED)
    p.text(1070, 1304, roc(spec.due, spec.style), 56, RED, bold=True, key="due_date", anchor="ra")
    _bill_notes(p, 130, 1440, [f"客服專線:{MASK_PHONE}"], 28)
    draw_store_barcode(p, spec.barcode, 1110, 1440)
    return p.done()


def render_bill_telecom(spec: BillSpec) -> Rendered:
    """電信費(test):紫色標題、摘要卡片、費用明細表;出帳日叫「帳單日期」,期限叫「繳款期限」。"""
    p = Page(1240, 1600)
    p.d.rounded_rectangle((130, 160, 1110, 360), radius=22, fill=(88, 56, 160))
    p.text(170, 190, spec.vendor, 64, WHITE, bold=True, key="vendor")
    p.text(170, 280, "電信費帳單", 38, (230, 222, 250))
    p.text(1070, 290, "門號 ○○○○-○○○-○○○", 32, (230, 222, 250), anchor="ra")
    p.d.rounded_rectangle((130, 400, 1110, 800), radius=22, fill=(242, 238, 250), outline=(88, 56, 160), width=3)
    month = f"{spec.period[1].year - 1911}年{spec.period[1].month}月"
    p.text(170, 436, "帳單月份", 32, GREY)
    p.text(400, 432, month, 36)
    p.text(170, 506, "帳單日期", 32, GREY)
    p.text(400, 502, roc(spec.issued, spec.style), 40, key="date")
    p.text(170, 590, "本期應繳金額", 36, INK)
    p.text(1070, 570, f"NT$ {money(spec.amount)}", 72, (88, 56, 160), bold=True, key="amount", anchor="ra")
    p.text(170, 700, "繳款期限", 36, RED)
    p.text(1070, 692, roc(spec.due, spec.style), 48, RED, bold=True, key="due_date", anchor="ra")
    p.text(130, 850, "費用明細", 40, bold=True)
    p.d.line((130, 910, 1110, 910), fill=GREY, width=2)
    y = 935
    for name, value in spec.items:
        p.text(150, y, name, 34)
        p.text(1090, y, money(value), 34, anchor="ra")
        y += 62
    p.d.line((130, y + 4, 1110, y + 4), fill=GREY, width=2)
    p.text(150, y + 30, "上期應繳金額(已繳清)", 30, GREY)
    p.text(1090, y + 30, money(spec.prev), 30, GREY, anchor="ra")
    _bill_notes(p, 130, y + 130, ["繳費方式:超商代收、行動支付、自動扣繳。", f"客服專線:{MASK_PHONE}"])
    return p.done()


def render_bill_gas(spec: BillSpec) -> Rendered:
    """瓦斯費(test,新版型):橫式、左邊是可撕下的收據聯,期限叫「最後繳款日」、出帳日叫「開單日期」。"""
    p = Page(1700, 1100)
    p.d.rectangle((160, 110, 1540, 990), outline=INK, width=3)
    stub_right = 580
    for y in range(110, 990, 24):                                   # 虛線:沿線撕下
        p.d.line((stub_right, y, stub_right, y + 12), fill=GREY, width=3)
    # 左:收據聯
    p.text(190, 140, "收據聯", 36, GREY)
    p.text(190, 210, "戶名", 28, GREY)
    p.text(190, 248, mask_name(), 34)
    p.text(190, 320, "表號", 28, GREY)
    p.text(190, 358, "○○-○○○○○", 34)
    p.text(190, 430, "應繳總額", 28, GREY)
    p.text(190, 468, f"NT$ {money(spec.amount)}", 44, bold=True)
    p.text(190, 570, "最後繳款日", 28, GREY)
    p.text(190, 608, roc(spec.due, spec.style), 34)
    p.text(190, 880, "(範例,無效)", 26, LIGHT)
    # 右:通知單
    p.text(620, 140, spec.vendor, 52, bold=True, key="vendor")
    p.text(620, 215, "瓦斯費繳費通知單", 38, GREY)
    box = p.text(620, 285, "開單日期:", 32, GREY)
    p.text(box[2] + 8, 285, roc(spec.issued, spec.style), 32, key="date")
    p.text(620, 345, "抄表日期:", 28, GREY)
    p.text(620 + 130, 345, roc(spec.meter, spec.style), 28, GREY)
    p.text(1000, 345, f"用氣量 {spec.usage} 度", 28, GREY)
    p.d.line((620, 400, 1510, 400), fill=GREY, width=2)
    y = 420
    for name, value in spec.items:
        p.text(640, y, name, 32)
        p.text(1490, y, money(value), 32, anchor="ra")
        y += 54
    p.text(640, y + 6, "上期金額", 28, GREY)
    p.text(1490, y + 6, money(spec.prev), 28, GREY, anchor="ra")
    p.d.line((620, y + 56, 1510, y + 56), fill=GREY, width=2)
    p.text(640, y + 90, "應繳總額", 36, bold=True)
    p.text(1490, y + 78, f"NT$ {money(spec.amount)}", 60, RED, bold=True, key="amount", anchor="ra")
    p.text(640, y + 190, "最後繳款日:", 40, RED)
    p.text(1490, y + 184, roc(spec.due, spec.style), 50, RED, bold=True, key="due_date", anchor="ra")
    draw_store_barcode(p, spec.barcode, 1510, 790, height=40, gap=14)
    return p.done()


register(Template("bill_power", "dev", "帳單", draw_bill_power, render_bill_power))
register(Template("bill_water", "dev", "帳單", draw_bill_water, render_bill_water))
register(Template("bill_telecom", "test", "帳單", draw_bill_telecom, render_bill_telecom))
register(Template("bill_gas", "test", "帳單", draw_bill_gas, render_bill_gas))


# --- 公文:收到本函後 N 日內、請於某日前(dev),自送達之次日起 N 日內(test) ---

LETTER_FIRST = date(2026, 7, 21)           # 發文日從這天起:說明裡「依據某日申請書」的日期(發文日前 5–20 天)才不會早於 7/1
LETTER_STYLES = ("full", "cn", "full_pad")  # 發文日期的民國年寫法
LETTER_DAYS = (7, 10, 14, 15, 20, 30)       # 「N 日內」的 N
CLAUSE_NO = "一二三四五六"


@dataclass(frozen=True)
class LetterSpec:
    agency: str                         # 發文機關
    issued: date                        # 發文日期
    style: str                          # 發文日期的民國年寫法(LETTER_STYLES)
    doc_number: str                     # 發文字號
    subject: str                        # 主旨全文(印在影像上的原文)
    deadline_text: str                  # 期限原文:影像上連續印在同一行
    deadline: date                      # 期望期限:由選定的數值直接指定,不呼叫 src.dates.resolve_deadline
    actions: tuple[str, ...]            # 應辦事項(各自獨立一行印出)
    matter: str                         # 事由(說明引用)
    ref_date: date                      # 說明裡「依據」的較早日期:干擾項
    ref_number: str = ""                # 說明裡引用的前案字號:干擾項(只有部分版型有)

    def truth(self) -> dict:
        return {"vendor": self.agency, "date": self.issued.isoformat(), "amount": None, "invoice_number": None,
                "fields": {"subject": self.subject, "doc_number": self.doc_number,
                           "deadline_text": self.deadline_text, "deadline": self.deadline.isoformat(),
                           "required_actions": list(self.actions)}}


def _letter_common(rng: random.Random, dates: set, serials: set, prefix: str) -> tuple[date, str]:
    issued = unique(lambda: random_day(rng, LETTER_FIRST), dates)
    serial = unique(lambda: rng.randint(1, 999), serials)
    return issued, f"{prefix}字第{issued.year - 1911}{serial:07d}號"


def _draw_letters(rng: random.Random, n: int, *, agency: str, prefix: str, matters: tuple, action_pool: tuple,
                  build: Callable[..., tuple[str, str, date]], with_ref_number: bool = False) -> list[LetterSpec]:
    """各版型共用的抽法;build(matter, issued, days) → (主旨, 期限原文, 期望期限)。

    第 k 組只用到亂數流前面的值,--per-template 加大時前面的組不變。
    """
    order_days, order_matters = shuffled(rng, LETTER_DAYS), shuffled(rng, matters)
    start = rng.randrange(len(LETTER_STYLES))
    specs, dates, serials = [], set(), set()
    for k in range(n):
        issued, number = _letter_common(rng, dates, serials, prefix)
        matter = order_matters[k % len(order_matters)]
        subject, deadline_text, deadline = build(matter, issued, order_days[k % len(order_days)], rng)
        specs.append(LetterSpec(
            agency, issued, LETTER_STYLES[(k + start) % len(LETTER_STYLES)], number, subject, deadline_text,
            deadline, tuple(rng.sample(action_pool, 2)), matter[0], issued - timedelta(days=rng.randint(5, 20)),
            f"{prefix}字第{issued.year - 1911}{rng.randint(1, 999):07d}號" if with_ref_number else ""))
    return specs


TAX_MATTERS = (("房屋稅減免", "檢附相關證明文件補正"), ("地價稅減免", "補送應備文件"), ("使用牌照稅減免", "補送證明文件"),
               ("稅籍資料變更", "至本局辦理補正"), ("房屋稅籍分戶", "補送相關文件"), ("扣除額核認", "提出說明資料"))
TAX_ACTIONS = ("檢附戶口名簿影本", "檢附房屋使用現況照片", "填寫補正申請書並簽名", "檢附身分證影本",
               "檢附所有權人同意書", "檢附最近一期繳款書影本")
SOCIAL_MATTERS = (("敬老卡換發",), ("老人福利資格複查",), ("租金補貼資格複核",), ("老人福利登記資料更新",),
                  ("愛心卡換發",), ("戶籍與居住資料確認",))
SOCIAL_ACTIONS = ("攜帶身分證正本至本局服務櫃台", "填寫資格複查表並簽名", "提供最近3個月內的戶籍謄本",
                  "提供郵局存簿封面影本", "簽署資料確認書", "核對個人資料並簽章")
CIVIL_MATTERS = (("戶籍地址登記疑義",), ("門牌整編資料確認",), ("印鑑登記資料更新",), ("里鄰調整資料確認",),
                 ("戶籍資料更正",), ("姓名登記資料確認",))
CIVIL_ACTIONS = ("攜帶身分證正本至戶政事務所", "填寫資料更正申請書", "攜帶印章到場簽章", "檢附土地或建物所有權狀影本",
                 "檢附最近一期水電費收據影本", "簽署同意書")


def draw_notice_receipt_days(rng: random.Random, n: int) -> list[LetterSpec]:
    """稅務局補正通知:主旨裡「請於收到本函後 N 日內…」;期限 = 發文日 + N 日。"""
    def build(matter, issued, days, rng):
        text = f"收到本函後{days}日內"
        return (f"台端申請{matter[0]}一案,請於{text}{matter[1]},逾期未補正者,依規定不予受理。", text,
                issued + timedelta(days=days))
    return _draw_letters(rng, n, agency="青禾市政府稅務局", prefix="青稅", matters=TAX_MATTERS,
                         action_pool=TAX_ACTIONS, build=build)


def draw_notice_fixed_date(rng: random.Random, n: int) -> list[LetterSpec]:
    """社會局通知:說明裡「請於115年X月X日前…」;期限就是寫明的那一天(發文日後 21–45 天)。"""
    def build(matter, issued, days, rng):
        deadline = issued + timedelta(days=rng.randint(21, 45))
        return f"為辦理115年度{matter[0]}一案,請查照。", f"請於{roc(deadline, 'cn')}前", deadline
    return _draw_letters(rng, n, agency="青禾市政府社會局", prefix="青社", matters=SOCIAL_MATTERS,
                         action_pool=SOCIAL_ACTIONS, build=build)


def draw_notice_service_next_day(rng: random.Random, n: int) -> list[LetterSpec]:
    """民政局通知(test):說明裡「請自送達之次日起 N 日內…」;以發文日為起算基準,期限 = 發文日 + N 日。"""
    def build(matter, issued, days, rng):
        text = f"自送達之次日起{days}日內"
        return f"有關台端{matter[0]}一案,請查照辦理。", text, issued + timedelta(days=days)
    return _draw_letters(rng, n, agency="青禾市政府民政局", prefix="青民", matters=CIVIL_MATTERS,
                         action_pool=CIVIL_ACTIONS, build=build, with_ref_number=True)


def _actions_block(p: Page, x: int, y: int, spec: LetterSpec, size: int, kind: str, width: int) -> int:
    """(一)…。(二)…。:每項應辦事項各自一行,原文連續印出。"""
    for i, action in enumerate(spec.actions):
        y = p.paragraph(x, y, f"({CLAUSE_NO[i]}){action}。", size, width, kind, key="body", gap=12)
    return y


def render_notice_receipt_days(spec: LetterSpec) -> Rendered:
    """經典公文版面(類似 make_demo_docs 的公文):置中的「機關 函」、受文者與發文資料、主旨、說明、機關首長。"""
    p = Page(1240, 1754)
    p.text(620, 110, f"{spec.agency}  函", 60, INK, "kai", anchor="ma")
    y = 240
    for label, value, key in (("受文者:", f"{mask_name()} 君", None), ("發文日期:", roc(spec.issued, spec.style), "date"),
                              ("發文字號:", spec.doc_number, "doc_number"), ("速別:", "普通件", None)):
        box = p.text(110, y, label, 30, GREY, "kai")
        p.text(box[2] + 4, y, value, 30, INK, "kai", key=key)
        y += 52
    p.d.line((110, y + 10, 1130, y + 10), fill=GREY, width=2)
    y += 50
    box = p.text(110, y, "主旨:", 36, INK, "kai", bold=True)
    y = p.paragraph(110, y, spec.subject, 36, 1020, "kai", first=1020 - (box[2] - 110) - 4, x_first=box[2] + 4,
                    key="subject", gap=16) + 24
    p.text(110, y, "說明:", 36, INK, "kai", bold=True)
    y += 60
    y = p.paragraph(150, y, f"一、依據台端{roc(spec.ref_date, 'cn')}申請書辦理。", 32, 980, "kai", key="body") + 6
    y = p.paragraph(150, y, "二、應補正事項:", 32, 980, "kai", key="body") + 2
    y = _actions_block(p, 190, y, spec, 32, "kai", 940) + 6
    y = p.paragraph(150, y, "三、如有疑問,請洽承辦人(分機 ○○○○)。", 32, 980, "kai", key="body")
    p.text(760, y + 120, "局長  ○○○", 36, INK, "kai")
    return p.done()


def render_notice_fixed_date(spec: LetterSpec) -> Rendered:
    """通知單版面:綠色標題帶、資料表格、框起來的主旨;期限寫在說明第二點(不在主旨裡)。"""
    p = Page(1240, 1754)
    p.d.rounded_rectangle((130, 150, 1110, 330), radius=14, fill=(30, 92, 76))
    p.text(170, 182, spec.agency, 54, WHITE, bold=True)
    p.text(170, 262, "通知單", 36, (214, 236, 228))
    p.d.rectangle((130, 370, 1110, 590), outline=GREY, width=2)
    p.d.line((620, 370, 620, 590), fill=GREY, width=2)
    p.d.line((130, 480, 1110, 480), fill=GREY, width=2)
    for x, y, label, value, key in ((150, 385, "發文日期", roc(spec.issued, spec.style), "date"),
                                    (640, 385, "發文字號", spec.doc_number, "doc_number"),
                                    (150, 495, "受文者", f"{mask_name()} 君", None),
                                    (640, 495, "承辦單位", "○○科", None)):
        p.text(x, y, label, 26, GREY)
        p.text(x, y + 38, value, 32, INK, key=key)
    lines = p.wrap(spec.subject, 36, "sans", 880)
    box_h = 76 + len(lines) * 52
    p.d.rectangle((130, 630, 1110, 630 + box_h), outline=(30, 92, 76), width=3)
    p.d.rectangle((130, 630, 250, 684), fill=(30, 92, 76))
    p.text(190, 638, "主旨", 34, WHITE, anchor="ma")
    for i, line in enumerate(lines):
        p.text(160, 700 + i * 52, line, 36, INK, key="subject")
    y = 630 + box_h + 50
    p.text(130, y, "說明", 36, INK, bold=True)
    y += 62
    y = p.paragraph(160, y, f"一、依據本局115年度{spec.matter}作業要點辦理。", 32, 930, key="body") + 6
    y = p.paragraph(160, y, f"二、{spec.deadline_text}辦理下列事項:", 32, 930, key="body") + 2
    y = _actions_block(p, 200, y, spec, 32, "sans", 890) + 6
    y = p.paragraph(160, y, "三、逾期未辦理者,將依規定處理。", 32, 930, key="body") + 6
    y = p.paragraph(160, y, "四、如有疑問,請洽承辦人(分機 ○○○○)。", 32, 930, key="body")
    p.d.line((130, y + 70, 1110, y + 70), fill=LIGHT, width=2)
    p.text(130, y + 95, f"機關地址:{MASK_ADDRESS}    電話:{MASK_PHONE}", 26, GREY)
    return p.done()


def render_notice_service_next_day(spec: LetterSpec) -> Rendered:
    """正式公文版面(test):左側裝訂線與打孔、機關與承辦資訊、檔號與保存年限、正本副本與機關印信。"""
    p = Page(1240, 1754)
    for cy in (300, 877, 1454):                                  # 打孔
        p.d.ellipse((50, cy - 18, 86, cy + 18), outline=LIGHT, width=3)
    for y in range(120, 1650, 26):                               # 裝訂線
        p.d.line((150, y, 150, y + 13), fill=LIGHT, width=2)
    p.text(200, 110, spec.agency, 56, INK, "ming", bold=True)
    p.text(1090, 120, "函", 56, INK, "ming", bold=True, anchor="ra")
    p.d.line((200, 195, 1110, 195), fill=INK, width=4)
    p.d.line((200, 205, 1110, 205), fill=INK, width=1)
    p.text(1110, 225, f"地址:{MASK_ADDRESS}", 24, GREY, "ming", anchor="ra")
    p.text(1110, 258, f"承辦人:○○○  電話:{MASK_PHONE}", 24, GREY, "ming", anchor="ra")
    y = 320
    for label, value, key in (("受文者:", f"{mask_name()} 君", None), ("發文日期:", roc(spec.issued, spec.style), "date"),
                              ("發文字號:", spec.doc_number, "doc_number"), ("速別:", "最速件", None),
                              ("檔號:", "○○/○○/○○", None), ("保存年限:", "○年", None)):
        box = p.text(200, y, label, 28, GREY, "ming")
        p.text(box[2] + 4, y, value, 28, INK, "ming", key=key)
        y += 46
    p.d.line((200, y + 12, 1110, y + 12), fill=GREY, width=2)
    y += 50
    box = p.text(200, y, "主旨:", 36, INK, "ming", bold=True)
    y = p.paragraph(200, y, spec.subject, 36, 910, "ming", first=910 - (box[2] - 200) - 4, x_first=box[2] + 4,
                    key="subject", gap=16) + 24
    p.text(200, y, "說明:", 36, INK, "ming", bold=True)
    y += 60
    ref = f"一、依據本局{roc(spec.ref_date, 'cn')}{spec.ref_number}函辦理。"
    y = p.paragraph(240, y, ref, 32, 870, "ming", key="body") + 6
    y = p.paragraph(240, y, f"二、請{spec.deadline_text}辦理下列事項:", 32, 870, "ming", key="body") + 2
    y = _actions_block(p, 280, y, spec, 32, "ming", 830) + 6
    y = p.paragraph(240, y, "三、逾期未辦理者,將依規定處理。", 32, 870, "ming", key="body")
    y += 70
    p.text(200, y, f"正本:{mask_name()} 君", 28, INK, "ming")
    p.text(200, y + 46, "副本:", 28, INK, "ming")
    p.d.rectangle((900, y - 10, 1060, y + 150), outline=(190, 30, 30), width=5)      # 機關印信
    p.text(980, y + 25, spec.agency[-3:], 34, (190, 30, 30), "ming", bold=True, anchor="ma")
    p.text(980, y + 85, "印", 34, (190, 30, 30), "ming", bold=True, anchor="ma")
    return p.done()


register(Template("notice_receipt_days", "dev", "公文", draw_notice_receipt_days, render_notice_receipt_days))
register(Template("notice_fixed_date", "dev", "公文", draw_notice_fixed_date, render_notice_fixed_date))
register(Template("notice_service_next_day", "test", "公文", draw_notice_service_next_day,
                  render_notice_service_next_day))


# --- 藥袋:med_bag_a(dev,每種藥一張卡片)、med_bag_b(test,勾選格表格) ---
#
# 只照抄紙上印的字:藥名(含規格與英文名)、用法、每次幾錠、數量、天數。刻意不印用途、注意事項、副作用等,
# 標準答案也不含任何紙上沒有的資訊;timing 只放紙上明印的早/中/晚/睡前,「一天三次」這種只印次數的不換算。
# 藥名用常見的學名,風格比照 make_demo_docs 的藥袋;「需要時」只配在常見的需要時服用的幾種。

PRN_DRUGS = ("乙醯胺酚錠 500 毫克(Acetaminophen)", "氯苯那敏錠 4 毫克(Chlorpheniramine)",
             "布洛芬錠 200 毫克(Ibuprofen)", "氯雷他定錠 10 毫克(Loratadine)")
REGULAR_DRUGS = ("氨氯地平錠 5 毫克(Amlodipine)", "二甲雙胍錠 500 毫克(Metformin)",
                 "阿托伐他汀錠 10 毫克(Atorvastatin)", "法莫替丁錠 20 毫克(Famotidine)",
                 "美託洛爾錠 50 毫克(Metoprolol)", "格列美脲錠 2 毫克(Glimepiride)",
                 "洛沙坦錠 50 毫克(Losartan)", "阿斯匹靈錠 100 毫克(Aspirin)")
MULTI_TIMINGS = (("早", "晚"), ("早", "中", "晚"), ("早", "睡前"), ("中", "晚"), ("早", "中"))
SINGLE_TIMINGS = (("早",), ("中",), ("晚",), ("睡前",))
COUNT_ONLY = (("每日三次", 3), ("一天兩次", 2), ("每日兩次", 2), ("一天四次", 4))   # 第一個藥固定用「一天三次」
PRN_TEXTS = ("需要時服用", "必要時服用")
SLOT_SHORT = {"早": "早", "中": "午", "晚": "晚"}          # 藥袋上常印「午」,標準答案的 timing 一律寫「中」
BAG_STYLES = ("cn", "cn_pad", "slash")                       # 調劑日期的民國年寫法
# 每袋的組成:multi 多個時段、single 單一時段、prn 只印「需要時」、count 只印次數沒印時段;前三組就涵蓋全部型態
BAG_RECIPES = (("multi", "count", "prn"), ("multi", "single", "prn"), ("count", "single", "multi", "prn"),
               ("multi", "count"), ("prn", "multi"), ("count", "single", "multi"))


@dataclass(frozen=True)
class Drug:
    name: str                       # 藥袋上藥名那一行的原文(含規格與英文名)
    kind: str                       # multi / single / prn / count:決定怎麼印
    timing: tuple[str, ...]         # 標準答案:紙上明印的時段(prn 與 count 為空)
    prn: bool
    freq_text: str                  # prn 與 count 印的原文,例「需要時服用」「一天三次」;其餘為空
    times: int                      # count 的一天次數
    dose: int                       # 每次幾錠
    days: int                       # 給藥天數(prn 為 0,不印)
    qty: int                        # 總數量(錠)

    def usage(self) -> str:
        """卡片版面印的用法原文。"""
        if self.kind == "prn":
            return self.freq_text
        if self.kind == "count":
            return f"{self.freq_text} 飯後"
        meals = "".join(SLOT_SHORT[s] for s in self.timing if s != "睡前")
        return "、".join(([meals + "飯後"] if meals else []) + (["睡前"] if "睡前" in self.timing else []))

    def amount_text(self) -> str:
        return f"數量 {self.qty} 錠" + (f"({self.days} 日)" if self.days else "")


@dataclass(frozen=True)
class BagSpec:
    clinic: str                     # 醫療院所(標準答案的 vendor)
    issued: date                    # 調劑日期
    style: str                      # 調劑日期的民國年寫法(BAG_STYLES)
    drugs: tuple[Drug, ...]

    def truth(self) -> dict:
        return {"vendor": self.clinic, "date": self.issued.isoformat(), "amount": None, "invoice_number": None,
                "fields": {"items": [{"name": d.name, "timing": list(d.timing), "prn": d.prn}
                                     for d in self.drugs]}}


def _draw_drug(rng: random.Random, kind: str, taken: set, next_text: Callable[[str], Any]) -> Drug:
    pool = PRN_DRUGS if kind == "prn" else REGULAR_DRUGS
    name = unique(lambda: rng.choice(pool), taken)
    dose, days = rng.choice((1, 1, 2)), rng.choice((7, 14, 28))
    if kind == "prn":
        return Drug(name, kind, (), True, next_text("prn"), 0, dose, 0, rng.choice((6, 9, 12)))
    if kind == "count":
        text, times = next_text("count")
        return Drug(name, kind, (), False, text, times, dose, days, times * dose * days)
    timing = rng.choice(MULTI_TIMINGS if kind == "multi" else SINGLE_TIMINGS)
    return Drug(name, kind, timing, False, "", 0, dose, days, len(timing) * dose * days)


def draw_bags(clinic: str) -> Callable[[random.Random, int], list[BagSpec]]:
    def draw(rng: random.Random, n: int) -> list[BagSpec]:
        sequences = {"count": [("一天三次", 3)] + shuffled(rng, COUNT_ONLY),   # 第一個「只印次數」的藥一定是「一天三次」
                     "prn": list(PRN_TEXTS)}
        used = {"count": 0, "prn": 0}

        def next_text(kind: str):
            seq, used[kind] = sequences[kind], used[kind] + 1
            return seq[(used[kind] - 1) % len(seq)]

        start = rng.randrange(len(BAG_STYLES))
        specs, dates = [], set()
        for k in range(n):
            taken: set = set()
            kinds = list(BAG_RECIPES[k % len(BAG_RECIPES)])
            rng.shuffle(kinds)
            drugs = tuple(_draw_drug(rng, kind, taken, next_text) for kind in kinds)
            specs.append(BagSpec(clinic, unique(lambda: random_day(rng), dates), BAG_STYLES[(k + start) % len(BAG_STYLES)],
                                 drugs))
        return specs
    return draw


def render_med_bag_a(spec: BagSpec) -> Rendered:
    """a(dev):診所藥袋,每種藥一張卡片:藥名、用法與每次幾錠、數量(類似 make_demo_docs 的藥袋,不印用途與注意事項)。"""
    p = Page(1100, 1500)
    box = p.text(90, 100, spec.clinic, 52, INK, key="vendor")
    p.text(box[2] + 24, 100, "藥袋", 52)
    p.text(90, 185, "姓名:○○○     病歷號:○○○○○○", 30, (40, 40, 40))
    box = p.text(90, 235, "調劑日期:", 30, (40, 40, 40))
    p.text(box[2] + 4, 235, roc(spec.issued, spec.style), 30, INK, key="date")
    p.d.line((90, 295, 1010, 295), fill=GREY, width=3)
    y = 330
    for d in spec.drugs:
        p.d.rectangle((90, y, 1010, y + 215), outline=(90, 90, 90), width=2)
        p.text_fit(115, y + 18, d.name, 36, 860, fill=INK, key="name")
        p.text(115, y + 85, f"用法:{d.usage()}  每次 {d.dose} 錠", 30, INK, key="usage")
        p.text(115, y + 145, d.amount_text(), 30, INK)
        y += 245
    p.text(90, y + 20, f"調劑藥師:○○○    藥師諮詢電話:{MASK_PHONE}", 28, (40, 40, 40))
    p.text(90, y + 75, "如有任何用藥問題,請洽詢醫師或藥師。", 28, (40, 40, 40))
    return p.done()


def render_med_bag_b(spec: BagSpec) -> Rendered:
    """b(test):社區藥局的用藥明細,橫式表格——早/中/晚/睡前四欄,服用的時段在格子裡畫實心圓;版面與 a 明顯不同。"""
    p = Page(1800, 1200)
    box = p.text(120, 100, spec.clinic, 54, (22, 84, 140), bold=True, key="vendor")
    p.text(box[2] + 28, 106, "用藥明細", 46, GREY)
    p.text(120, 190, "姓名:○○○     病歷號:○○○○○○", 30, GREY)
    box = p.text(1000, 190, "調劑日期:", 30, GREY)
    p.text(box[2] + 4, 190, roc(spec.issued, spec.style), 30, INK, key="date")
    cols = [120, 740, 840, 940, 1040, 1160, 1400, 1720]           # 藥名 | 早 | 中 | 晚 | 睡前 | 服用方式 | 用量與數量
    top, head, row_h = 260, 70, 140
    bottom = top + head + row_h * len(spec.drugs)
    p.d.rectangle((cols[0], top, cols[-1], top + head), fill=(226, 236, 246))
    for i, title in enumerate(("藥名與規格", "早", "中", "晚", "睡前", "服用方式", "用量與數量")):
        p.text((cols[i] + cols[i + 1]) // 2, top + 18, title, 30, INK, anchor="ma")
    for x in cols:
        p.d.line((x, top, x, bottom), fill=GREY, width=2)
    for i in range(len(spec.drugs) + 2):
        y = top if i == 0 else top + head + (i - 1) * row_h
        p.d.line((cols[0], y, cols[-1], y), fill=GREY, width=2)
    for row, d in enumerate(spec.drugs):
        y = top + head + row * row_h
        p.text_fit(cols[0] + 18, y + 48, d.name, 32, cols[1] - cols[0] - 36, fill=INK, key="name")
        for i, slot in enumerate(MED_TIMINGS):
            if slot in d.timing:
                cx, cy = (cols[i + 1] + cols[i + 2]) // 2, y + row_h // 2
                p.d.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=INK)
                p.marks.append(Mark(f"slot:{row}", slot, (cx - 17, cy - 17, cx + 17, cy + 17)))
        way = d.freq_text if d.kind == "prn" else (f"{d.freq_text} 飯後" if d.kind == "count"
                                                    else ("飯後" if any(s != "睡前" for s in d.timing) else ""))
        if way:
            p.text_fit(cols[5] + 16, y + 52, way, 30, cols[6] - cols[5] - 28, fill=INK, key="way")
        p.text_fit(cols[6] + 18, y + 28, f"每次 {d.dose} 錠", 28, cols[7] - cols[6] - 36, fill=INK)
        p.text_fit(cols[6] + 18, y + 78, d.amount_text(), 28, cols[7] - cols[6] - 36, fill=INK)
    p.text(120, bottom + 50, f"調劑藥師:○○○    藥師諮詢電話:{MASK_PHONE}", 28, GREY)
    p.text(120, bottom + 100, "如有任何用藥問題,請洽詢醫師或藥師。", 28, GREY)
    return p.done()


register(Template("med_bag_a", "dev", "藥袋", draw_bags("青禾家庭醫學科診所"), render_med_bag_a))
register(Template("med_bag_b", "test", "藥袋", draw_bags("青禾社區藥局"), render_med_bag_b))


# --- 收據:receipt_a(dev,感熱紙明細)、receipt_b(test,國字大寫金額的一般收據);都沒有 QR ---

RECEIPT_SHOPS = (("青禾生活超市股份有限公司", "站前店"), ("青禾便利商店股份有限公司", "河濱門市"),
                 ("青禾藥妝股份有限公司", "廣場店"), ("青禾書店股份有限公司", "綠園店"),
                 ("青禾五金行", "巷口店"), ("青禾早餐坊", "公園店"))
RECEIPT_STYLES = ("iso", "slash", "cn")           # 日期寫法:2026-08-05、115/08/05、115年8月5日
FORM_PAYEES = (("青禾社區管理委員會", "115年第3季管理費"), ("青禾社區管理委員會", "停車位使用費"),
               ("青禾社區發展協會", "年度會費"), ("青禾里民活動中心", "場地清潔費"),
               ("青禾志工服務隊", "活動報名費"), ("青禾愛心協會", "會費"))
CN_DIGITS = "零壹貳參肆伍陸柒捌玖"


def date_text(day: date, style: str) -> str:
    """印在收據上的日期:西元 ISO,或民國年寫法。"""
    return day.isoformat() if style == "iso" else roc(day, style)


def chinese_amount(n: int) -> str:
    """收據常見的國字大寫金額:1250 → 壹仟貳佰伍拾、2005 → 貳仟零伍、10500 → 壹萬零伍佰;只支援 1–99,999。"""
    if not 0 < n < 100_000:
        raise ValueError(f"只支援 1–99,999:{n}")

    def section(m: int) -> str:                       # 0–9999
        out, zero = "", False
        for pos, ch in zip((3, 2, 1, 0), f"{m:04d}"):
            if ch == "0":
                zero = True
                continue
            out += ("零" if zero and out else "") + CN_DIGITS[int(ch)] + ("仟", "佰", "拾", "")[3 - pos]
            zero = False
        return out

    wan, rest = divmod(n, 10_000)
    return (CN_DIGITS[wan] + "萬" if wan else "") + ("零" if wan and 0 < rest < 1000 else "") + section(rest)


@dataclass(frozen=True)
class ShopReceipt:
    vendor: str
    branch: str
    issued: date
    style: str                                  # 日期寫法(RECEIPT_STYLES)
    items: tuple[tuple[str, int, int], ...]     # (品名, 數量, 小計)

    @property
    def amount(self) -> int:
        return sum(sub for _, _, sub in self.items)

    def truth(self) -> dict:
        return {"vendor": self.vendor, "date": self.issued.isoformat(), "amount": self.amount,
                "invoice_number": None, "fields": {}}


@dataclass(frozen=True)
class FormReceipt:
    vendor: str                                 # 收款單位
    purpose: str                                # 繳納事由
    issued: date
    style: str                                  # 日期寫法:full、full_pad
    amount: int
    serial: int                                 # 收據編號:干擾項(不是發票號碼,標準答案的 invoice_number 是 null)

    def truth(self) -> dict:
        return {"vendor": self.vendor, "date": self.issued.isoformat(), "amount": self.amount,
                "invoice_number": None, "fields": {}}


def draw_receipt_a(rng: random.Random, n: int) -> list[ShopReceipt]:
    specs, dates, amounts = [], set(), set()
    start = rng.randrange(len(RECEIPT_STYLES))
    for k in range(n):
        vendor, branch = rng.choice(RECEIPT_SHOPS)

        def draw_items() -> tuple:
            names = rng.sample(make_sample.ITEMS, rng.randint(2, 5))
            lines = []
            for name in names:
                qty, unit = rng.randint(1, 3), rng.randint(20, 500)
                lines.append((name, qty, qty * unit))
            return tuple(lines)

        items = unique(draw_items, amounts, key=lambda lines: sum(sub for _, _, sub in lines))
        specs.append(ShopReceipt(vendor, branch, unique(lambda: random_day(rng), dates),
                                 RECEIPT_STYLES[(k + start) % len(RECEIPT_STYLES)], items))
    return specs


def draw_receipt_b(rng: random.Random, n: int) -> list[FormReceipt]:
    specs, dates, amounts = [], set(), set()
    start = rng.randrange(2)
    for k in range(n):
        vendor, purpose = rng.choice(FORM_PAYEES)
        amount = unique(lambda: rng.randrange(300, 9951, 50), amounts)
        specs.append(FormReceipt(vendor, purpose, unique(lambda: random_day(rng), dates),
                                 ("full", "full_pad")[(k + start) % 2], amount, rng.randint(1, 999)))
    return specs


def render_receipt_a(spec: ShopReceipt) -> Rendered:
    """a(dev):感熱紙的免用統一發票收據,窄幅、白底,品名與小計逐行列出(沒有 QR)。"""
    height = 640 + 56 * len(spec.items)
    p = Page(760, height, WHITE)
    p.text(380, 70, "免用統一發票收據", 46, INK, bold=True, anchor="ma")
    p.text_fit(380, 140, spec.vendor, 34, 640, key="vendor", anchor="ma")
    p.text(380, 192, spec.branch, 28, GREY, anchor="ma")
    box = p.text(60, 262, "日期:", 28, INK)
    p.text(box[2] + 4, 262, date_text(spec.issued, spec.style), 28, INK, key="date")
    p.d.line((50, 318, 710, 318), fill=INK, width=2)
    y = 340
    for name, qty, sub in spec.items:
        p.text(60, y, f"{name}  x{qty}", 28, INK)
        p.text(700, y, money(sub), 28, INK, anchor="ra")
        y += 56
    p.d.line((50, y + 6, 710, y + 6), fill=INK, width=2)
    p.text(60, y + 36, "合計金額", 34, INK)
    p.text(700, y + 30, f"{money(spec.amount)} 元", 46, INK, bold=True, key="amount", anchor="ra")
    p.text(60, y + 130, "此據  收訖", 28, GREY)
    return p.done()


def render_receipt_b(spec: FormReceipt) -> Rendered:
    """b(test):一般收據的表單版面(橫式、楷體、雙線框):金額同時印國字大寫與阿拉伯數字,收款單位蓋章。"""
    p = Page(1500, 900, PAPER)
    p.d.rectangle((110, 110, 1390, 790), outline=INK, width=4)
    p.d.rectangle((122, 122, 1378, 778), outline=INK, width=1)
    p.text(750, 150, "收    據", 72, INK, "kai", bold=True, anchor="ma")
    p.text(1330, 176, f"No. {spec.serial:07d}", 30, GREY, "kai", anchor="ra")
    p.text(170, 270, date_text(spec.issued, spec.style), 36, INK, "kai", key="date")
    p.text(170, 350, f"茲收到  {mask_name()} 君  繳納  {spec.purpose}", 42, INK, "kai")
    box = p.text(170, 450, f"新臺幣  {chinese_amount(spec.amount)}  元整", 46, INK, "kai", bold=True, key="amount_cn")
    p.text(box[2] + 60, 458, f"(NT$ {money(spec.amount)})", 40, INK, "kai", key="amount")
    p.text(170, 650, f"收款單位:{spec.vendor}", 38, INK, "kai", key="vendor")
    p.text(940, 658, f"經手人:{mask_name()}", 34, INK, "kai")
    p.d.rectangle((1250, 600, 1340, 690), outline=(190, 30, 30), width=5)           # 收款單位的印章
    p.text(1295, 622, "印", 46, (190, 30, 30), "kai", bold=True, anchor="ma")
    return p.done()


register(Template("receipt_a", "dev", "收據", draw_receipt_a, render_receipt_a))
register(Template("receipt_b", "test", "收據", draw_receipt_b, render_receipt_b))


# --- 電子發票:einvoice_a(dev)、einvoice_b(test,每組另有一張 QR 汙損到解不開的 qr_damaged) ---
#
# 版面與 QR 都沿用 tools/make_sample.py 的產生方式(財政部規格的左右 QR、Code 39 一維條碼);
# 證明聯的版面是財政部規定的,只有一種,所以 a、b 的差別只在店名池與數值(不是版面的泛化證據)。
# 店名一律帶「示範」與「青禾」;統編是通過檢查碼的假號碼,加密驗證資訊是隨機字串。

EINVOICE_STORES_A = (("青禾示範商行", "河濱店"), ("青禾示範書店", "公園店"), ("青禾示範早餐坊", "站前店"))
EINVOICE_STORES_B = (("青禾示範超市", "大道門市"), ("青禾示範文具行", "老街店"), ("青禾示範藥妝", "廣場店"))
EINVOICE_PAD = (72, 100)        # 證明聯外圍加的白邊(左右、上下):讓 crop 有留白可裁、傾斜後不貼邊


@dataclass(frozen=True)
class EInvoice:
    spec: Any                   # make_sample.EInvoiceSpec

    def truth(self) -> dict:
        s = self.spec
        return {"vendor": s.store, "date": s.issued.date().isoformat(), "amount": s.total,
                "invoice_number": s.invoice_number,
                "fields": {"seller_tax_id": s.seller_tax_id, "random_code": s.random_code, "period": s.period}}


def draw_einvoices(stores: tuple) -> Callable[[random.Random, int], list[EInvoice]]:
    def draw(rng: random.Random, n: int) -> list[EInvoice]:
        specs, dates, numbers = [], set(), set()
        start = rng.randrange(len(stores))
        for k in range(n):
            store, branch = stores[(k + start) % len(stores)]
            day = unique(lambda: random_day(rng), dates)
            issued = datetime.combine(day, time(8)) + timedelta(minutes=rng.randrange(780), seconds=rng.randrange(60))
            items = [(name, rng.randint(1, 3), rng.randint(15, 300))
                     for name in rng.sample(make_sample.ITEMS, rng.randint(2, 4))]
            number = unique(lambda: "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
                            + str(rng.randrange(10_000_000, 100_000_000)), numbers)
            specs.append(EInvoice(make_sample.EInvoiceSpec(
                filename=f"e{k + 1:02d}", store=store, branch=branch, issued=issued, invoice_number=number,
                random_code=f"{rng.randrange(10_000):04d}", seller_tax_id=make_sample.fake_tax_id(rng), items=items,
                encrypted="".join(rng.choice(make_sample._B64) for _ in range(22)) + "==")))
        return specs
    return draw


def render_einvoice(invoice: EInvoice) -> Rendered:
    """make_sample 畫的證明聯(600 寬)外圍加白邊;記下左右 QR 的位置(亮斑要避開、汙損要蓋上去)。"""
    spec, (pad_x, pad_y) = invoice.spec, EINVOICE_PAD
    tight = make_sample.render_einvoice(spec)
    img = Image.new("RGB", (tight.width + 2 * pad_x, tight.height + 2 * pad_y), WHITE)
    img.paste(tight, (pad_x, pad_y))
    left, right = make_sample._qr_pair(*spec.qr_texts(), box=4)       # 位置同 make_sample.render_einvoice 貼的地方
    marks = [Mark("qr", "left", (pad_x + 50, pad_y + 480, pad_x + 50 + left.width, pad_y + 480 + left.height)),
             Mark("qr", "right", (pad_x + 550 - right.width, pad_y + 480, pad_x + 550, pad_y + 480 + right.height))]
    return Rendered(img, marks, content_box(img, WHITE), WHITE)


def _blot(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, rng: random.Random, fill) -> None:
    """一塊不規則的墨漬。"""
    pts = []
    for k in range(11):
        ang, rad = 2 * math.pi * k / 11 + rng.uniform(-0.2, 0.2), r * rng.uniform(0.85, 1.2)
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=fill)


def damage_qr(img: Image.Image, rng: random.Random, rendered: Rendered, strength: int = 0) -> Image.Image:
    """左側 QR 的三個定位圖樣各蓋一塊墨漬、再劃幾道刮痕:解不開,證明聯上其餘的字(日期、金額、發票號碼)完好。

    strength 加大墨漬(產生器發現這樣還解得開時才會調高)。右側 QR 不動:它本來就不是左側 QR。
    """
    out = img.copy()
    d = ImageDraw.Draw(out)
    x0, y0, x1, y1 = rendered.boxes("qr")[0]
    for cx, cy in ((x0 + 30, y0 + 30), (x1 - 30, y0 + 30), (x0 + 30, y1 - 30)):     # 三個定位圖樣的中心
        _blot(d, cx, cy, rng.uniform(26, 34) + 6 * strength, rng, rng.choice([(35, 30, 28), (60, 45, 30), (20, 20, 20)]))
    for _ in range(3 + strength):
        d.line((rng.uniform(x0, x1), rng.uniform(y0, y1), rng.uniform(x0, x1), rng.uniform(y0, y1)),
               fill=(235, 235, 235), width=rng.randint(4, 7))
    return out


def read_left_qr(image: Image.Image):
    """用系統自己的解碼流程(src.verify.einvoice)讀影像的左側 QR;解不開回 None。"""
    from src.verify import einvoice
    bgr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    return einvoice.find_left_qr(einvoice.decode_qr_texts(bgr))


def qr_matches(qr: Any, spec: Any) -> bool:
    """解出來的左側 QR 與這張證明聯的規格一致(字軌、開立日期、總計、賣方統編)。"""
    return (qr is not None and qr.invoice_number == spec.invoice_number and qr.date == spec.issued.date().isoformat()
            and qr.total_amount == spec.total and qr.seller_tax_id == spec.seller_tax_id)


register(Template("einvoice_a", "dev", "發票", draw_einvoices(EINVOICE_STORES_A), render_einvoice, has_qr=True))
register(Template("einvoice_b", "test", "發票", draw_einvoices(EINVOICE_STORES_B), render_einvoice, has_qr=True,
                  extra_conditions=("qr_damaged",)))


# --- 計畫、產生與寫出 ---

@dataclass
class Item:
    """一組數值:同一個版型的第 variant 組,會畫成 conditions 裡的每一張影像。"""

    template: Template
    variant: int
    spec: Any

    @property
    def conditions(self) -> list[str]:
        return self.template.conditions(self.variant)

    def file(self, condition: str) -> str:
        return f"{self.template.split}/{self.template.name}_{self.variant:02d}_{condition}.png"

    def label(self, condition: str) -> dict:
        t, truth = self.template, self.spec.truth()
        return {
            "file": self.file(condition), "split": t.split, "template": t.name, "variant": self.variant,
            "condition": condition, "doc_type": t.doc_type, "vendor": truth["vendor"], "date": truth["date"],
            "amount": truth["amount"], "invoice_number": truth["invoice_number"], "fields": truth["fields"],
            "qr": {"present": t.has_qr, "readable": t.has_qr and condition != "qr_damaged"},
            "barcode": {"present": getattr(self.spec, "barcode", None) is not None},
        }


def plan(seed: int = DEFAULT_SEED, per_template: int = DEFAULT_PER_TEMPLATE,
         templates: list[str] | None = None) -> list[Item]:
    """決定每個版型的每一組數值(不畫圖)。順序:dev 的版型在前,各版型依登記順序,組別由小到大。"""
    names = list(templates) if templates else list(TEMPLATES)
    unknown = [n for n in names if n not in TEMPLATES]
    if unknown:
        raise ValueError(f"不認得的版型:{unknown};可用:{list(TEMPLATES)}")
    if per_template < 1:
        raise ValueError("--per-template 至少要 1")
    chosen = [TEMPLATES[n] for n in TEMPLATES if n in names]
    chosen.sort(key=lambda t: SPLITS.index(t.split))
    items: list[Item] = []
    for t in chosen:
        specs = t.draw(random.Random(f"{seed}:{t.name}"), per_template)
        items += [Item(t, k, spec) for k, spec in enumerate(specs, start=1)]
    return items


def labels_for(items: list[Item]) -> list[dict]:
    return [item.label(c) for item in items for c in item.conditions]


def _render_condition(item: Item, rendered: Rendered, watermarked: Image.Image, condition: str,
                      rng: random.Random) -> Image.Image:
    if condition == "clean":
        return watermarked
    if condition in PRE_WATERMARK:
        return make_demo_docs._watermark(DEGRADES[condition](rendered.image, rng, rendered))
    if condition in DEGRADES:
        return DEGRADES[condition](watermarked, rng, rendered)
    if condition == "qr_damaged" and item.template.has_qr:
        for strength in range(4):           # 汙損後還解得開就加重,直到解不開為止
            damaged = make_demo_docs._watermark(damage_qr(rendered.image, rng, rendered, strength))
            if read_left_qr(damaged) is None:
                return damaged
        raise RuntimeError(f"{item.file(condition)}:QR 汙損到最重還是解得開")
    raise ValueError(f"版型 {item.template.name} 不會畫條件 {condition}")


def render_item(item: Item, seed: int = DEFAULT_SEED) -> dict[str, Image.Image]:
    """畫出一組數值的每一張影像:條件 → 影像。浮水印加在降質之前(降質模擬拍照,浮水印是文件本身的一部分)。

    電子發票畫完會用系統自己的解碼流程(src.verify.einvoice)確認:標了 readable 的解得開且與標準答案一致,
    qr_damaged 解不開;印有超商代收條碼的帳單,clean 影像要用 src.verify.billcode 解得開且與規格一致。
    不一致就丟 RuntimeError,不會寫出標準答案說謊的資料集。
    """
    rendered = item.template.render(item.spec)
    watermarked = make_demo_docs._watermark(rendered.image)
    barcode = getattr(item.spec, "barcode", None)
    images: dict[str, Image.Image] = {}
    for condition in item.conditions:
        rng = random.Random(f"{seed}:{item.template.name}:{item.variant}:{condition}")
        images[condition] = _render_condition(item, rendered, watermarked, condition, rng)
        if item.template.has_qr and condition != "qr_damaged" and not qr_matches(read_left_qr(images[condition]),
                                                                                    item.spec.spec):
            raise RuntimeError(f"{item.file(condition)}:左側 QR 解不開或與標準答案不一致(換一個 --seed 試試)")
        if barcode and condition == "clean" and read_store_barcode(images[condition]) != barcode:
            raise RuntimeError(f"{item.file(condition)}:超商代收條碼解不開或與規格不一致")
    return images


def clear_previous(out: Path) -> int:
    """重產前清掉上一次留下的影像(只清舊 labels.jsonl 裡列出、且在 out 底下的 .png),回傳清掉幾張。"""
    labels_path, removed = out / "labels.jsonl", 0
    if not labels_path.is_file():
        return 0
    root = out.resolve()
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        try:
            rel = json.loads(line).get("file")
        except (ValueError, AttributeError):
            continue
        if not isinstance(rel, str) or not rel.endswith(".png"):
            continue
        target = (out / rel).resolve()
        if target.is_relative_to(root) and target.is_file():
            target.unlink()
            removed += 1
    return removed


def generate(out: Path, seed: int = DEFAULT_SEED, per_template: int = DEFAULT_PER_TEMPLATE,
             templates: list[str] | None = None) -> list[dict]:
    """產生影像與 labels.jsonl 到 out,回傳標準答案清單。重產會先清掉上一次的影像。

    每一筆標準答案寫出前都過 validate_label:契約(與 EV-3WAY 共用)被改壞時直接中止,不寫出格式不對的資料集。
    """
    items = plan(seed, per_template, templates)
    problems = [f"{label['file']}:{p}" for label in labels_for(items) for p in validate_label(label)]
    if problems:
        raise ValueError("標準答案不符 labels.jsonl 契約:\n" + "\n".join(problems[:20]))
    check_fonts()
    out.mkdir(parents=True, exist_ok=True)
    clear_previous(out)
    labels: list[dict] = []
    for item in items:
        for condition, image in render_item(item, seed).items():
            label = item.label(condition)
            target = out / label["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
            labels.append(label)
    with open(out / "labels.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for label in labels:
            f.write(json.dumps(label, ensure_ascii=False) + "\n")
    return labels


def summarize(labels: list[dict]) -> list[str]:
    lines = []
    for split in SPLITS:
        rows = [r for r in labels if r["split"] == split]
        if not rows:
            continue
        lines.append(f"{split}:{len(rows)} 張,{len({r['template'] for r in rows})} 個版型")
        for name in dict.fromkeys(r["template"] for r in rows):
            mine = [r for r in rows if r["template"] == name]
            conds = {}
            for r in mine:
                conds[r["condition"]] = conds.get(r["condition"], 0) + 1
            lines.append(f"  {name}({mine[0]['doc_type']}):{len(mine)} 張 "
                         + "、".join(f"{c} {n}" for c, n in conds.items()))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生多版型的合成評測集(影像 + labels.jsonl)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"輸出資料夾(預設 {DEFAULT_OUT})")
    parser.add_argument("--per-template", type=int, default=DEFAULT_PER_TEMPLATE,
                        help=f"每個版型幾組數值(預設 {DEFAULT_PER_TEMPLATE};每組一張 clean + 一張降質)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"隨機種子(固定可重現,預設 {DEFAULT_SEED})")
    parser.add_argument("--templates", default=None,
                        help="只產生這些版型(逗號分隔,預設全部);可用:" + "、".join(TEMPLATES))
    args = parser.parse_args(argv)
    names = [n.strip() for n in args.templates.split(",") if n.strip()] if args.templates else None
    try:
        plan(args.seed, args.per_template, names)       # 先檢查版型名稱與組數,參數不對就在這裡擋下、什麼都不寫
    except ValueError as exc:
        parser.error(str(exc))
    labels = generate(args.out, args.seed, args.per_template, names)
    print(f"已產生 {len(labels)} 張(seed={args.seed},每版型 {args.per_template} 組)到 {args.out}")
    for line in summarize(labels):
        print(line)
    print(f"標準答案:{args.out / 'labels.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
