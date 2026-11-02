"""影像前處理測試:EXIF 轉正、縮圖、PDF 轉 PNG、中繼資料移除、歪斜校正、裁白邊、
找紙張並拉正、放大紙張、補光、拉對比、細長單據、掃描 PDF 的原解析度。

所有影像都在測試中用 PIL/OpenCV 合成(白紙、黑塊當字、深色有紋路的背景),不讀任何真實文件。
"""
import io

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from src import preprocess
from src.config import PreprocessConfig
from src.preprocess import (
    MAX_LONG_EDGE,
    MAX_PIXELS,
    MAX_TALL_EDGE,
    STEP_CONTRAST,
    STEP_DOCUMENT,
    STEP_ENLARGE,
    STEP_LIGHT,
    STEP_SCAN,
    UnreadableImageError,
    deskew_image,
    enlarge_paper,
    estimate_skew,
    even_light,
    find_document,
    load_image_bytes,
    prepare_image,
    scanned_page_image,
    stretch_contrast,
    warp_document,
)

OFF = PreprocessConfig(find_document=False, even_light=False, contrast=False)   # 只剩原本的四步

_EXIF_ORIENTATION = 0x0112
_EXIF_MAKE = 0x010F


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _text_like(width: int = 900, height: int = 1200) -> Image.Image:
    """合成一張「像文件」的影像:白底、多行水平黑色文字列(以短黑塊模擬字)。"""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for row, y in enumerate(range(80, height - 80, 45)):
        x = 60 + (row % 3) * 15
        while x < width - 120:
            w = 18 + (x * 7 + row * 13) % 22
            draw.rectangle([x, y, x + w, y + 16], fill="black")
            x += w + 9
    return img


# --- EXIF 轉正 ---


def test_exif_orientation_6_is_transposed(tmp_path):
    # 以「橫放」的像素儲存(寬 300、高 200),Orientation=6 表示顯示時需順時針轉 90°
    img = Image.new("RGB", (300, 200), "white")
    img.paste((255, 0, 0), (0, 0, 40, 40))  # 左上角紅色標記
    exif = Image.Exif()
    exif[_EXIF_ORIENTATION] = 6
    path = tmp_path / "rotated.jpg"
    img.save(path, format="JPEG", exif=exif.tobytes(), quality=95)

    # 只看轉正:白底上一小塊標記會被當成小張文件裁掉,這裡關掉裁白邊
    out = _decode(prepare_image(path, deskew=False, crop=False).data)

    assert out.size == (200, 300)  # 轉正後變直式
    # 順時針 90°:原本的左上角移到右上角
    r, g, b = out.convert("RGB").getpixel((out.width - 10, 10))
    assert r > 200 and g < 80 and b < 80
    assert out.getexif().get(_EXIF_ORIENTATION) in (None, 1)  # 不可再帶轉向標記,避免被轉兩次


def test_metadata_is_stripped_before_sending(tmp_path):
    # 手機照片常帶拍攝裝置、GPS 等中繼資料;送給模型(尤其雲端)前一律去除
    img = Image.new("RGB", (120, 80), "white")
    exif = Image.Exif()
    exif[_EXIF_MAKE] = "SyntheticCam"
    path = tmp_path / "photo.jpg"
    img.save(path, format="JPEG", exif=exif.tobytes())

    out = _decode(prepare_image(path).data)

    assert _EXIF_MAKE not in out.getexif()


# --- 存檔的原件去中繼資料(上傳後立刻做) ---

_GPS_IFD = 0x8825


def _phone_photo(path, orientation=6):
    """像手機照片:帶拍攝裝置、GPS 位置與轉向標記(以橫放的像素儲存,左上角紅色標記)。"""
    img = Image.new("RGB", (300, 200), "white")
    img.paste((255, 0, 0), (0, 0, 40, 40))
    exif = Image.Exif()
    exif[_EXIF_MAKE] = "SyntheticCam"
    exif[_EXIF_ORIENTATION] = orientation
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"
    gps[2] = (25.0, 2.0, 30.0)
    img.save(path, format="JPEG", exif=exif.tobytes(), quality=95)
    with Image.open(path) as check:
        assert check.getexif().get_ifd(_GPS_IFD), "前提:照片裡真的有 GPS"
    return path


def test_stored_photo_loses_gps_and_device_but_keeps_its_orientation(tmp_path):
    from src.preprocess import strip_metadata
    path = _phone_photo(tmp_path / "photo.jpg")
    assert strip_metadata(path) is True
    with Image.open(path) as out:
        exif = out.getexif()
        assert not exif.get_ifd(_GPS_IFD) and _EXIF_MAKE not in exif and exif.get(_EXIF_ORIENTATION) in (None, 1)
        assert out.size == (200, 300)                                   # 轉向套到像素上,看起來還是正的
        r, g, b = out.convert("RGB").getpixel((out.width - 10, 10))
        assert r > 200 and g < 80 and b < 80
    assert not list(tmp_path.glob("*.tmp"))


def test_png_text_chunks_are_removed(tmp_path):
    from PIL import PngImagePlugin

    from src.preprocess import strip_metadata
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "合成測試")
    path = tmp_path / "scan.png"
    Image.new("RGB", (40, 30), "white").save(path, pnginfo=info)
    assert strip_metadata(path) is True
    with Image.open(path) as out:
        assert not getattr(out, "text", {})


def test_clean_images_and_pdfs_are_left_untouched(tmp_path):
    from src.preprocess import strip_metadata
    clean = tmp_path / "clean.png"
    Image.new("RGB", (40, 30), "white").save(clean)
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% synthetic\n")
    before = clean.read_bytes(), pdf.read_bytes()
    assert strip_metadata(clean) is False and strip_metadata(pdf) is False
    assert (clean.read_bytes(), pdf.read_bytes()) == before


def test_unreadable_file_is_left_for_the_pipeline_to_fail(tmp_path):
    from src.preprocess import strip_metadata
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    assert strip_metadata(broken) is False and broken.read_bytes().startswith(b"\xff\xd8")
    assert not list(tmp_path.glob("*.tmp"))


# --- 縮圖 ---


def test_oversized_image_is_downscaled_keeping_aspect(tmp_path):
    path = tmp_path / "huge.png"
    Image.new("RGB", (5000, 1000), "white").save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    assert max(out.size) == MAX_LONG_EDGE
    assert out.width / out.height == pytest.approx(5.0, rel=0.01)


def test_oversized_portrait_jpeg_is_downscaled(tmp_path):
    path = tmp_path / "phone.jpg"
    Image.new("RGB", (3000, 4000), "white").save(path, format="JPEG")

    out = _decode(prepare_image(path, deskew=False).data)

    assert out.size[1] == MAX_LONG_EDGE
    assert out.size[0] == pytest.approx(MAX_LONG_EDGE * 3 / 4, abs=2)


def test_small_image_is_not_upscaled(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (320, 240), "white").save(path)

    out = _decode(prepare_image(path).data)

    assert out.size == (320, 240)


# --- 輸出格式 ---


def test_jpeg_stays_jpeg_and_png_stays_png(tmp_path):
    jpg = tmp_path / "a.jpg"
    png = tmp_path / "b.png"
    Image.new("RGB", (64, 64), "white").save(jpg, format="JPEG")
    Image.new("RGB", (64, 64), "white").save(png)

    prepared_jpg = prepare_image(jpg)
    prepared_png = prepare_image(png)

    assert prepared_jpg.mime_type == "image/jpeg"
    assert _decode(prepared_jpg.data).format == "JPEG"
    assert prepared_png.mime_type == "image/png"
    assert _decode(prepared_png.data).format == "PNG"


def test_transparent_png_is_flattened_on_white(tmp_path):
    # 透明背景直接丟掉 alpha 會變黑底,模型讀不到黑字
    img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    path = tmp_path / "transparent.png"
    img.save(path)

    out = _decode(prepare_image(path).data)

    assert out.mode == "RGB"
    assert out.getpixel((25, 25)) == (255, 255, 255)


def test_pdf_is_rendered_to_png(tmp_path):
    path = tmp_path / "doc.pdf"
    Image.new("RGB", (400, 600), "white").save(path, format="PDF")

    prepared = prepare_image(path)

    assert prepared.mime_type == "image/png"
    out = _decode(prepared.data)
    assert out.format == "PNG"
    assert out.height > out.width  # 直式頁面維持直式
    assert load_image_bytes(path) == prepared.data  # 舊入口行為一致


def test_undecodable_image_is_never_sent_raw(tmp_path):
    # 解不開就無法去除 EXIF/GPS:不把原檔送出,改丟出清楚的錯誤(Pipeline 記成 failed)
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not really a jpeg")

    with pytest.raises(UnreadableImageError, match="broken.jpg"):
        prepare_image(path)


def test_unreadable_image_error_does_not_carry_the_full_path(tmp_path):
    # PIL 的原文帶完整路徑(這台電腦的資料夾位置);這個例外訊息會進處理紀錄、資料庫與匯出,只留檔名與例外種類
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not really a jpeg")

    with pytest.raises(UnreadableImageError) as err:
        prepare_image(path)

    message = str(err.value)
    assert "broken.jpg" in message and "UnidentifiedImageError" in message
    assert tmp_path.name not in message and str(tmp_path) not in message
    assert "cannot identify" in str(err.value.__cause__)  # 原始例外仍掛在 __cause__,除錯時看得到


def test_image_that_cannot_be_reencoded_is_never_sent_raw(tmp_path, monkeypatch):
    # 打得開但重新編碼失敗,同樣不能退回原檔(原檔帶著中繼資料)
    path = tmp_path / "photo.jpg"
    exif = Image.Exif()
    exif[_EXIF_MAKE] = "SyntheticCam"
    Image.new("RGB", (120, 80), "white").save(path, format="JPEG", exif=exif.tobytes())

    def boom(img, fmt):
        raise OSError("編碼器壞了")

    monkeypatch.setattr(preprocess, "_encode", boom)
    with pytest.raises(UnreadableImageError):
        prepare_image(path)


# --- 歪斜校正 ---


def test_estimate_skew_detects_clear_tilt():
    tilted = _text_like().rotate(5, resample=Image.BICUBIC, expand=True, fillcolor="white")

    angle = estimate_skew(tilted)

    # 回傳的是「要轉回來的角度」:逆時針歪 5° → 要轉 -5°
    assert angle == pytest.approx(-5.0, abs=0.6)


def test_deskew_straightens_tilted_document():
    tilted = _text_like().rotate(-6, resample=Image.BICUBIC, expand=True, fillcolor="white")

    fixed = deskew_image(tilted)

    assert fixed is not tilted
    assert abs(estimate_skew(fixed)) < 1.0


def test_deskew_leaves_straight_or_slightly_tilted_image_alone():
    straight = _text_like()
    slight = straight.rotate(1, resample=Image.BICUBIC, expand=True, fillcolor="white")

    assert deskew_image(straight) is straight
    assert deskew_image(slight) is slight  # 小於門檻不轉,避免無謂的重新取樣


def test_deskew_leaves_blank_or_photo_like_image_alone():
    blank = Image.new("RGB", (400, 400), "white")
    dark = Image.new("RGB", (400, 400), (20, 20, 20))

    assert deskew_image(blank) is blank
    assert deskew_image(dark) is dark


def test_deskew_failure_returns_original(monkeypatch):
    def boom(_img):
        raise RuntimeError("偵測失敗")

    monkeypatch.setattr(preprocess, "estimate_skew", boom)
    img = _text_like()

    assert deskew_image(img) is img


def test_prepare_image_deskews_tilted_scan(tmp_path):
    path = tmp_path / "tilted.png"
    _text_like().rotate(4, resample=Image.BICUBIC, expand=True, fillcolor="white").save(path)

    out = _decode(prepare_image(path).data)

    assert abs(estimate_skew(out)) < 1.0


# --- 裁掉白邊 ---


def _receipt_on_page(receipt: tuple[int, int] = (600, 1700)) -> Image.Image:
    """合成「A4 掃描(300 DPI),上面只有一小張收據」:白底,收據在左上,約佔 1/4 寬、1/2 高。"""
    page = Image.new("RGB", (2480, 3508), "white")
    page.paste(_text_like(*receipt), (420, 260))
    return page


def _ink_share(img: Image.Image) -> tuple[float, float]:
    """黑色內容的外框佔整張影像的比例(寬、高)。"""
    left, top, right, bottom = img.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()
    return (right - left) / img.width, (bottom - top) / img.height


def _dark_pixels(img: Image.Image) -> int:
    return sum(img.convert("L").histogram()[:128])


def test_white_margins_are_cropped_so_small_receipt_fills_the_image(tmp_path):
    # 掃描的小張收據只佔 A4 一角:整頁縮小後送進模型,字只剩幾個像素高,統編讀不出來(10/5 實測)
    path = tmp_path / "scan.png"
    _receipt_on_page().save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    wide, tall = _ink_share(out)
    assert wide > 0.8 and tall > 0.8
    assert max(out.size) <= MAX_LONG_EDGE


def test_cropping_never_drops_content(tmp_path):
    # 裁的只能是空白:裁完的黑色像素和原圖一樣多(裁完不超過長邊上限,沒有重新取樣)
    page = _receipt_on_page()
    path = tmp_path / "scan.png"
    page.save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    assert _dark_pixels(out) == _dark_pixels(page)


def test_faint_content_is_kept_when_cropping(tmp_path):
    # 褪色的熱感紙、淡色的複寫聯也是內容:收據下方一塊很淡的字(灰階 205)要留在裁切範圍內
    page = _receipt_on_page(receipt=(600, 1200))
    draw = ImageDraw.Draw(page)
    for y in range(1600, 1800, 45):
        draw.rectangle([480, y, 1000, y + 16], fill=(205, 205, 205))
    faint = page.convert("L").histogram()[205]
    path = tmp_path / "faint.png"
    page.save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    assert out.height < 2000                       # 有裁(整頁縮小的話高度是長邊上限 2048)
    assert out.convert("L").histogram()[205] == faint


def test_dust_specks_do_not_stop_cropping(tmp_path):
    # 掃描器玻璃上的灰塵:離內容很遠的幾個小點不算內容,照樣裁到收據
    page = _receipt_on_page()
    draw = ImageDraw.Draw(page)
    for x, y in [(2400, 3400), (2350, 150), (80, 3300)]:
        draw.rectangle([x, y, x + 3, y + 3], fill="black")
    path = tmp_path / "dusty.png"
    page.save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    # 裁到收據(約 600 × 1700)加留邊;被雜點擋住的話會是整頁縮小後的 1448 × 2048
    assert out.width < 800 and out.height < 1950


def test_full_page_document_is_not_cropped(tmp_path):
    # 內容本來就佔滿整頁:省不了多少,不裁
    path = tmp_path / "page.png"
    _text_like(900, 1200).save(path)

    assert _decode(prepare_image(path, deskew=False).data).size == (900, 1200)


def test_photo_on_dark_background_is_cropped_to_the_paper(tmp_path):
    # 手機拍桌上的收據:背景不是白的,裁白邊分不出空白;找到紙張就裁到紙張(四周只多 DOC_PAD 那一點背景)
    photo = Image.new("RGB", (1600, 1200), (90, 70, 50))
    photo.paste(_text_like(400, 700), (200, 150))
    path = tmp_path / "photo.png"
    photo.save(path)

    prepared = prepare_image(path, deskew=False)

    assert _decode(prepared.data).size == pytest.approx((400, 700), abs=12)
    assert prepared.steps == (STEP_DOCUMENT,)


def test_photo_is_left_whole_when_finding_the_paper_is_off(tmp_path):
    # 關掉找紙張就是原本的做法:背景不是白的,不裁
    photo = Image.new("RGB", (1600, 1200), (90, 70, 50))
    photo.paste(_text_like(400, 700), (200, 150))
    path = tmp_path / "photo.png"
    photo.save(path)

    prepared = prepare_image(path, deskew=False, options=OFF)

    assert _decode(prepared.data).size == (1600, 1200) and prepared.steps == ()


def test_crop_failure_sends_the_uncropped_image(tmp_path, monkeypatch):
    # 裁切只是加分:偵測出錯就照原樣送,不能讓文件辨識失敗
    def boom(_img):
        raise RuntimeError("偵測失敗")

    monkeypatch.setattr(preprocess, "content_box", boom)
    path = tmp_path / "scan.png"
    _receipt_on_page().save(path)

    out = _decode(prepare_image(path, deskew=False).data)

    assert max(out.size) == MAX_LONG_EDGE          # 整頁縮到長邊上限


def test_crop_can_be_turned_off(tmp_path):
    path = tmp_path / "scan.png"
    _receipt_on_page().save(path)

    out = _decode(prepare_image(path, deskew=False, crop=False).data)

    assert max(out.size) == MAX_LONG_EDGE


def test_small_receipt_in_pdf_is_rendered_large_enough_to_read(tmp_path):
    # PDF 原本用 144 DPI 轉圖:A4 一角的小收據高度只有八百多像素。改用較高解析度,裁掉白邊後約一千六百像素
    path = tmp_path / "scan.pdf"
    _receipt_on_page().save(path, format="PDF", resolution=300.0)

    out = _decode(prepare_image(path, deskew=False).data)

    wide, tall = _ink_share(out)
    assert wide > 0.8 and tall > 0.8
    assert out.height >= 1500


def test_huge_pdf_page_is_rendered_within_a_pixel_budget(tmp_path):
    # 海報尺寸的頁面不能照固定倍率轉圖(會佔掉幾百 MB 記憶體):依頁面大小降低倍率
    path = tmp_path / "poster.pdf"
    Image.new("RGB", (3000, 4000), "white").save(path, format="PDF")   # 3000 × 4000 pt

    rendered = preprocess._render_pdf(path)

    assert max(rendered.size) <= preprocess.PDF_MAX_EDGE
    assert rendered.height > rendered.width


# ---- 找到紙張並拉正(照片) ---------------------------------------------------------

PAPER = (500, 750)
# 斜拍:紙的四角落在畫面上的位置(左上、右上、右下、左下),梯形
ANGLED = [[520, 160], [1040, 205], [1080, 995], [470, 960]]


def _photo(corners=ANGLED, paper=PAPER, size=(1600, 1200), header=False, seed=0) -> Image.Image:
    """合成手機照片:深色、有紋路的桌面上放一張「像文件」的紙,依 corners 做透視變形。"""
    rng = np.random.default_rng(seed)
    width, height = size
    table = np.clip(rng.normal(0, 14, (height, width, 3)) + [95, 72, 52], 0, 255).astype(np.uint8)
    sheet = _text_like(*paper)
    if header:
        ImageDraw.Draw(sheet).rectangle([0, 0, paper[0], paper[1] // 5], fill=(30, 45, 110))   # 深色標題列
    source = np.float32([[0, 0], [paper[0] - 1, 0], [paper[0] - 1, paper[1] - 1], [0, paper[1] - 1]])
    matrix = cv2.getPerspectiveTransform(source, np.float32(corners))
    warped = cv2.warpPerspective(np.asarray(sheet), matrix, size)
    mask = cv2.warpPerspective(np.full(paper[::-1], 255, np.uint8), matrix, size)
    return Image.fromarray(np.where(mask[..., None] > 0, warped, table))


def _brown_share(img: Image.Image) -> float:
    """桌面那種棕色像素的比例(紅明顯大於藍、又不亮)。"""
    rgb = np.asarray(img.convert("RGB")).astype(int)
    return float(((rgb[..., 0] - rgb[..., 2] > 25) & (rgb.max(axis=2) < 170)).mean())


def test_angled_photo_is_found_and_straightened():
    photo = _photo()

    quad = find_document(photo)

    diagonal = float(np.hypot(*photo.size))
    assert quad is not None
    assert float(np.abs(quad - np.float32(ANGLED)).max()) < 0.02 * diagonal    # 四角都找對(含往外放的那一點)
    flat = warp_document(photo, quad)
    assert 1.3 < flat.height / flat.width < 1.7                                 # 拉回直式的長方形
    assert _brown_share(flat) < 0.05                                            # 桌面幾乎都裁掉了


def test_scans_and_full_frame_documents_have_no_paper_to_find():
    # 紙外沒有比較暗的背景:交給裁白邊,不動
    assert find_document(_text_like(900, 1200)) is None
    assert find_document(_receipt_on_page()) is None


def test_dark_header_is_not_mistaken_for_the_paper():
    # 紙上的深色標題列也是四邊形:要找的是整張紙,不能只裁出標題那一塊
    photo = _photo(header=True)

    quad = find_document(photo)

    assert quad is not None
    assert cv2.contourArea(quad) > 0.9 * cv2.contourArea(np.float32(ANGLED))


def test_noise_has_no_paper():
    noise = Image.fromarray(np.random.default_rng(3).integers(0, 255, (800, 800, 3), dtype=np.uint8))
    assert find_document(noise) is None


def test_finding_the_paper_fails_safely(tmp_path, monkeypatch):
    # 找紙張只是加分:出錯就照原本的做法送,不能讓辨識失敗
    def boom(_img):
        raise RuntimeError("偵測失敗")

    monkeypatch.setattr(preprocess, "find_document", boom)
    path = tmp_path / "photo.png"
    _photo().save(path)

    prepared = prepare_image(path, deskew=False)

    assert STEP_DOCUMENT not in prepared.steps
    assert prepared.data == prepare_image(path, deskew=False, options=PreprocessConfig(find_document=False)).data


def test_jpeg_photo_is_cropped_before_it_is_shrunk(tmp_path):
    # 4000×3000 的照片,紙約佔一半:先縮成 2048 再裁的話,紙只剩一千多像素高;先裁再縮就有 2048
    rng = np.random.default_rng(1)
    table = np.clip(rng.normal(0, 14, (3000, 4000, 3)) + [95, 72, 52], 0, 255).astype(np.uint8)
    photo = Image.fromarray(table)
    photo.paste(_text_like(1500, 2300), (1200, 350))
    path = tmp_path / "phone.jpg"
    photo.save(path, quality=90)

    prepared = prepare_image(path, deskew=False)

    assert prepared.steps[0] == STEP_DOCUMENT
    assert max(_decode(prepared.data).size) == MAX_LONG_EDGE


def test_straightened_paper_is_enlarged_to_the_limit_when_turned_on(tmp_path):
    # 拉正後只剩紙,像素比整張照片少;打開放大紙張,長邊放大到上限、長寬比不變
    path = tmp_path / "photo.png"
    _photo().save(path)

    plain = prepare_image(path, deskew=False)
    enlarged = prepare_image(path, deskew=False, options=PreprocessConfig(enlarge_paper=True))

    small, big = _decode(plain.data), _decode(enlarged.data)
    assert STEP_ENLARGE not in plain.steps and max(small.size) < MAX_LONG_EDGE
    assert {STEP_DOCUMENT, STEP_ENLARGE} <= set(enlarged.steps)
    assert max(big.size) == MAX_LONG_EDGE
    assert big.width / big.height == pytest.approx(small.width / small.height, rel=0.01)


def test_only_straightened_paper_is_enlarged(tmp_path):
    # 掃描檔、數位檔、找不到紙的照片:照舊只縮不放
    path = tmp_path / "small.png"
    _text_like(600, 800).save(path)

    prepared = prepare_image(path, deskew=False, options=PreprocessConfig(enlarge_paper=True))

    assert STEP_ENLARGE not in prepared.steps
    assert _decode(prepared.data).size == (600, 800)


def test_enlarging_stops_at_the_limit():
    assert enlarge_paper(Image.new("RGB", (1500, 2000))) is None                 # 差不到 5%:不重新取樣
    assert enlarge_paper(Image.new("RGB", (200, 300))).size == (600, 900)        # 最多 3 倍
    assert enlarge_paper(Image.new("RGB", (400, 1600))).size == (512, MAX_LONG_EDGE)
    tall = enlarge_paper(Image.new("RGB", (400, 1600)), tall_pixels=True)        # 細長單據:面積上限
    assert tall.width * tall.height <= MAX_PIXELS and max(tall.size) <= MAX_TALL_EDGE
    assert tall.width > 512


# ---- 補光、拉對比 -------------------------------------------------------------------

def _paper_spread(img: Image.Image) -> float:
    """紙面(亮的像素)亮度第 95 與第 5 百分位的差;四周留一圈不算。"""
    gray = np.asarray(img.convert("L")).astype(float)[60:-60, 60:-60]
    paper = gray[gray > 90]
    return float(np.percentile(paper, 95) - np.percentile(paper, 5))


def _shadowed(strength: float = 0.45) -> Image.Image:
    """左亮右暗:手機在右邊擋到光。"""
    page = np.asarray(_text_like(800, 1100)).astype(np.float32)
    ramp = np.linspace(1.0, 1.0 - strength, page.shape[1])[None, :, None]
    return Image.fromarray(np.clip(page * ramp, 0, 255).astype(np.uint8))


def test_shadow_across_the_page_is_evened_out():
    shadowed = _shadowed()

    fixed = even_light(shadowed)

    assert fixed is not None and fixed.size == shadowed.size
    assert _paper_spread(fixed) < _paper_spread(shadowed) / 4


def test_evenly_lit_page_and_printed_dark_blocks_are_left_alone():
    # 深色標題列、灰底表格是印上去的,不是陰影:光照曲面是平的,不補
    page = _text_like(800, 1100)
    assert even_light(page) is None
    ImageDraw.Draw(page).rectangle([0, 0, 800, 200], fill=(30, 45, 110))
    ImageDraw.Draw(page).rectangle([60, 600, 740, 700], fill=(200, 200, 200))
    assert even_light(page) is None


def test_dark_photo_is_brightened():
    dark = Image.fromarray((np.asarray(_text_like(800, 1100)) * 0.42).astype(np.uint8))

    out = stretch_contrast(dark)

    assert out is not None
    ink, paper = np.percentile(np.asarray(out.convert("L")), [1, 90])
    assert paper > 230 and ink < 40


def test_faded_print_gets_more_contrast():
    # 熱感紙褪色:字只比紙暗一點
    page = Image.new("RGB", (800, 1100), (235, 235, 235))
    draw = ImageDraw.Draw(page)
    for y in range(80, 1000, 45):
        draw.rectangle([60, y, 700, y + 16], fill=(165, 165, 165))

    out = stretch_contrast(page)

    assert out is not None
    ink, paper = np.percentile(np.asarray(out.convert("L")), [1, 90])
    assert paper - ink > 150


def test_normal_or_blank_pages_keep_their_contrast():
    assert stretch_contrast(_text_like()) is None
    assert stretch_contrast(Image.new("RGB", (300, 300), "white")) is None


def test_each_step_can_be_turned_off(tmp_path):
    # 偏暗又有陰影的照片:三步都會做(補光只補到最亮那一塊的亮度,整張還是偏暗,所以也會拉對比);
    # 關掉哪一步,那一步就不見
    photo = np.asarray(_photo()).astype(np.float32)
    ramp = np.linspace(0.55, 0.25, photo.shape[1])[None, :, None]
    path = tmp_path / "dim.png"
    Image.fromarray(np.clip(photo * ramp, 0, 255).astype(np.uint8)).save(path)
    on = dict(find_document=True, even_light=True, contrast=True)

    full = prepare_image(path, deskew=False, options=PreprocessConfig(**on)).steps
    assert {STEP_DOCUMENT, STEP_LIGHT, STEP_CONTRAST} <= set(full)
    for name, step in (("find_document", STEP_DOCUMENT), ("even_light", STEP_LIGHT), ("contrast", STEP_CONTRAST)):
        steps = prepare_image(path, deskew=False, options=PreprocessConfig(**{**on, name: False})).steps
        assert step not in steps
    assert STEP_CONTRAST not in prepare_image(path, deskew=False).steps     # 拉對比預設關(11/2 實測)


def test_light_is_only_evened_out_on_a_straightened_paper(tmp_path):
    # 沒找到紙時,整張照片連桌面一起估光照會把紙補歪:只補拉正後的紙
    shadowed = tmp_path / "shadowed.png"
    _shadowed().save(shadowed)                      # 拍滿畫面的紙:找不到紙張,不補光

    assert STEP_LIGHT not in prepare_image(shadowed, deskew=False).steps
    assert even_light(_shadowed()) is not None      # 直接呼叫還是會補(核對的再試一次用它)


# ---- 細長單據、掃描 PDF -------------------------------------------------------------

def test_tall_receipt_keeps_more_pixels_with_the_area_cap(tmp_path):
    path = tmp_path / "receipt.png"
    _text_like(700, 2800).save(path)

    tall = _decode(prepare_image(path, deskew=False, options=PreprocessConfig(tall_pixels=True)).data)
    normal = _decode(prepare_image(path, deskew=False).data)

    assert tall.size == (700, 2800) and tall.width * tall.height <= MAX_PIXELS
    assert max(normal.size) == MAX_LONG_EDGE


def test_area_cap_still_shrinks_big_photos(tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (5000, 4000), "white").save(path)

    out = _decode(prepare_image(path, deskew=False, options=PreprocessConfig(tall_pixels=True)).data)

    assert out.width * out.height <= MAX_PIXELS * 1.01
    assert out.width / out.height == pytest.approx(1.25, rel=0.01)


def test_scanned_pdf_gives_its_image_at_native_resolution(tmp_path):
    path = tmp_path / "scan.pdf"
    _receipt_on_page().save(path, format="PDF", resolution=200.0)

    native = scanned_page_image(path)

    assert native is not None and native.size == (2480, 3508)


def _pdf_with_logo(path, logo: tuple[int, int] | None) -> None:
    """數位 PDF:一頁 A4,可以放一個小 logo(影像只佔頁面一小塊)。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(595, 842)
    if logo:
        image = pdfium.PdfImage.new(pdf)
        image.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("RGB", logo, (200, 30, 30))))
        image.set_matrix(pdfium.PdfMatrix().scale(*logo).translate(40, 700))
        page.insert_obj(image)
        page.gen_content()
    pdf.save(str(path))
    pdf.close()


def test_digital_pdfs_and_other_files_are_not_scans(tmp_path):
    blank, logo, broken, photo = (tmp_path / "blank.pdf", tmp_path / "logo.pdf",
                                  tmp_path / "broken.pdf", tmp_path / "photo.png")
    _pdf_with_logo(blank, None)
    _pdf_with_logo(logo, (120, 60))
    broken.write_bytes(b"%PDF-1.7 not really")
    _text_like().save(photo)

    for path in (blank, logo, broken, photo):
        assert scanned_page_image(path) is None, path.name


def test_scanned_pdf_is_read_at_native_resolution_when_turned_on(tmp_path):
    path = tmp_path / "scan.pdf"
    _receipt_on_page().save(path, format="PDF", resolution=200.0)

    on = prepare_image(path, deskew=False, options=PreprocessConfig(scan_native=True))
    off = prepare_image(path, deskew=False)

    assert on.steps[0] == STEP_SCAN and STEP_SCAN not in off.steps
    wide, tall = _ink_share(_decode(on.data))
    assert wide > 0.8 and tall > 0.8                 # 一樣裁到收據


# ---- 加密 PDF(PDF-PW):偵測、用密碼解開 --------------------------------------------

def test_encrypted_pdf_is_detected(tmp_path):
    from samples import encrypted_pdf, plain_pdf

    locked, plain, photo = tmp_path / "locked.pdf", tmp_path / "plain.pdf", tmp_path / "photo.png"
    locked.write_bytes(encrypted_pdf())
    plain.write_bytes(plain_pdf())
    Image.new("RGB", (8, 8), "white").save(photo)
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.3 not really a pdf")

    assert preprocess.pdf_needs_password(locked) is True
    assert preprocess.pdf_needs_password(plain) is False
    assert preprocess.pdf_needs_password(photo) is False                     # 不是 PDF
    assert preprocess.pdf_needs_password(tmp_path / "broken.pdf") is False   # 壞掉的交給後面當成讀不出來


def test_decrypt_pdf_writes_a_copy_that_opens_without_a_password(tmp_path):
    from samples import PDF_PASSWORD, encrypted_pdf

    locked, opened = tmp_path / "locked.pdf", tmp_path / "opened.pdf"
    locked.write_bytes(encrypted_pdf())

    assert preprocess.decrypt_pdf(locked, PDF_PASSWORD, opened) is True
    assert preprocess.pdf_needs_password(opened) is False
    assert _decode(load_image_bytes(opened)).size == (480, 640)   # 原本 120×160 的頁面,照常以 4 倍渲染
    assert preprocess.pdf_needs_password(locked) is True          # 原檔不動


@pytest.mark.parametrize("wrong", ["", "sample-123", "SAMPLE-1234", "密碼"])
def test_decrypt_pdf_with_wrong_password_writes_nothing(tmp_path, wrong):
    from samples import encrypted_pdf

    locked, opened = tmp_path / "locked.pdf", tmp_path / "opened.pdf"
    locked.write_bytes(encrypted_pdf())

    assert preprocess.decrypt_pdf(locked, wrong, opened) is False
    assert not opened.exists()


def test_encrypted_pdf_without_password_is_a_clear_error(tmp_path):
    """沒有經過輸入密碼那一頁的入口(資料夾監控、命令列):讀不出來,原因寫 PDF 有密碼,不帶完整路徑。"""
    from samples import encrypted_pdf

    locked = tmp_path / "locked.pdf"
    locked.write_bytes(encrypted_pdf())

    with pytest.raises(preprocess.EncryptedPdfError) as caught:
        prepare_image(locked)
    assert isinstance(caught.value, UnreadableImageError)
    assert "密碼" in str(caught.value) and str(tmp_path) not in str(caught.value)
