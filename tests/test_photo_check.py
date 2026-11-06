"""拍照檢查(src/photo_check.py)與 POST /upload/check 的測試。

影像全部是程式畫出來的假文件(測試只用合成資料)。這些圖只拿來確認程式照設計運作,
不拿來定門檻、也不是成效數字:門檻是用真實公文量的(見 src/photo_check.py 的說明)。不呼叫模型、不連網。
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter

from src.photo_check import (
    BLUR_WIDTH, CHECK_EDGE, HINT_BLUR, HINT_BOTH, HINT_CUT, check_photo, edge_width, photo_hint,
)
from src.providers import MockAnalyzer
from src.store import Store
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app

DESK = (82, 64, 48)       # 深色桌面
PAPER = (242, 241, 236)


def _text(draw: ImageDraw.ImageDraw, left: int, top: int, right: int, bottom: int) -> None:
    """在一塊紙上畫幾行「字」(粗細不一的黑色筆畫與方塊,橫的直的都有),當成假文件的內容。"""
    y = top
    while y + 40 < bottom:
        x = left
        while x + 60 < right:
            draw.rectangle((x, y, x + 34, y + 6), fill=(20, 20, 20))             # 橫筆畫
            draw.rectangle((x + 14, y, x + 20, y + 30), fill=(20, 20, 20))       # 直筆畫
            draw.rectangle((x + 40, y + 12, x + 52, y + 30), fill=(40, 40, 40))  # 方塊
            x += 70
        y += 60


def full_page(size=(1200, 1600)) -> Image.Image:
    """掃描檔那樣整張都是紙(看不到紙外)。"""
    img = Image.new("RGB", size, PAPER)
    _text(ImageDraw.Draw(img), 80, 80, size[0] - 80, size[1] - 80)
    return img


def on_desk(corners, size=(1200, 900)) -> Image.Image:
    """紙放在深色桌面上拍的照片;corners 是紙的四角(左上、右上、右下、左下),可以超出畫面(紙沒拍進去的部分)。"""
    img = Image.new("RGB", size, DESK)
    draw = ImageDraw.Draw(img)
    draw.polygon(corners, fill=PAPER)
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(corners, fill=255)
    content = Image.new("RGB", size, PAPER)
    _text(ImageDraw.Draw(content), min(xs) + 60, min(ys) + 60, max(xs) - 60, max(ys) - 60)
    img.paste(content, mask=mask.filter(ImageFilter.MinFilter(41)))   # 字只畫在紙裡,離紙邊留一點
    return img


def jpeg(img: Image.Image, quality: int = 80) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def blurred(img: Image.Image, radius: float) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(radius))


INSIDE = [(250, 100), (950, 110), (940, 820), (260, 810)]           # 整張都在畫面裡
CUT_BOTTOM = [(250, 150), (950, 150), (950, 1300), (250, 1300)]      # 下半截在畫面外:看得到兩個角
CUT_CORNER = [(300, 100), (1350, 160), (1150, 850), (250, 760)]      # 右上角在畫面外:看得到三個角


# ---- 模糊 ---------------------------------------------------------------------------------------

def test_sharp_page_gets_no_hint():
    check = check_photo(full_page())
    assert check.edge_width is not None and check.edge_width < 3
    assert check.hint is None


def test_clearly_blurry_page_says_blurry():
    check = check_photo(blurred(full_page(), 4))
    assert check.edge_width > BLUR_WIDTH and not check.cut_off
    assert check.hint == HINT_BLUR


def test_slightly_soft_page_is_not_flagged():
    """門檻保守:輕微的柔焦(手機照片常見)不提示,寧可漏報"""
    assert check_photo(blurred(full_page(), 1)).hint is None


def test_blank_page_cannot_be_judged():
    """沒有字(空白頁、一片牆)就沒有邊緣可以量:不判斷、不提示"""
    check = check_photo(Image.new("RGB", (1200, 900), PAPER))
    assert check.edge_width is None and check.hint is None


def test_blur_only_counts_the_paper_when_paper_is_found():
    """找到紙時只看紙的內部:清楚的紙放在模糊的背景前不算模糊"""
    photo = on_desk(INSIDE)
    background = blurred(Image.new("RGB", photo.size, DESK), 0)
    noise = ImageDraw.Draw(background)
    for x in range(0, photo.width, 90):              # 背景上的軟邊(焦外的東西)
        noise.rectangle((x, 0, x + 40, photo.height), fill=(120, 100, 80))
    background = blurred(background, 6)
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).polygon(INSIDE, fill=255)
    background.paste(photo, mask=mask)
    assert check_photo(background).hint is None


# ---- 缺角 ---------------------------------------------------------------------------------------

def test_whole_sheet_on_the_desk_gets_no_hint():
    assert check_photo(on_desk(INSIDE)).hint is None


@pytest.mark.parametrize("corners", [CUT_BOTTOM, CUT_CORNER], ids=["two-corners", "three-corners"])
def test_sheet_running_off_the_frame_says_cut(corners):
    check = check_photo(on_desk(corners))
    assert check.cut_off and not check.blurry
    assert check.hint == HINT_CUT


def test_page_filling_the_frame_is_not_called_cut():
    """紙貼滿畫面(掃描檔、拍得很近)看不到紙外,分不出有沒有缺,不提示"""
    assert not check_photo(full_page()).cut_off


def test_corner_barely_touching_the_frame_is_not_called_cut():
    """只有一個紙角剛好碰到(或超出一點點)畫面邊界:字都拍到了,不提示"""
    assert not check_photo(on_desk([(250, 100), (1206, 110), (940, 820), (260, 810)])).cut_off


def test_no_corner_visible_is_not_called_cut():
    """紙超出三邊、一個角都看不到:分不清是缺角還是拍得很近,不提示"""
    assert not check_photo(on_desk([(300, -200), (1500, -200), (1500, 1200), (300, 1200)])).cut_off


def test_blurry_and_cut_says_both():
    assert check_photo(blurred(on_desk(CUT_BOTTOM), 4)).hint == HINT_BOTH


# ---- 收到的位元組 -----------------------------------------------------------------------------

def test_photo_hint_reads_the_browser_jpeg():
    assert photo_hint(jpeg(blurred(full_page(), 4))) == HINT_BLUR
    assert photo_hint(jpeg(full_page())) is None


@pytest.mark.parametrize("data", [
    b"", b"not an image", b"\xff\xd8\xff\xe0" + b"\x00" * 64,      # 空的、不是圖、壞掉的 JPEG
], ids=["empty", "text", "broken-jpeg"])
def test_unreadable_bytes_get_no_hint(data):
    assert photo_hint(data) is None


def test_only_browser_sized_jpegs_are_judged():
    """瀏覽器送的是長邊不超過 CHECK_EDGE 的 JPEG;PNG、太大、太小的圖都不判斷(不是我們的頁面送的,或分不出來)"""
    blurry = blurred(full_page(), 4)
    png = io.BytesIO()
    blurry.save(png, format="PNG")
    assert photo_hint(png.getvalue()) is None
    assert photo_hint(jpeg(blurry.resize((CHECK_EDGE // 3 * 2, CHECK_EDGE + 8)))) is None
    assert photo_hint(jpeg(blurry.resize((240, 320)))) is None


def test_edge_width_is_about_two_pixels_for_crisp_edges():
    img = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(img).rectangle((200, 150, 600, 450), fill="black")
    assert 1.5 <= edge_width(img) <= 2.5


# ---- POST /upload/check --------------------------------------------------------------------------

class NoModel(MockAnalyzer):
    """拍照檢查不能叫模型:叫了就讓測試失敗。"""

    def analyze(self, *args, **kwargs):
        raise AssertionError("拍照檢查不應該呼叫模型")


@pytest.fixture
def analyzer(cfg):
    return NoModel(cfg)


def _check(client, data: bytes, **extra):
    return client.post("/upload/check", files={"photo": ("check.jpg", data, "image/jpeg")}, data=extra)


def _files(cfg) -> set:
    """資料夾裡所有的檔(資料庫以外):檢查前後要一模一樣。"""
    found = set()
    for root in (cfg.paths.inbox, cfg.paths.archive, cfg.paths.review, cfg.paths.failed, cfg.paths.logs,
                 cfg.paths.uploads_path):
        if root.exists():
            found |= {(p.relative_to(root.parent).as_posix(), p.stat().st_size) for p in root.rglob("*")
                      if p.is_file() and p != cfg.paths.db_path and not p.name.startswith(cfg.paths.db_path.name)}
    return found


def test_check_route_answers_with_the_hint_only(cfg, client):
    r = _check(client, jpeg(blurred(full_page(), 4)))
    assert r.status_code == 200 and r.json() == {"hint": HINT_BLUR}
    assert r.headers["cache-control"] == "no-store"
    assert _check(client, jpeg(on_desk(CUT_BOTTOM))).json() == {"hint": HINT_CUT}
    assert _check(client, jpeg(full_page())).json() == {"hint": None}
    assert _check(client, b"garbage").json() == {"hint": None}
    assert client.post("/upload/check", data={}).json() == {"hint": None}     # 沒附照片


def test_check_route_keeps_nothing(cfg, client):
    """不存檔、不寫紀錄、不送模型(NoModel 一被叫就失敗)"""
    client.get("/")
    before_files, before_events = _files(cfg), Store(cfg.paths.db_path).list_audit_events(limit=1000)
    before_docs = Store(cfg.paths.db_path).list_documents(limit=None)
    for photo in (blurred(full_page(), 4), on_desk(CUT_BOTTOM), full_page()):
        assert _check(client, jpeg(photo)).status_code == 200
    assert _files(cfg) == before_files
    assert Store(cfg.paths.db_path).list_audit_events(limit=1000) == before_events
    assert Store(cfg.paths.db_path).list_documents(limit=None) == before_docs


def test_check_route_needs_the_csrf_token(app):
    raw = TestClient(app)
    raw.get("/")
    r = raw.post("/upload/check", files={"photo": ("check.jpg", jpeg(full_page()), "image/jpeg")})
    assert r.status_code == 403
    r = raw.post("/upload/check", files={"photo": ("check.jpg", jpeg(full_page()), "image/jpeg")},
                 data={CSRF_FIELD: "x" * 43})
    assert r.status_code == 403


class Viewer:
    """不在家庭成員名單上的人:只能看,不能上傳(can_upload 是 False)。"""

    def resolve(self, request):
        return Identity("stranger@example.test", False)


def test_check_route_needs_upload_permission(cfg):
    viewer = TestClient(create_app(cfg, analyzer=NoModel(cfg), identity_resolver=Viewer()))
    viewer.get("/")
    r = viewer.post("/upload/check", files={"photo": ("check.jpg", jpeg(blurred(full_page(), 4)), "image/jpeg")},
                    data={CSRF_FIELD: viewer.cookies[CSRF_COOKIE]})
    assert r.status_code == 403 and "hint" not in r.text


def test_check_route_refuses_big_bodies_before_reading(cfg, client):
    """縮小的照片不到 1MB;更大的整份不收(和其他表單同一個上限),也不留下檔案"""
    before = _files(cfg)
    r = _check(client, b"\xff\xd8" + b"\x00" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert _files(cfg) == before
