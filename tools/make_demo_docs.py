"""產生 Demo 用的合成文件:帳單、藥袋、公文(電子發票由 make_sample.py 產生)。

用法(專案根目錄):
    python tools/make_demo_docs.py                         # 輸出到 data/samples/demo/,日期以今天推算
    python tools/make_demo_docs.py --as-of 2026-11-07      # 指定展示日(要先印出來的話,用展示當天)
    python tools/make_demo_docs.py --out <資料夾>

日期全部由展示日推算(demo_dates):帳單 5 天前出帳、13 天後到期,公文 2 天前發文(收到本函後 15 日內),
藥袋前一天調劑——展示當天首頁的「要記得的事」一定看得到期限提醒。電費單印有超商代收三段條碼(金額 1,286 元)。

所有內容皆為虛構:機關、診所、公司名稱都冠上「範例」,姓名以「○」遮蔽,
電話與編號為無效格式;每張左上角與對角線都有「範例 SAMPLE」浮水印,
避免被誤認為真實文件。影片與 repo 只能使用這些合成文件。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
for _path in (str(BASE_DIR), str(TOOLS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import code39  # noqa: E402  電費單的超商代收三段條碼

DEFAULT_OUT = BASE_DIR / "data" / "samples" / "demo"
BILL_AMOUNT = 1286


def demo_dates(as_of: date) -> dict:
    """Demo 文件上的日期,全部由展示日推算。

    限制(核對與首頁的規則):文件日期不能晚於當天 + 1 天;期限要在展示日或之後,才不會一上傳就標成逾期。
    公文的期限由程式從發文日算(收到本函後 15 日內 = 發文日 + 15 天),也落在展示日之後。
    """
    bill_issued = as_of - timedelta(days=5)
    period_end = bill_issued.replace(day=1) - timedelta(days=1)                  # 計費期間:出帳前兩個整月
    period_start = (period_end.replace(day=1) - timedelta(days=1)).replace(day=1)
    return {
        "bill_issued": bill_issued, "bill_due": as_of + timedelta(days=13), "bill_period": (period_start, period_end),
        "dispensed": as_of - timedelta(days=1),
        "letter_issued": as_of - timedelta(days=2), "letter_applied": as_of - timedelta(days=20),
    }


def bill_barcode(dates: dict) -> tuple[str, str, str]:
    """電費單的三段條碼:代收期限與應繳日期都 = 繳費期限,金額 1,286 元;項目代號與銷帳編號是虛構的。"""
    from src.verify import billcode
    issued, due = dates["bill_issued"], dates["bill_due"]
    return billcode.build_store_barcodes(due, "6DM", f"DM{issued:%Y%m%d}000001", BILL_AMOUNT, pay_date=due)


def _roc(day: date, pad: bool = True) -> str:
    if pad:
        return f"{day.year - 1911}年{day.month:02d}月{day.day:02d}日"
    return f"{day.year - 1911}年{day.month}月{day.day}日"

# 依序嘗試的繁中字型:macOS、Windows、Linux
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    print("警告:找不到繁中字型,改用 PIL 預設字型(中文會變方塊)", file=sys.stderr)
    return ImageFont.load_default()


WATERMARK_ALPHA = 14   # 對角線浮水印的不透明度(0~255):38 會蓋住字,模型把被蓋到的主旨、藥品清單當成讀不清(10/27)


def _watermark(img: Image.Image) -> Image.Image:
    """左上角標籤 + 對角線淡色浮水印(只染色、不蓋住字)。"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    big = _font(img.width // 8)
    step = img.height // 3
    for y in range(-step, img.height + step, step):
        d.text((img.width // 10, y), "範例 SAMPLE", font=big, fill=(200, 0, 0, WATERMARK_ALPHA))
    overlay = overlay.rotate(28, center=(img.width // 2, img.height // 2))
    out = Image.alpha_composite(img.convert("RGBA"), overlay)
    d2 = ImageDraw.Draw(out)
    d2.rectangle((16, 16, 196, 70), fill=(185, 28, 28, 255))
    d2.text((30, 22), "範例", font=_font(36), fill=(255, 255, 255, 255))
    return out.convert("RGB")


def _page(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), (252, 252, 250))
    return img, ImageDraw.Draw(img)


def make_bill(dates: dict) -> Image.Image:
    img, d = _page(1240, 1600)
    title, body, small = _font(56), _font(38), _font(30)
    d.text((110, 110), "範例電力公司  電費通知單", font=title, fill=(20, 20, 20))
    d.line((110, 200, 1130, 200), fill=(60, 60, 60), width=3)
    start, end = dates["bill_period"]
    rows = [
        ("用戶名稱", "陳○○"),
        ("電號", "00-00-0000-00-0"),
        ("計費期間", f"{_roc(start)} 至 {_roc(end)}"),
        ("出帳日期", _roc(dates["bill_issued"])),
        ("本期用電度數", "412 度"),
    ]
    y = 250
    for k, v in rows:
        d.text((130, y), k, font=body, fill=(70, 70, 70))
        d.text((480, y), v, font=body, fill=(20, 20, 20))
        y += 70
    d.rectangle((110, 640, 1130, 900), outline=(30, 30, 30), width=4)
    d.text((150, 680), "本期應繳總金額", font=body, fill=(20, 20, 20))
    d.text((620, 660), f"{BILL_AMOUNT:,} 元", font=_font(76), fill=(160, 0, 0))
    d.text((150, 790), f"繳費期限:{_roc(dates['bill_due'])}", font=_font(46), fill=(20, 20, 20))
    notes = [
        "繳費方式:超商代收、金融機構、自動扣繳。",
        "逾期未繳將依規定加收違約金。",
        "客服專線:0000-000-000(範例號碼)",
    ]
    y = 960
    for n in notes:
        d.text((130, y), n, font=small, fill=(60, 60, 60))
        y += 56
    # 超商代收條碼:窄條 3 像素,印在紙上用手機拍也讀得到;條碼下不印號碼
    d.text((1130, 1210), "超商代收專用條碼(範例)", font=small, fill=(60, 60, 60), anchor="ra")
    bars = code39.stack([code39.draw_code39(s, module=3, height=60) for s in bill_barcode(dates)], gap=24)
    img.paste(bars, (1130 - bars.width, 1260))
    return _watermark(img)


def make_medication_bag(dates: dict) -> Image.Image:
    img, d = _page(1100, 1500)
    title, body, small = _font(52), _font(36), _font(30)
    d.text((90, 100), "範例診所  藥袋", font=title, fill=(20, 20, 20))
    d.text((90, 180), f"姓名:陳○○   性別:女   調劑日期:{_roc(dates['dispensed'])}", font=small,
           fill=(40, 40, 40))
    d.line((90, 240, 1010, 240), fill=(60, 60, 60), width=3)
    drugs = [
        ("乙醯胺酚錠 500 毫克(Acetaminophen)", "每日三次,早午晚飯後,每次 1 錠", "數量 21 錠(7 日)", "用途:解熱鎮痛"),
        ("氯苯那敏錠 4 毫克(Chlorpheniramine)", "需要時(皮膚癢時)服用,每次 1 錠", "數量 6 錠", "注意:可能引起嗜睡,服藥後避免開車"),
    ]
    y = 280
    for name, usage, qty, note in drugs:
        d.rectangle((90, y, 1010, y + 290), outline=(90, 90, 90), width=2)
        d.text((115, y + 20), name, font=body, fill=(20, 20, 20))
        d.text((115, y + 90), usage, font=small, fill=(30, 30, 30))
        d.text((115, y + 150), qty, font=small, fill=(30, 30, 30))
        d.text((115, y + 210), note, font=small, fill=(150, 0, 0))
        y += 330
    d.text((90, 980), "調劑藥師:林○○    藥師諮詢電話:00-0000-0000(範例號碼)", font=small, fill=(40, 40, 40))
    d.text((90, 1040), "如有任何用藥問題,請洽詢醫師或藥師。", font=small, fill=(40, 40, 40))
    return _watermark(img)


def make_official_letter(dates: dict) -> Image.Image:
    img, d = _page(1240, 1754)
    title, body, small = _font(60), _font(36), _font(30)
    d.text((440, 110), "範例市稅務局  函", font=title, fill=(20, 20, 20))
    meta = [
        "受文者:陳○○ 君",
        f"發文日期:中華民國{_roc(dates['letter_issued'], pad=False)}",
        "發文字號:範稅字第1150000001號",
        "速別:普通件",
    ]
    y = 240
    for m in meta:
        d.text((110, y), m, font=small, fill=(40, 40, 40))
        y += 52
    d.line((110, y + 10, 1130, y + 10), fill=(60, 60, 60), width=2)
    y += 50
    d.text((110, y), "主旨:台端申請房屋稅減免一案,請於收到本函後15日內", font=body, fill=(20, 20, 20))
    d.text((110, y + 56), "      檢附相關證明文件補正,逾期未補正者,依規定不予受理。", font=body, fill=(20, 20, 20))
    y += 170
    d.text((110, y), "說明:", font=body, fill=(20, 20, 20))
    lines = [
        f"一、依據台端{_roc(dates['letter_applied'], pad=False)}申請書辦理。",
        "二、應補正文件:戶口名簿影本、房屋使用現況照片各一份。",
        "三、補正方式:親自送達、郵寄或線上申辦(範例網址)。",
        "四、如有疑問,請洽承辦人(範例分機 0000)。",
    ]
    y += 60
    for line in lines:
        d.text((150, y), line, font=small, fill=(30, 30, 30))
        y += 56
    d.text((760, y + 120), "局長  ○○○", font=body, fill=(20, 20, 20))
    return _watermark(img)


DOCS = {
    "範例帳單_電費.png": make_bill,
    "範例藥袋.png": make_medication_bag,
    "範例公文_補正通知.png": make_official_letter,
}


def generate(out: Path, as_of: date) -> list[Path]:
    """依展示日畫出三份文件到 out,回傳檔案路徑。"""
    out.mkdir(parents=True, exist_ok=True)
    dates, paths = demo_dates(as_of), []
    for name, make in DOCS.items():
        path = out / name
        make(dates).save(path)
        paths.append(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 Demo 用合成帳單/藥袋/公文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="輸出資料夾")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="展示日 YYYY-MM-DD(預設今天);文件日期與期限都由它推算")
    args = parser.parse_args()
    as_of = args.as_of or date.today()
    for path in generate(Path(args.out), as_of):
        print(f"已產生 {path}")
    print(f"展示日:{as_of.isoformat()}(帳單期限 {demo_dates(as_of)['bill_due'].isoformat()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
