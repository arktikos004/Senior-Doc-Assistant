"""影像前處理:把輸入檔轉成要送進視覺模型的影像位元組。

所有 provider 共用。轉正、歪斜校正等前處理也集中在這裡,
避免各 provider 各自處理、結果不一致。

只影響「送進模型」的影像;自我驗證(src/verify)讀的是原檔,不受這裡影響(QR 與條碼解不開時,
會借這裡的找紙張、補光再試一次,同樣只在記憶體裡做)。
例外是 strip_metadata:網頁上傳收下檔案後立刻用它把「存檔的原件」去掉 EXIF/GPS(轉向先套到像素上)。
順序:EXIF 轉正 → 找到紙張並拉正(照片)→ 補光(光線不均才做)→ 裁掉白邊 → 拉對比(偏暗或褪色才做)
→ 縮到上限 → 輕度歪斜校正 → 重新編碼(同時去除 EXIF/GPS 等中繼資料)。每一步能不能做由 PreprocessConfig 決定。
這些步驟都是「盡力而為」:偵測失敗就略過、照原樣送。但影像解不開、無法重新編碼,或 PDF 渲染失敗時
會丟出例外,由 Pipeline 記成 failed——絕不把原檔送出(原檔可能帶 EXIF/GPS 等中繼資料)。
只用 OpenCV 與 Pillow 的傳統影像處理,不用任何深度學習模型。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .config import PreprocessConfig

log = logging.getLogger(__name__)

# 長邊上限:手機照片常見 4000px 以上,原尺寸送模型只會變慢、雲端請求變大(Workers AI 有 413 上限),
# 2048px 對單據上的小字仍足夠清楚
MAX_LONG_EDGE = 2048
# 細長單據(tall_pixels,只在本機模型):改用面積上限,長條收據的短邊不必跟著長邊縮成約 500px
MAX_PIXELS = MAX_LONG_EDGE * MAX_LONG_EDGE
MAX_TALL_EDGE = 4096        # 面積上限之外,長邊仍不超過這個值
# 要找紙張時 JPEG 先保留到這個長邊再解碼:紙只佔畫面一部分,先縮到 2048 再裁,紙張本身剩不到一半像素
DECODE_LONG_EDGE = 4096
JPEG_QUALITY = 92  # 照片重新編碼的品質;太低會讓細小數字糊掉

# PDF 轉圖:約 288 DPI。原本 2 倍(144 DPI)時,A4 一角的小張收據裁掉白邊後字太小,統編讀不出來(10/5 實測)
PDF_RENDER_SCALE = 4.0
PDF_MAX_EDGE = 3400         # 轉出來的長邊上限(A4 的 4 倍約 3,368);更大的頁面降低倍率,不佔掉幾百 MB 記憶體

# 裁白邊:只裁「白底、內容只佔一小塊」的影像(掃描的小張收據)。分不出哪裡是空白的(照片、深色背景)
# 和內容本來就佔滿的都不動;裁的只能是空白,所以淡色的字也算內容、四周留邊
CROP_MAX_CONTENT = 0.60     # 內容外框佔整張面積超過這個比例就不裁(省不了多少)
CROP_MIN_CONTENT = 0.01     # 小於這個比例多半是雜點或空白頁,不裁
CROP_MARGIN = 0.04          # 內容外框四周各留這個比例
_CROP_ANALYSIS_EDGE = 600   # 偵測時先縮小
_CROP_BACKGROUND = 200      # 四邊亮度的中位數低於這個值就不是白底
_CROP_INK_DELTA = 25        # 比背景暗這麼多才算內容(褪色的熱感紙、淡色的複寫聯也算)
_CROP_MIN_RUN = 3           # 縮小後一列(欄)至少有幾個內容像素才算有內容;掃描器上的灰塵不算

# 歪斜校正:只在偵測到「明確」傾角時才轉,小角度視覺模型本身就讀得懂,轉了反而多一次重新取樣
DESKEW_MIN_ANGLE = 2.0      # 小於此角度不轉
DESKEW_MAX_ANGLE = 10.0     # 只搜尋 ±10°;更大的傾角多半是拍攝方向問題,不在這裡處理
_DESKEW_COARSE_STEP = 0.5
_DESKEW_FINE_STEP = 0.1
_DESKEW_ANALYSIS_EDGE = 800  # 偵測時先縮小,速度與精度的折衷
_DESKEW_MIN_GAIN = 1.25      # 最佳角度的分數要比不轉高出這個倍數,才算「明確」
_DESKEW_INK_RANGE = (0.005, 0.40)  # 墨跡比例不在此範圍(空白頁、大面積暗背景)就不判斷

# 找紙張(照片):紙放在桌上、拿在手上,背景不是白的,斜拍時紙是梯形
DOC_ANALYSIS_EDGE = 1000    # 偵測時先縮到這個長邊
DOC_MIN_AREA = 0.10         # 紙至少佔畫面這個比例;更小的多半是紙上的方框、表格,不是整張紙
DOC_MAX_AREA = 0.95         # 超過就是本來就拍滿了:拉正省不了多少,誤裁的風險反而大
DOC_ANGLES = (50.0, 130.0)  # 四個內角的範圍;斜拍的梯形也在裡面
DOC_MIN_CONTRAST = 20       # 紙(亮度中位數)要比紙外一圈亮這麼多,才像「紙放在比較暗的背景上」
DOC_PAD = 0.015             # 四角往外放一點,貼著紙邊的字不會被切到

# 放大紙張:送進模型的 token 數跟著像素走(約 300 萬像素、1,100 個 token 封頂)。拉正只留下紙,像素比整張照片少,
# 1.6 百萬像素的照片從 715 個 token 變成 223–591 個(11/2 實測),紙上的字並沒有看得比較清楚
ENLARGE_MAX_SCALE = 3.0     # 最多放大這麼多倍;再大只是把 JPEG 的雜訊放大
ENLARGE_MIN_SCALE = 1.05    # 差不到這個比例就不放(多一次重新取樣不划算)

# 補光:陰影、燈光不均。估出來的光照曲面高低差超過門檻才做,均勻的掃描檔不動
LIGHT_ANALYSIS_EDGE = 200   # 光照是緩慢變化的,縮到很小再估
LIGHT_MIN_RATIO = 0.10      # 光照曲面第 5 百分位比第 95 百分位暗這個比例以上才補(暗的照片同樣的陰影,亮度差比較小)
LIGHT_MAX_GAIN = 2.5        # 最多調亮這麼多倍
LIGHT_OUTLIER = 10          # 比曲面暗這麼多的取樣點當成內容(字、標題列、灰底表格),不拿來估光照
LIGHT_ROUNDS = 4            # 去掉內容再重估的次數

# 拉對比:偏暗(室內光線不足)或褪色(熱感紙)才做
CONTRAST_DARK_PAPER = 150   # 紙面(亮度第 90 百分位)比這個暗就是偏暗
CONTRAST_FADED_RANGE = 110  # 字(第 1 百分位)和紙的亮度差小於這個就是褪色
CONTRAST_MIN_RANGE = 12     # 差不到這個:幾乎是空白頁,沒有東西可以拉
CLAHE_CLIP = 2.0

# 掃描的 PDF:第一頁最大的內嵌影像佔頁面這個比例以上,就當成掃描檔
SCAN_MIN_COVER = 0.90

# 步驟名稱(PreparedImage.steps;評測與實測工具只記這些名稱)
STEP_DOCUMENT = "找到紙張並拉正"
STEP_ENLARGE = "放大紙張"
STEP_LIGHT = "補光"
STEP_CROP = "裁白邊"
STEP_CONTRAST = "拉對比"
STEP_DESKEW = "歪斜校正"
STEP_SCAN = "掃描檔原解析度"


@dataclass(frozen=True)
class PreparedImage:
    """送進模型的影像:位元組與 MIME 類型(雲端 API 需要 data URI),以及實際做了哪些處理。"""

    data: bytes
    mime_type: str  # "image/png" 或 "image/jpeg"
    steps: tuple[str, ...] = ()   # 依序做了的處理(STEP_* 名稱);沒偵測到就沒做的不列


class UnreadableImageError(ValueError):
    """影像解不開或無法重新編碼(檔案損毀、不是支援的影像格式)。"""


class EncryptedPdfError(UnreadableImageError):
    """PDF 有密碼、沒給密碼打不開。網頁上傳會先請家人輸入密碼(decrypt_pdf);其他入口照讀不出來處理。"""


MSG_PDF_LOCKED = "這份 PDF 有密碼,要先輸入密碼才能讀"

# 存檔原件去中繼資料:照片格式 → Pillow 的存檔格式;PDF 不在這裡處理(沒有拍照位置)
_STORED_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".tif": "TIFF", ".tiff": "TIFF"}
_META_KEYS = ("exif", "xmp", "XML:com.adobe.xmp", "comment", "Comment")   # img.info 裡會洩漏資訊的鍵


def strip_metadata(file_path: Path) -> bool:
    """存檔的原件去掉 EXIF(拍攝裝置、時間、GPS 位置)、XMP 與文字註記,回傳有沒有改寫。

    轉向先套到像素上(看起來還是正的,後面的辨識也不必再靠轉向標記)。JPEG 以品質 95、不降色度取樣重存,
    QR 與條碼照樣讀得到;PNG 無損。PDF 與沒有中繼資料的影像原樣不動;讀不開的檔也不動,
    留給 Pipeline 記成讀不出來。先寫暫存檔再換掉原檔,失敗時原檔不變。
    """
    fmt = _STORED_FORMATS.get(file_path.suffix.lower())
    if fmt is None:
        return False
    tmp = file_path.with_name(file_path.name + ".tmp")
    try:
        with Image.open(file_path) as img:
            if not (img.getexif() or getattr(img, "text", None) or any(k in img.info for k in _META_KEYS)):
                return False
            icc = img.info.get("icc_profile")      # 色彩設定檔不含個資,留著顏色才不會跑掉
            upright = ImageOps.exif_transpose(img)
            options = {"exif": b""}
            if icc:
                options["icc_profile"] = icc
            if fmt == "JPEG":
                options.update(quality=95, subsampling=0)
                upright = _to_rgb(upright)
            elif fmt == "WEBP":
                options.update(quality=95)
            upright.save(tmp, format=fmt, **options)
        tmp.replace(file_path)
        return True
    except (OSError, ValueError, SyntaxError) as exc:
        tmp.unlink(missing_ok=True)
        log.warning("原件去中繼資料失敗,保留原檔(%s)", type(exc).__name__)
        return False


def prepare_image(file_path: Path, deskew: bool = True, crop: bool = True,
                  options: PreprocessConfig | None = None) -> PreparedImage:
    """讀取文件並做前處理;PDF 渲染第一頁為 PNG。

    options 決定各步驟開不開(沒給用 PreprocessConfig 的預設);crop=False 時連找紙張也不做(兩者都是裁切)。
    一律重新編碼,順便去掉 EXIF(拍攝裝置、GPS 位置等),避免隨影像送出。
    非 PDF 的影像若 PIL 打不開或無法重新編碼,丟 UnreadableImageError,絕不原檔送出
    (原檔可能帶中繼資料);Pipeline 會把文件記成 failed,原檔留在 failed/。
    """
    opts = options or PreprocessConfig()
    if file_path.suffix.lower() == ".pdf":
        scan = scanned_page_image(file_path) if opts.scan_native else None
        page = scan if scan is not None else _render_pdf(file_path)
        img, steps = _process(page, deskew, crop, opts)
        return _encode(img, "PNG", ((STEP_SCAN,) if scan is not None else ()) + steps)

    try:
        img = Image.open(file_path)
        source_format = img.format
        if source_format == "JPEG":
            # 讓 JPEG 解碼器直接以 1/2、1/4… 解碼,超大照片省下大量時間與記憶體;
            # 要找紙張時多留一倍,裁到紙張後才不會只剩一半像素
            edge = DECODE_LONG_EDGE if crop and opts.find_document else MAX_LONG_EDGE
            img.draft("RGB", (edge, edge))
        img = ImageOps.exif_transpose(img)
        out_format = "JPEG" if source_format in ("JPEG", "MPO") else "PNG"
        processed, steps = _process(img, deskew, crop, opts)
        return _encode(processed, out_format, steps)
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
        scale = min(PDF_RENDER_SCALE, PDF_MAX_EDGE / max(page.get_size()))
        return page.render(scale=scale).to_pil()
    finally:
        pdf.close()


def scanned_page_image(file_path: Path) -> Image.Image | None:
    """掃描的 PDF(第一頁最大的內嵌影像佔頁面 ≥ SCAN_MIN_COVER)回傳那張影像的原解析度;否則回 None。

    重新轉圖會多縮放一次:200 DPI 的掃描檔放大成 288 DPI 後,QR 反而解不開,用原解析度就解得開(10/30 實測)。
    影像的擺放方向(PDF 的矩陣)與頁面旋轉都會套上,看起來和整頁轉圖一樣是正的。
    不是 PDF、數位 PDF(文字是向量)、有密碼或讀不到都回 None,由呼叫端照舊整頁轉圖。
    """
    if file_path.suffix.lower() != ".pdf":
        return None
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        pdf = pdfium.PdfDocument(str(file_path))
    except pdfium.PdfiumError:
        return None
    try:
        page = pdf[0]
        width, height = page.get_size()
        best, cover = None, 0.0
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
            left, bottom, right, top = obj.get_bounds()
            share = (right - left) * (top - bottom) / (width * height)
            if share > cover:
                best, cover = obj, share
        if best is None or cover < SCAN_MIN_COVER:
            return None
        img = best.get_bitmap(render=True).to_pil()     # render:套上擺放方向與遮罩,大小仍是原解析度
        rotation = page.get_rotation()                  # 頁面本身轉了幾度(順時針)
        return img.rotate(-rotation, expand=True) if rotation else img
    except Exception as exc:   # 原解析度只是加分;讀不到就整頁轉圖
        log.info("讀不到掃描檔的內嵌影像,改整頁轉圖:%s", type(exc).__name__)
        return None
    finally:
        pdf.close()


def _process(img: Image.Image, deskew: bool, crop: bool = True,
             opts: PreprocessConfig | None = None) -> tuple[Image.Image, tuple[str, ...]]:
    """色彩模式統一 → 找紙張並拉正 → 補光 → 裁掉白邊 → 拉對比 → 縮圖 → 歪斜校正;回傳 (影像, 做了的步驟)。

    先裁再縮:紙只佔畫面一部分(照片、A4 一角的小張收據)時,裁完才留得住像素。
    """
    opts = opts or PreprocessConfig()
    steps: list[str] = []
    img = _to_rgb(img)
    if crop and opts.find_document:
        img = _best_effort(steps, STEP_DOCUMENT, _find_and_warp, img)
    if opts.even_light and STEP_DOCUMENT in steps:
        # 只補拉正後的紙:沒找到紙時整張照片連桌面一起估光照,6 張真實照片有 5 次讀得更差、沒有一次更好(11/2 實測)
        img = _best_effort(steps, STEP_LIGHT, even_light, img)
    if crop:
        cropped = crop_to_content(img)
        if cropped is not img:
            steps.append(STEP_CROP)
        img = cropped
    if opts.contrast:
        img = _best_effort(steps, STEP_CONTRAST, stretch_contrast, img)
    img = _shrink(img, opts.tall_pixels)  # 只縮不放
    if opts.enlarge_paper and STEP_DOCUMENT in steps:   # 只放大拉正後的紙;掃描檔、數位檔不動
        img = _best_effort(steps, STEP_ENLARGE, lambda paper: enlarge_paper(paper, opts.tall_pixels), img)
    if deskew:
        straight = deskew_image(img)
        if straight is not img:
            steps.append(STEP_DESKEW)
        img = _shrink(straight, opts.tall_pixels)  # 旋轉後外框變大,再收一次
    return img, tuple(steps)


def _best_effort(steps: list[str], name: str, step, img: Image.Image) -> Image.Image:
    """跑一個「沒偵測到就回 None」的步驟:有做就記下名稱;回 None 或出錯都照原樣往下,不能讓辨識失敗。"""
    try:
        out = step(img)
    except Exception as exc:
        log.warning("%s失敗,略過:%s", name, type(exc).__name__)
        return img
    if out is None:
        return img
    steps.append(name)
    return out


def _shrink(img: Image.Image, tall_pixels: bool) -> Image.Image:
    """縮到上限(只縮不放)。一般是長邊 MAX_LONG_EDGE;tall_pixels 時改用面積上限,長邊最多 MAX_TALL_EDGE。"""
    if not tall_pixels:
        img.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.LANCZOS)
        return img
    scale = min(1.0, (MAX_PIXELS / (img.width * img.height)) ** 0.5, MAX_TALL_EDGE / max(img.size))
    if scale < 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    return img


def enlarge_paper(img: Image.Image, tall_pixels: bool = False) -> Image.Image | None:
    """拉正後的紙比上限小時放大到上限(同 _shrink 的上限),最多 ENLARGE_MAX_SCALE 倍;已經夠大回 None(不動)。"""
    scale = MAX_LONG_EDGE / max(img.size)
    if tall_pixels:
        scale = min((MAX_PIXELS / (img.width * img.height)) ** 0.5, MAX_TALL_EDGE / max(img.size))
    scale = min(scale, ENLARGE_MAX_SCALE)
    if scale < ENLARGE_MIN_SCALE:
        return None
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)


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


def _encode(img: Image.Image, fmt: str, steps: tuple[str, ...] = ()) -> PreparedImage:
    """重新編碼;不傳 exif 參數,輸出就不含任何 EXIF。"""
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return PreparedImage(buf.getvalue(), "image/jpeg", steps)
    img.save(buf, format="PNG")
    return PreparedImage(buf.getvalue(), "image/png", steps)


# --- 裁掉白邊 ---


def crop_to_content(img: Image.Image) -> Image.Image:
    """白底、內容只佔一小塊時裁到內容外框(四周留邊);不該裁或任何失敗都回傳原物件。"""
    try:
        box = content_box(img)
    except Exception as exc:  # 裁切只是加分,不能讓它造成辨識失敗
        log.warning("裁白邊失敗,略過:%s", type(exc).__name__)
        return img
    return img.crop(box) if box else img


def content_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """內容外框(原圖座標 left, top, right, bottom,已含留邊);不該裁就回 None。

    四邊夠亮才當成白底;比背景明顯暗的像素算內容,每列(欄)要有連續幾個才算,免得被灰塵雜點撐大。
    外框佔整張的比例太大(省不了多少)或太小(多半是雜點、空白頁)都不裁。
    """
    import math

    import numpy as np

    gray = img.convert("L")
    gray.thumbnail((_CROP_ANALYSIS_EDGE, _CROP_ANALYSIS_EDGE), Image.BOX)
    pixels = np.asarray(gray, dtype=np.int16)
    border = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
    background = float(np.median(border))
    if background < _CROP_BACKGROUND:
        return None
    ink = pixels < background - _CROP_INK_DELTA
    rows = np.flatnonzero(ink.sum(axis=1) >= _CROP_MIN_RUN)
    cols = np.flatnonzero(ink.sum(axis=0) >= _CROP_MIN_RUN)
    if rows.size == 0 or cols.size == 0:
        return None
    top, bottom, left, right = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    height, width = pixels.shape
    share = (bottom - top) * (right - left) / (height * width)
    if not CROP_MIN_CONTENT <= share <= CROP_MAX_CONTENT:
        return None
    # 留邊再多放一個縮小後的像素:外框邊緣那一列可能只有一兩個內容像素,沒被算進來
    pad_x = (right - left) * CROP_MARGIN + 1
    pad_y = (bottom - top) * CROP_MARGIN + 1
    scale_x, scale_y = img.width / width, img.height / height
    return (
        max(0, math.floor((left - pad_x) * scale_x)),
        max(0, math.floor((top - pad_y) * scale_y)),
        min(img.width, math.ceil((right + pad_x) * scale_x)),
        min(img.height, math.ceil((bottom + pad_y) * scale_y)),
    )


# --- 找到紙張並拉正(照片) ---


def _find_and_warp(img: Image.Image) -> Image.Image | None:
    quad = find_document(img)
    return warp_document(img, quad) if quad is not None else None


def find_document(img: Image.Image):
    """照片裡那張紙的四角(原圖座標,依序左上、右上、右下、左下);找不到或不像一張紙回 None。

    先找邊緣圍成的四邊形;邊緣不明顯時,改用「紙比背景亮、顏色淡」找最大的一塊。
    驗收:凸四邊形、佔畫面 DOC_MIN_AREA~DOC_MAX_AREA、內角在 DOC_ANGLES 內、紙比紙外一圈亮 DOC_MIN_CONTRAST。
    掃描檔與拍滿的文件找不到(紙外沒有比較暗的背景),交給裁白邊。
    """
    import cv2
    import numpy as np

    rgb = np.asarray(img.convert("RGB"))
    scale = min(1.0, DOC_ANALYSIS_EDGE / max(rgb.shape[:2]))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    for find in (_quad_from_edges, _quad_from_brightness):
        quad = find(gray if find is _quad_from_edges else small)
        if quad is not None and _looks_like_paper(quad, gray):
            return _pad_quad(quad, gray.shape) / scale
    return None


def _quad_from_edges(gray):
    """Canny 邊緣(門檻依亮度中位數)→ 閉運算接上斷掉的紙邊 → 最大的外輪廓取四邊形。"""
    import cv2
    import numpy as np

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(blurred))
    edges = cv2.Canny(blurred, int(max(0.0, 0.66 * median)), int(min(255.0, 1.33 * median)))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
                             iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(contour) < DOC_MIN_AREA * gray.size:
            break
        quad = _four_corners(cv2.convexHull(contour))
        if quad is not None:
            return quad
    return None


def _quad_from_brightness(rgb):
    """紙比背景亮、顏色淡:亮度減飽和度後用 Otsu 分兩群,取最大的一塊;不是四邊形就用最小外接矩形(圓角、捲起來的紙)。"""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    whiteness = cv2.GaussianBlur(cv2.subtract(hsv[:, :, 2], hsv[:, :, 1]), (7, 7), 0)
    _, mask = cv2.threshold(whiteness, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    size = max(3, min(mask.shape) // 40) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (size, size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)    # 補上字造成的洞
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)     # 去掉背景上零星的亮點
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < DOC_MIN_AREA * mask.size:
        return None
    hull = cv2.convexHull(contour)
    quad = _four_corners(hull)
    if quad is None:
        quad = _order_corners(cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32))
    height, width = mask.shape
    return np.clip(quad, 0, [width - 1, height - 1]).astype(np.float32)


def _four_corners(hull):
    """凸包簡化成多邊形;剛好四個角才回傳(依序左上、右上、右下、左下),否則 None。"""
    import cv2
    import numpy as np

    approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
    return _order_corners(approx.reshape(4, 2).astype(np.float32)) if len(approx) == 4 else None


def _order_corners(points):
    """四個點排成左上、右上、右下、左下(x+y 最小是左上、最大是右下;y−x 最小是右上、最大是左下)。"""
    import numpy as np

    total, diff = points.sum(axis=1), points[:, 1] - points[:, 0]
    return np.array([points[np.argmin(total)], points[np.argmin(diff)],
                     points[np.argmax(total)], points[np.argmax(diff)]], dtype=np.float32)


def _looks_like_paper(quad, gray) -> bool:
    """四邊形像不像「一張放在比較暗的背景上的紙」:面積、凸、內角、紙比紙外一圈亮。"""
    import cv2
    import numpy as np

    height, width = gray.shape
    if not DOC_MIN_AREA <= cv2.contourArea(quad) / (height * width) <= DOC_MAX_AREA:
        return False
    if not cv2.isContourConvex(quad.reshape(-1, 1, 2)):
        return False
    for i in range(4):
        a, b, c = quad[i - 1], quad[i], quad[(i + 1) % 4]
        u, v = a - b, c - b
        cos = float(np.dot(u, v) / max(np.linalg.norm(u) * np.linalg.norm(v), 1e-6))
        if not DOC_ANGLES[0] <= np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))) <= DOC_ANGLES[1]:
            return False
    inside = np.zeros_like(gray)
    cv2.fillConvexPoly(inside, quad.astype(np.int32), 255)
    ring_size = max(3, int(0.04 * max(height, width))) | 1
    ring = cv2.dilate(inside, cv2.getStructuringElement(cv2.MORPH_RECT, (ring_size, ring_size))) & ~inside
    if np.count_nonzero(ring) < 0.01 * gray.size:      # 紙貼滿畫面,看不到紙外
        return False
    return float(np.median(gray[inside > 0])) - float(np.median(gray[ring > 0])) >= DOC_MIN_CONTRAST


def _pad_quad(quad, shape):
    """四角從中心往外放 DOC_PAD,再限制在畫面內。"""
    import numpy as np

    height, width = shape[:2]
    center = quad.mean(axis=0)
    padded = center + (quad - center) * (1 + DOC_PAD)
    return np.clip(padded, 0, [width - 1, height - 1]).astype(np.float32)


def warp_document(img: Image.Image, quad) -> Image.Image:
    """把四角拉成長方形(透視校正);輸出的長寬取對邊的較長者,不會比原圖的紙張更大。"""
    import cv2
    import numpy as np

    top_left, top_right, bottom_right, bottom_left = quad
    width = int(round(max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))))
    height = int(round(max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))))
    target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), target)
    warped = cv2.warpPerspective(np.asarray(img.convert("RGB")), matrix, (width, height),
                                 flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(warped)


# --- 補光(陰影、燈光不均) ---


def even_light(img: Image.Image) -> Image.Image | None:
    """紙面亮度不均(手或手機的陰影、燈光)時,把紙面補成一樣亮;均勻的影像回 None(不動)。

    光照是緩慢變化的:縮到很小、膨脹去掉字以後,用二次曲面配出紙面亮度;比曲面暗一截的取樣點
    (字、深色標題列、灰底表格)當成內容去掉再重配,所以印刷的深色區塊不會被當成陰影洗白。
    曲面最暗處比最亮處暗 LIGHT_MIN_RATIO 以上才補:補到最亮那一塊的亮度(整張偏暗交給拉對比);
    三個色版乘上同一個倍數(顏色不變),只調亮不調暗,最多 LIGHT_MAX_GAIN 倍。
    """
    import cv2
    import numpy as np

    rgb = np.asarray(img.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(1.0, LIGHT_ANALYSIS_EDGE / max(height, width))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    paper = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))).astype(np.float64)
    rows, cols = np.mgrid[0:paper.shape[0], 0:paper.shape[1]]
    y, x = rows.ravel() / max(paper.shape[0] - 1, 1), cols.ravel() / max(paper.shape[1] - 1, 1)
    terms = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)
    values = paper.ravel()
    keep = values >= np.median(values) - 4 * LIGHT_OUTLIER       # 先去掉明顯是內容的暗點
    coef = None
    for _ in range(LIGHT_ROUNDS):
        if keep.sum() < terms.shape[1] * 10:
            return None
        coef, *_ = np.linalg.lstsq(terms[keep], values[keep], rcond=None)
        keep = values >= terms @ coef - LIGHT_OUTLIER
    surface = (terms @ coef).reshape(paper.shape)
    low, high = np.percentile(surface, [5, 95])
    if high <= 0 or (high - low) / high < LIGHT_MIN_RATIO:
        return None
    gain = np.clip(high / np.maximum(surface, 1.0), 1.0, LIGHT_MAX_GAIN).astype(np.float32)
    gain = cv2.resize(gain, (width, height), interpolation=cv2.INTER_LINEAR)[:, :, None]
    return Image.fromarray(np.clip(rgb.astype(np.float32) * gain + 0.5, 0, 255).astype(np.uint8))


# --- 拉對比(偏暗、褪色) ---


def stretch_contrast(img: Image.Image) -> Image.Image | None:
    """偏暗(紙面不夠亮)或褪色(字和紙的亮度差太小)時拉開亮度差;正常或空白的影像回 None(不動)。

    在 LAB 的亮度上做:先把最暗與最亮的 0.5% 拉到兩端,再用 CLAHE 補局部的褪色;顏色大致保留。
    """
    import cv2
    import numpy as np

    lab = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2LAB)
    lightness = lab[:, :, 0]
    ink, paper = np.percentile(lightness, [1, 90])
    if paper - ink < CONTRAST_MIN_RANGE:
        return None
    if paper >= CONTRAST_DARK_PAPER and paper - ink >= CONTRAST_FADED_RANGE:
        return None
    low, high = np.percentile(lightness, [0.5, 99.5])
    table = np.clip((np.arange(256) - low) * 255.0 / max(high - low, 1.0), 0, 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(cv2.LUT(lightness, table))
    return Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


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
