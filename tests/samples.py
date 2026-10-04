"""網頁測試共用的合成樣本(AI 讀值的形狀同 src/models.py)。

商家、機關都是虛構的「示範」;期限一律在 2099 年,測試不會因為日子過了而突然失敗。
各測試要改某個欄位時用 dict(BILL, ...) 複製一份,不要直接改這裡的物件。
加密 PDF 用 encrypted_pdf() 當場產生(只用標準函式庫與 Pillow),不提交二進位檔、不新增相依。
"""
import hashlib
import io
import struct

from PIL import Image

BILL = {
    "doc_type": "帳單", "date": "2026-09-20", "vendor": "示範電力公司", "amount": 1854.0, "currency": "NTD",
    "plain_summary": "這是電費帳單,要在10月15日前繳1854元。",
    "fields": {"due_date": "2099-10-15", "bill_kind": "電費"},
    "verification": {}, "verified_confidence": None, "unreadable": [],
}

LETTER = {
    "doc_type": "公文", "date": "2026-09-25", "vendor": "示範區公所", "plain_summary": "",
    "fields": {"subject": "請補繳文件", "doc_number": "示範字第1150000001號",
               "deadline_text": "收到本函後15日內", "deadline": "2099-10-10",
               "required_actions": ["補繳身分證影本", "親自簽名"]},
}

INVOICE = {"doc_type": "發票", "date": "2026-09-18", "vendor": "示範超市", "amount": 320.0, "currency": "NTD"}

# 帳單自動列入的期限提醒(行動 payload,W1-C 的形狀)
REMINDER = {"title": "繳電費", "date": "2099-10-15", "description": "繳費期限:2099-10-15"}


# ---- 加密 PDF(PDF 1.3 Standard Security Handler:RC4 40 位元,R2;規格 Algorithm 3.1–3.4) ----

PDF_PASSWORD = "sample-1234"   # 測試用的密碼(虛構;真的電子帳單常用身分證字號或生日)
_PDF_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_PDF_PERMISSIONS = -44         # 權限位元;不影響能不能用密碼打開


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _pdf(objects: dict[int, bytes | tuple[bytes, str]], trailer: str, key=None) -> bytes:
    """把物件排成一份 PDF;tuple 是串流(內容, 字典其餘的項目),有 key 就用各物件的金鑰加密。"""
    out = bytearray(b"%PDF-1.3\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        body = objects[num]
        out += f"{num} 0 obj\n".encode()
        if isinstance(body, tuple):
            data, extra = body
            if key:
                data = _rc4(key(num), data)
            out += f"<< {extra} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"
        else:
            out += body
        out += b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _page_objects(jpeg: bytes, width: int, height: int) -> dict[int, bytes | tuple[bytes, str]]:
    return {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>").encode(),
        4: (f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode(), ""),
        5: (jpeg, f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
                  f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode"),
    }


def _sample_jpeg(size: tuple[int, int] = (120, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


def plain_pdf(size: tuple[int, int] = (120, 160)) -> bytes:
    """一頁、沒有密碼的 PDF(內容是一張白色影像)。"""
    return _pdf(_page_objects(_sample_jpeg(size), *size), "")


def encrypted_pdf(password: str = PDF_PASSWORD, size: tuple[int, int] = (120, 160)) -> bytes:
    """一頁、要 password 才打得開的 PDF(內容同 plain_pdf)。"""
    jpeg = _sample_jpeg(size)
    padded = (password.encode("latin-1") + _PDF_PAD)[:32]
    doc_id = hashlib.md5(jpeg + padded).digest()
    o_value = _rc4(hashlib.md5((b"owner" + _PDF_PAD)[:32]).digest()[:5], padded)             # Algorithm 3.3
    file_key = hashlib.md5(padded + o_value + struct.pack("<i", _PDF_PERMISSIONS) + doc_id).digest()[:5]  # 3.2
    u_value = _rc4(file_key, _PDF_PAD)                                                          # 3.4

    def object_key(num: int) -> bytes:                                                          # 3.1
        return hashlib.md5(file_key + struct.pack("<i", num)[:3] + b"\x00\x00").digest()[:10]

    objects = _page_objects(jpeg, *size)
    objects[6] = (f"<< /Filter /Standard /V 1 /R 2 /O <{o_value.hex()}> /U <{u_value.hex()}> "
                  f"/P {_PDF_PERMISSIONS} >>").encode()
    return _pdf(objects, f"/Encrypt 6 0 R /ID [<{doc_id.hex()}> <{doc_id.hex()}>]", key=object_key)
