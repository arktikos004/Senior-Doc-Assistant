"""另開一份「看有」(例如現場展示):自己的資料夾、自己的資料庫,用真的模型,和家裡正式使用的那份完全分開。

用法(專案根目錄,在 GPU 電腦上):
    python tools/run_instance.py --root C:\\Users\\<你>\\kanyou-demo --port 8100 \\
        --access-aud <展示用 Access 應用的 AUD> --member demo-family@example.com:family:家人

- 照片、讀值、資料庫、處理紀錄全部放在 --root 底下(inbox、archive、review、failed、uploads、logs);
  --root 必須在專案資料夾外面,不碰 data/、logs/ 裡的正式資料。
- 只綁 127.0.0.1;外面經 Cloudflare Tunnel 的展示網址進來,登入由另一個 Access 應用負責(--access-aud)。
- 第一次使用會在 --root 放一個標記檔;不是空的、又沒有標記檔的資料夾一律不用(避免指錯資料夾)。
- --reset 只清掉這份自己的子資料夾(開始彩排或正式展示前清空)。
- --member 可以給好幾次(email:角色[:稱呼],角色是 family 或 elder),啟動時加進這份的家庭成員名單。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BASE_DIR, AppConfig, PathsConfig, load_config  # noqa: E402
from src.models import ROLES  # noqa: E402

MARKER = ".kanyou-instance"            # 這個資料夾是 run_instance 建的
SUBFOLDERS = ("inbox", "archive", "review", "failed", "uploads", "logs")


class InstanceError(ValueError):
    """--root、--member 不能用;訊息直接印給操作的人看。"""


def check_root(root: Path) -> Path:
    """--root 要在專案資料夾外面,而且是空的、或是之前由這支程式建的(有標記檔)。回傳絕對路徑。"""
    root = root.expanduser().resolve()
    if root == BASE_DIR or root.is_relative_to(BASE_DIR):
        raise InstanceError(f"{root} 在專案資料夾裡面;請用專案外面的資料夾,才不會和正式資料或 git 混在一起。")
    if root.exists() and not root.is_dir():
        raise InstanceError(f"{root} 不是資料夾。")
    if root.is_dir() and any(root.iterdir()) and not (root / MARKER).is_file():
        raise InstanceError(f"{root} 裡已經有別的東西,而且不是這支程式建的;請換一個空的資料夾。")
    return root


def instance_config(base: AppConfig, root: Path) -> AppConfig:
    """設定照 config.yaml(模型、門檻…),只有資料夾全部換到 root 底下(資料庫跟著 logs 走)。"""
    paths = PathsConfig(inbox=root / "inbox", archive=root / "archive", review=root / "review",
                        failed=root / "failed", logs=root / "logs", uploads=root / "uploads", db=root / "logs" / "app.db")
    return replace(base, paths=paths)


def prepare(root: Path, *, reset: bool) -> None:
    """建資料夾與標記檔;reset 只刪這份自己的子資料夾,root 本身與其他檔案不動。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("看有的獨立實例資料夾(tools/run_instance.py)\n", encoding="utf-8")
    if reset:
        for name in SUBFOLDERS:
            shutil.rmtree(root / name, ignore_errors=True)


def parse_member(text: str) -> tuple[str, str, str | None]:
    """email:角色[:稱呼] → (email, 角色, 稱呼)。"""
    email, _, rest = text.partition(":")
    role, _, name = rest.partition(":")
    if "@" not in email or role not in ROLES:
        raise InstanceError(f"--member 要寫成 email:角色[:稱呼],角色是 {' 或 '.join(ROLES)}:{text!r}")
    return email.strip(), role, name.strip() or None


def main(argv: list[str] | None = None, serve=None) -> int:
    parser = argparse.ArgumentParser(description="看有:另開一份獨立的實例(展示用)")
    parser.add_argument("--root", type=Path, required=True, help="這份的資料夾(專案外面)")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--access-aud", default=None, help="這份用的 Cloudflare Access 應用 AUD(沒給就用環境變數)")
    parser.add_argument("--member", action="append", default=[], help="email:角色[:稱呼],可以給好幾次")
    parser.add_argument("--reset", action="store_true", help="啟動前清空這份的資料")
    args = parser.parse_args(argv)
    try:
        root = check_root(args.root)
        members = [parse_member(m) for m in args.member]
    except InstanceError as exc:
        print(exc)
        return 2
    prepare(root, reset=args.reset)
    if args.access_aud:
        os.environ["KANYOU_ACCESS_AUD"] = args.access_aud   # 只改這個行程;正式那份用自己的
    cfg = instance_config(load_config(), root)

    from src.store import Store
    from web.app import create_app
    app = create_app(cfg)
    store = Store(cfg.paths.db_path)
    for email, role, name in members:
        store.set_member(email, role, name, actor="local")
    print(f"看有(獨立實例):http://127.0.0.1:{args.port}  資料在 {root}")
    if serve is None:
        import uvicorn
        serve = uvicorn.run
    # 和正式那份一樣:只信任本機 cloudflared 轉來的 X-Forwarded-Proto
    serve(app, host="127.0.0.1", port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
