"""產生影片 Demo 用的合成文件:帳單、藥袋、公文(電子發票由 make_sample.py 產生)。

用法(專案根目錄):
    python tools/make_demo_docs.py              # 輸出到 data/samples/demo/
    python tools/make_demo_docs.py --out <資料夾>

所有內容皆為虛構:機關、診所、公司名稱都冠上「範例」,姓名以「○」遮蔽,
電話與編號為無效格式;每張左上角與對角線都有「範例 SAMPLE」浮水印,
避免被誤認為真實文件。影片與 repo 只能使用這些合成文件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = BASE_DIR / "data" / "samples" / "demo"

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


def _watermark(img: Image.Image) -> Image.Image:
    """左上角標籤 + 對角線淡色浮水印。"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    big = _font(img.width // 8)
    step = img.height // 3
    for y in range(-step, img.height + step, step):
        d.text((img.width // 10, y), "範例 SAMPLE", font=big, fill=(200, 0, 0, 38))
    overlay = overlay.rotate(28, center=(img.width // 2, img.height // 2))
    out = Image.alpha_composite(img.convert("RGBA"), overlay)
    d2 = ImageDraw.Draw(out)
    d2.rectangle((16, 16, 196, 70), fill=(185, 28, 28, 255))
    d2.text((30, 22), "範例", font=_font(36), fill=(255, 255, 255, 255))
    return out.convert("RGB")


def _page(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), (252, 252, 250))
    return img, ImageDraw.Draw(img)


def make_bill() -> Image.Image:
    img, d = _page(1240, 1600)
    title, body, small = _font(56), _font(38), _font(30)
    d.text((110, 110), "範例電力公司  電費通知單", font=title, fill=(20, 20, 20))
    d.line((110, 200, 1130, 200), fill=(60, 60, 60), width=3)
    rows = [
        ("用戶名稱", "陳○○"),
        ("電號", "00-00-0000-00-0"),
        ("計費期間", "115年08月01日 至 115年09月30日"),
        ("出帳日期", "115年10月01日"),
        ("本期用電度數", "412 度"),
    ]
    y = 250
    for k, v in rows:
        d.text((130, y), k, font=body, fill=(70, 70, 70))
        d.text((480, y), v, font=body, fill=(20, 20, 20))
        y += 70
    d.rectangle((110, 640, 1130, 900), outline=(30, 30, 30), width=4)
    d.text((150, 680), "本期應繳總金額", font=body, fill=(20, 20, 20))
    d.text((620, 660), "1,286 元", font=_font(76), fill=(160, 0, 0))
    d.text((150, 790), "繳費期限:115年10月20日", font=_font(46), fill=(20, 20, 20))
    notes = [
        "繳費方式:超商代收、金融機構、自動扣繳。",
        "逾期未繳將依規定加收違約金。",
        "客服專線:0000-000-000(範例號碼)",
    ]
    y = 960
    for n in notes:
        d.text((130, y), n, font=small, fill=(60, 60, 60))
        y += 56
    return _watermark(img)


def make_medication_bag() -> Image.Image:
    img, d = _page(1100, 1500)
    title, body, small = _font(52), _font(36), _font(30)
    d.text((90, 100), "範例診所  藥袋", font=title, fill=(20, 20, 20))
    d.text((90, 180), "姓名:陳○○   性別:女   調劑日期:115年09月30日", font=small, fill=(40, 40, 40))
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


def make_official_letter() -> Image.Image:
    img, d = _page(1240, 1754)
    title, body, small = _font(60), _font(36), _font(30)
    d.text((440, 110), "範例市稅務局  函", font=title, fill=(20, 20, 20))
    meta = [
        "受文者:陳○○ 君",
        "發文日期:中華民國115年9月28日",
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
        "一、依據台端115年9月10日申請書辦理。",
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


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 Demo 用合成帳單/藥袋/公文")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="輸出資料夾")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, make in DOCS.items():
        path = out / name
        make().save(path)
        print(f"已產生 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
