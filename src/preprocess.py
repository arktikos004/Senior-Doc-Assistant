"""影像前處理:把輸入檔轉成要送進視覺模型的影像位元組。

所有 provider 共用。轉正、歪斜校正等前處理也集中在這裡,
避免各 provider 各自處理、結果不一致。

只影響「送進模型」的影像;自我驗證(src/verify)讀的是原檔,不受這裡影響。
順序:EXIF 轉正 → 縮到長邊上限 → 輕度歪斜校正 → 重新編碼(同時去除 EXIF/GPS 等中繼資料)。
歪斜校正是「盡力而為」:偵測失敗就略過、不轉。但影像解不開、無法重新編碼,或 PDF 渲染失敗時
會丟出例外,由 Pipeline 記成 failed——絕不把原檔送出(原檔可能帶 EXIF/GPS 等中繼資料)。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# 長邊上限:手機照片常見 4000px 以上,原尺寸送模型只會變慢、雲端請求變大(Workers AI 有 413 上限),
# 2048px 對單據上的小字仍足夠清楚
MAX_LONG_EDGE = 2048
JPEG_QUALITY = 92  # 照片重新編碼的品質;太低會讓細小數字糊掉

# 歪斜校正:只在偵測到「明確」傾角時才轉,小角度視覺模型本身就讀得懂,轉了反而多一次重新取樣
DESKEW_MIN_ANGLE = 2.0      # 小於此角度不轉
DESKEW_MAX_ANGLE = 10.0     # 只搜尋 ±10°;更大的傾角多半是拍攝方向問題,不在這裡處理
_DESKEW_COARSE_STEP = 0.5
_DESKEW_FINE_STEP = 0.1
_DESKEW_ANALYSIS_EDGE = 800  # 偵測時先縮小,速度與精度的折衷
_DESKEW_MIN_GAIN = 1.25      # 最佳角度的分數要比不轉高出這個倍數,才算「明確」
_DESKEW_INK_RANGE = (0.005, 0.40)  # 墨跡比例不在此範圍(空白頁、大面積暗背景)就不判斷


@dataclass(frozen=True)
class PreparedImage:
    """送進模型的影像:位元組與 MIME 類型(雲端 API 需要 data URI)。"""

    data: bytes
    mime_type: str  # "image/png" 或 "image/jpeg"


class UnreadableImageError(ValueError):
    """影像解不開或無法重新編碼(檔案損毀、不是支援的影像格式)。"""


class EncryptedPdfError(UnreadableImageError):
    """PDF 有密碼、沒給密碼打不開。網頁上傳會先請家人輸入密碼(decrypt_pdf);其他入口照讀不出來處理。"""


MSG_PDF_LOCKED = "這份 PDF 有密碼,要先輸入密碼才能讀"


def prepare_image(file_path: Path, deskew: bool = True) -> PreparedImage:
    """讀取文件並做前處理;PDF 渲染第一頁為 PNG。

    一律重新編碼,順便去掉 EXIF(拍攝裝置、GPS 位置等),避免隨影像送出。
    非 PDF 的影像若 PIL 打不開或無法重新編碼,丟 UnreadableImageError,絕不原檔送出
    (原檔可能帶中繼資料);Pipeline 會把文件記成 failed,原檔留在 failed/。
    """
    if file_path.suffix.lower() == ".pdf":
        return _encode(_process(_render_pdf(file_path), deskew), "PNG")

    try:
        img = Image.open(file_path)
        source_format = img.format
        if source_format == "JPEG":
            # 讓 JPEG 解碼器直接以 1/2、1/4… 解碼,超大照片省下大量時間與記憶體
            img.draft("RGB", (MAX_LONG_EDGE, MAX_LONG_EDGE))
        img = ImageOps.exif_transpose(img)
        out_format = "JPEG" if source_format in ("JPEG", "MPO") else "PNG"
        return _encode(_process(img, deskew), out_format)
    except Image.DecompressionBombError:
        raise  # 惡意或異常巨大的影像:原樣往外丟,訊息本身已說明原因
    except Exception as exc:
        # 只寫檔名與例外種類,不接 PIL 的原文:原文帶完整路徑(這台電腦的資料夾位置),
        # 而這個訊息會進處理紀錄、資料庫與匯出;原始例外仍掛在 __cause__
        raise UnreadableImageError(
            f"無法讀取影像 {file_path.name}(檔案可能損毀或不是支援的影像格式;{type(exc).__name__})"
        ) from exc


def load_image_bytes(file_path: Path) -> bytes:
    """讀取影像檔並前處理;PDF 則渲染第一頁為 PNG。只需要位元組時使用。"""
    return prepare_image(file_path).data


def _is_password_error(exc: Exception) -> bool:
    import pypdfium2.raw as pdfium_c

    return getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD


def pdf_needs_password(file_path: Path) -> bool:
    """這份 PDF 要密碼才打得開嗎?不是 PDF、或壞掉打不開的都回 False(後面照讀不出來處理)。"""
    if file_path.suffix.lower() != ".pdf":
        return False
    import pypdfium2 as pdfium

    try:
        pdfium.PdfDocument(str(file_path)).close()
    except pdfium.PdfiumError as exc:
        return _is_password_error(exc)
    return False


def decrypt_pdf(file_path: Path, password: str, dest: Path) -> bool:
    """用 password 打開加密的 PDF,在 dest 存一份解除密碼的副本;密碼不對回 False、不寫 dest。

    密碼(常是身分證字號或生日)只在這裡用一次:不寫紀錄、不放進例外訊息、不存檔。file_path 不動。
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        pdf = pdfium.PdfDocument(str(file_path), password=password)
    except pdfium.PdfiumError as exc:
        if _is_password_error(exc):
            return False
        raise
    try:
        pdf.save(str(dest), flags=pdfium_c.FPDF_REMOVE_SECURITY)
    finally:
        pdf.close()
    return True


def _render_pdf(file_path: Path) -> Image.Image:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(file_path))
    except pdfium.PdfiumError as exc:
        if _is_password_error(exc):
            raise EncryptedPdfError(MSG_PDF_LOCKED) from None   # 不帶原始訊息與路徑
        raise
    try:
        page = pdf[0]
        bitmap = page.render(scale=2.0)  # 約 144 DPI,兼顧清晰度與大小
        return bitmap.to_pil()
    finally:
        pdf.close()


def _process(img: Image.Image, deskew: bool) -> Image.Image:
    """色彩模式統一 → 縮圖 → (可選)歪斜校正。"""
    img = _to_rgb(img)
    img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.LANCZOS)  # 只縮不放
    if deskew:
        img = deskew_image(img)
        img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.LANCZOS)  # 旋轉後外框變大,再收一次
    return img


def _to_rgb(img: Image.Image) -> Image.Image:
    """統一為 RGB 或 L;透明背景鋪白底(直接丟 alpha 會變黑底,黑字就看不見了)。"""
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _encode(img: Image.Image, fmt: str) -> PreparedImage:
    """重新編碼;不傳 exif 參數,輸出就不含任何 EXIF。"""
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return PreparedImage(buf.getvalue(), "image/jpeg")
    img.save(buf, format="PNG")
    return PreparedImage(buf.getvalue(), "image/png")


# --- 歪斜校正 ---


def deskew_image(img: Image.Image) -> Image.Image:
    """偵測到明確傾角(> DESKEW_MIN_ANGLE)才旋轉;否則或任何失敗都回傳原物件。"""
    try:
        angle = estimate_skew(img)
    except Exception as exc:  # 校正只是加分,不能讓它造成辨識失敗
        log.warning("歪斜偵測失敗,略過校正:%s", exc)
        return img
    if angle is None or abs(angle) <= DESKEW_MIN_ANGLE:
        return img
    fill = 255 if img.mode == "L" else (255, 255, 255)
    return img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=fill)


def estimate_skew(img: Image.Image) -> float | None:
    """以投影剖面法估計要「逆時針轉幾度」才能讓文字列水平。

    做法:二值化後在 ±DESKEW_MAX_ANGLE 內試轉,文字列對齊水平時,逐列墨跡量的
    起伏最劇烈(列與列間距分明)。最佳角度不比 0° 明顯好就回 0.0;
    影像看起來不像文件(幾乎空白或大面積暗色)回 None。
    """
    import numpy as np

    gray = img.convert("L")
    gray.thumbnail((_DESKEW_ANALYSIS_EDGE, _DESKEW_ANALYSIS_EDGE))
    pixels = np.asarray(gray, dtype=np.uint8)
    ink = pixels < _otsu_threshold(pixels)
    ratio = float(ink.mean())
    if not (_DESKEW_INK_RANGE[0] < ratio < _DESKEW_INK_RANGE[1]):
        return None
    ink_img = Image.fromarray(ink.astype(np.uint8) * 255)

    def score(angle: float) -> float:
        rotated = ink_img.rotate(angle, resample=Image.NEAREST, expand=True, fillcolor=0)
        profile = np.asarray(rotated, dtype=np.float64).sum(axis=1)
        return float(np.sum(np.diff(profile) ** 2))

    coarse = np.arange(-DESKEW_MAX_ANGLE, DESKEW_MAX_ANGLE + 1e-9, _DESKEW_COARSE_STEP)
    best = max(coarse, key=score)
    fine = np.arange(best - _DESKEW_COARSE_STEP, best + _DESKEW_COARSE_STEP + 1e-9, _DESKEW_FINE_STEP)
    best = max(fine, key=score)

    baseline = score(0.0)
    if baseline <= 0 or score(best) < baseline * _DESKEW_MIN_GAIN:
        return 0.0
    return round(float(best), 1)


def _otsu_threshold(pixels) -> int:
    """Otsu 自動門檻:讓「墨跡 / 背景」兩群的組間變異最大。"""
    import numpy as np

    hist = np.bincount(pixels.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    cum_mean = np.cumsum(hist * np.arange(256))
    mean_all = cum_mean[-1]
    valid = (weight_bg > 0) & (weight_fg > 0)
    between = np.zeros(256)
    mean_bg = cum_mean[valid] / weight_bg[valid]
    mean_fg = (mean_all - cum_mean[valid]) / weight_fg[valid]
    between[valid] = weight_bg[valid] * weight_fg[valid] * (mean_bg - mean_fg) ** 2
    return int(np.argmax(between)) + 1
