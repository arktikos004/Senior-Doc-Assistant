"""影像前處理測試:EXIF 轉正、縮圖、PDF 轉 PNG、中繼資料移除、歪斜校正。

所有影像都在測試中用 PIL 合成,不讀任何真實文件。
"""
import io

import pytest
from PIL import Image, ImageDraw

from src import preprocess
from src.preprocess import (
    MAX_LONG_EDGE,
    UnreadableImageError,
    deskew_image,
    estimate_skew,
    load_image_bytes,
    prepare_image,
)

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

    out = _decode(prepare_image(path, deskew=False).data)

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
    assert _decode(load_image_bytes(opened)).size == (240, 320)   # 原本 120×160 的頁面,照常以 2 倍渲染
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
