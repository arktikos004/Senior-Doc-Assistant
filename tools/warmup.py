"""先把 Gemma 4 載入顯示記憶體(開機後、展示前):第一張文件就不必等模型載入(冷開機曾到 77 秒)。

用法(專案根目錄):
    python tools/warmup.py                    # 照 Ollama 的設定留著(OLLAMA_KEEP_ALIVE,預設 5 分鐘)
    python tools/warmup.py --keep-alive 3h    # 這次載入後留 3 小時;"-1" 是一直留著

只連 config.yaml 設定的 Ollama(本機),送一個沒有提示詞的 generate 請求:Ollama 只載入模型、不產生文字,
不送任何文件。載好之後 /healthz 的 model_loaded 會是 true。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig, load_config  # noqa: E402


def warm_up(cfg: AppConfig, keep_alive: str | None = None, transport: httpx.BaseTransport | None = None) -> float:
    """載入設定的模型,回傳花了幾秒;連不上或 Ollama 回錯誤丟 httpx.HTTPError。Ollama 在本機,不走系統代理。"""
    body: dict[str, str] = {"model": cfg.ollama.model}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    started = time.monotonic()
    with httpx.Client(transport=transport, timeout=cfg.ollama.timeout, trust_env=False) as client:
        client.post(f"{cfg.ollama.host.rstrip('/')}/api/generate", json=body).raise_for_status()
    return time.monotonic() - started


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description="看有:先把模型載入顯示記憶體")
    parser.add_argument("--keep-alive", default=None,
                        help='載入後留多久,例如 30m、3h;"-1" 是一直留著(沒給就照 Ollama 的設定)')
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        seconds = warm_up(cfg, args.keep_alive, transport)
    except httpx.HTTPError as exc:
        print(f"沒有載入 {cfg.ollama.model}:{type(exc).__name__}。請確認 Ollama 已啟動、模型已下載(ollama list)。")
        return 1
    print(f"已載入 {cfg.ollama.model}(花了 {seconds:.1f} 秒)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
