"""選好照片後的拍照檢查:只有偵測到模糊或缺角,才提示重拍(使用者 11/4 的上傳流程第 2 點)。

瀏覽器把照片縮到長邊 CHECK_EDGE 送到 POST /upload/check,這裡只在記憶體裡算:不存檔、不寫紀錄、不送模型
(藥袋是特種個資),回的只有固定的一句提示。提示不擋人,「開始辨識」照樣能按。
只用 OpenCV 的傳統影像處理,不用任何深度學習模型;找紙張沿用 src/preprocess.py,和拉正用的是同一套。

門檻只用真實文件量(11/5,公開政府公文:掃描 28 份、數位 PDF 37 份,都清楚完整):這批一份都不會跳提示。
手上還沒有真的模糊、缺角的照片,**抓得到多少沒量過**,所以判斷不了一律不提示,寧可漏報,不要誤報(原則 5、7)。
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from .preprocess import DOC_ANALYSIS_EDGE, DOC_ANGLES, find_document

CHECK_EDGE = 1600           # 瀏覽器把照片縮到這個長邊再送來(app.js 的 CHECK_EDGE 要一樣);更大的不是我們的頁面送的,不看
CHECK_MIN_EDGE = 400        # 長邊比這個小的圖,字本來就看不清楚,分不出是不是模糊,不判斷

# 模糊:看「最強的那批邊緣有幾個像素寬」。字和紙的交界在清楚的照片上只有兩三個像素寬,模糊時會散開;
# 邊緣寬度已經除掉對比,淡字、褪色的紙不會被當成模糊,也不受一頁字多字少影響。
# (拉普拉斯變異數不能用:同一批清楚的公文縮到長邊 1600 後,從 38 到 7,900 都有,門檻低到不誤報就什麼都抓不到)
# 清楚的公文最寬 2.6 像素(掃描 28 份、數位 37 份,換過縮圖方式與 JPEG 品質)。真實的手機照片會比掃描檔軟一點,
# 又還沒拿到,所以門檻抓這個值的兩倍多;字的筆畫這麼散開時,A4 上的小字已經糊在一起
BLUR_WIDTH = 6.0            # 超過這麼多像素才算模糊
BLUR_TOP_EDGES = 300        # 每個方向只看梯度最強的這麼多個峰值:雜訊和 JPEG 的漣漪梯度小,進不來
BLUR_SHARPEST = 25          # 取這批裡比較銳利的那一端(百分位):連這些都散開才算模糊
BLUR_MIN_EDGES = 100        # 這個方向的邊緣少於這個數就不算這個方向;兩個方向都不夠(空白頁)就不判斷
BLUR_MIN_CONTRAST = 25      # 局部明暗差不到這個的不算邊緣
BLUR_WINDOW = 21            # 局部明暗差的範圍(像素);要比想量到的邊緣寬度大
PAPER_INNER = 0.92          # 找到紙時只看紙的內部(四角往中心收),桌面和紙邊不算

# 缺角:紙有一部分跑出畫面。看得到 2~3 個紙角,紙有一長段被畫面邊界切掉,紙外又看得到明顯比較暗的背景才算。
# 紙貼滿畫面(掃描檔、數位檔、拍得很近)看不到紙外,分不出來,不提示;深色的標題列、掃描的黑邊沒有紙角,也不算
CUT_MIN_PAPER = 0.25        # 紙至少佔畫面這個比例;太小的多半是紙上的方框或別的東西
CUT_MAX_PAPER = 0.92        # 超過就是幾乎貼滿:露出來的那一點可能只是掃描的黑邊或標題列
CUT_MIN_SOLID = 0.92        # 紙的面積 ÷ 凸包面積:不是實心的一塊(手擋著、形狀零碎)就分不清紙在哪裡
CUT_MIN_CONTRAST = 40       # 紙要比紙外亮這麼多(找紙張的門檻 20 的兩倍)
CUT_MIN_CORNERS, CUT_MAX_CORNERS = 2, 3     # 畫面內看得到幾個紙角才算;四個都看得到就是沒缺,0、1 個分不清是不是紙
CUT_BORDER = 0.005          # 外框的頂點離畫面邊界不到長邊的這個比例,算碰到邊界
CUT_MIN_TOUCH = 0.10        # 紙貼著畫面邊界的長度至少是短邊的這個比例:只有紙角剛好碰到邊界、或只缺一個小尖角的不算

HINT_BLUR = "照片有點模糊，建議換一張再拍。"
HINT_CUT = "文件的邊角好像沒拍進去，建議換一張，整張拍進畫面。"
HINT_BOTH = "照片有點模糊，邊角也好像沒拍進去，建議換一張，整張拍清楚。"


@dataclass(frozen=True)
class PhotoCheck:
    """檢查結果。edge_width 是邊緣寬度(像素),None 是沒有足夠的邊緣可以判斷;cut_off 是有沒有缺角。"""

    edge_width: float | None
    cut_off: bool

    @property
    def blurry(self) -> bool:
        return self.edge_width is not None and self.edge_width > BLUR_WIDTH

    @property
    def hint(self) -> str | None:
        """要給長輩看的一句話;沒偵測到就是 None,畫面上什麼都不寫。"""
        if self.blurry and self.cut_off:
            return HINT_BOTH
        if self.blurry:
            return HINT_BLUR
        return HINT_CUT if self.cut_off else None


def photo_hint(data: bytes) -> str | None:
    """POST /upload/check 收到的位元組 → 提示。不是瀏覽器送的那種圖(JPEG、大小在範圍內)或任何出錯都回 None:
    檢查只是多一句提醒,不能擋上傳。出錯也不寫紀錄(照片不留任何痕跡)。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "JPEG" or not CHECK_MIN_EDGE <= max(img.size) <= CHECK_EDGE:
                return None
            return check_photo(img).hint
    except Exception:
        return None


def check_photo(img: Image.Image) -> PhotoCheck:
    """檢查一張縮小過的照片。找得到紙(find_document)時邊緣寬度只看紙的內部。
    缺角另外看:紙被畫面切掉一截時,看得到的那塊也常是四邊形,find_document 一樣找得到,不能拿它當「沒缺」。"""
    rgb = img.convert("RGB")
    return PhotoCheck(edge_width(rgb, find_document(rgb)), looks_cut_off(cut_off_evidence(rgb)))


def edge_width(img: Image.Image, quad=None) -> float | None:
    """最強的那批邊緣有幾個像素寬;邊緣太少回 None。quad 是紙的四角(只看紙的內部),None 是看整張。

    一個方向一個方向算,取比較寬的:手晃的時候只有和晃動垂直的筆畫會散開,另一個方向還是銳利的。
    寬度 = 局部明暗差 ÷ 那一點的梯度;一個像素的銳利交界,用這個算法約 2。
    """
    import cv2
    import numpy as np

    gray = np.asarray(img.convert("L"), dtype=np.float32)
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3) / 8     # 除以 8:變成每個像素的亮度差
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3) / 8
    strength = cv2.magnitude(grad_x, grad_y)
    window = np.ones((BLUR_WINDOW, BLUR_WINDOW), np.uint8)
    contrast = cv2.dilate(gray, window) - cv2.erode(gray, window)
    peaks = (strength == cv2.dilate(strength, np.ones((3, 3), np.uint8))) & (contrast >= BLUR_MIN_CONTRAST) & (strength > 0)
    if quad is not None:
        inner = np.zeros(gray.shape, np.uint8)
        center = quad.mean(axis=0)
        cv2.fillConvexPoly(inner, (center + (quad - center) * PAPER_INNER).astype(np.int32), 255)
        peaks &= inner > 0
    widths = []
    for chosen in (peaks & (np.abs(grad_x) >= np.abs(grad_y)), peaks & (np.abs(grad_x) < np.abs(grad_y))):
        strong, local = strength[chosen], contrast[chosen]
        if strong.size < BLUR_MIN_EDGES:
            continue
        if strong.size > BLUR_TOP_EDGES:
            top = np.argpartition(strong, -BLUR_TOP_EDGES)[-BLUR_TOP_EDGES:]
            strong, local = strong[top], local[top]
        widths.append(float(np.percentile(local / strong, BLUR_SHARPEST)))
    return max(widths) if widths else None


def cut_off_evidence(img: Image.Image) -> dict | None:
    """紙跑出畫面的跡象:最大的一塊亮區的面積比例、實心程度、亮度差,外框頂點有幾個在畫面內(看得到的紙角)、
    畫面內的頂點像不像紙角,以及紙貼著畫面邊界多長(短邊的比例);畫面裡沒有亮區就回 None。
    找亮區的作法同 preprocess._quad_from_brightness。"""
    import cv2
    import numpy as np

    rgb = np.asarray(img.convert("RGB"))
    scale = min(1.0, DOC_ANALYSIS_EDGE / max(rgb.shape[:2]))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    whiteness = cv2.GaussianBlur(cv2.subtract(hsv[:, :, 2], hsv[:, :, 1]), (7, 7), 0)
    _, mask = cv2.threshold(whiteness, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    size = max(3, min(mask.shape) // 40) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (size, size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(contour)
    hull = cv2.convexHull(contour)
    if area <= 0 or cv2.contourArea(hull) <= 0:
        return None

    height, width = mask.shape
    margin = CUT_BORDER * max(height, width)
    corners = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True).reshape(-1, 2).astype(np.float32)
    on_border = [x <= margin or y <= margin or x >= width - 1 - margin or y >= height - 1 - margin for x, y in corners]

    angles_ok = True
    for i, touching in enumerate(on_border):
        if touching:
            continue
        a, b, c = corners[i - 1], corners[i], corners[(i + 1) % len(corners)]
        u, v = a - b, c - b
        cos = float(np.dot(u, v) / max(np.linalg.norm(u) * np.linalg.norm(v), 1e-6))
        angles_ok = angles_ok and DOC_ANGLES[0] <= float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))) <= DOC_ANGLES[1]

    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    paper = np.zeros_like(gray)
    cv2.drawContours(paper, [contour], -1, 255, cv2.FILLED)
    outside = gray[paper == 0]
    edges = np.concatenate([paper[0], paper[-1], paper[1:-1, 0], paper[1:-1, -1]])
    return {
        "paper": area / mask.size,
        "solid": area / cv2.contourArea(hull),
        "contrast": float(np.median(gray[paper > 0])) - float(np.median(outside)) if outside.size else 0.0,
        "inside": on_border.count(False),
        "angles_ok": angles_ok,
        "touch": float(np.count_nonzero(edges)) / min(height, width),
    }


def looks_cut_off(evidence: dict | None) -> bool:
    """每一項都要符合才算缺角;差一項就是判斷不了,不提示。"""
    return (evidence is not None
            and CUT_MIN_PAPER <= evidence["paper"] <= CUT_MAX_PAPER
            and evidence["solid"] >= CUT_MIN_SOLID
            and evidence["contrast"] >= CUT_MIN_CONTRAST
            and CUT_MIN_CORNERS <= evidence["inside"] <= CUT_MAX_CORNERS
            and evidence["angles_ok"]
            and evidence["touch"] >= CUT_MIN_TOUCH)
