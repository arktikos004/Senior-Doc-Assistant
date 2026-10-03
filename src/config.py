"""設定檔載入模組:讀取 config.yaml 並轉為型別化設定物件。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 專案根目錄(src/ 的上一層)
BASE_DIR = Path(__file__).resolve().parent.parent

# 預設模型一律選非中資的開源模型(競賽切結書第 6 點;見 README「設定」一節)
DEFAULT_OLLAMA_MODEL = "gemma4:12b"
DEFAULT_WORKERS_AI_MODEL = "@cf/google/gemma-4-26b-a4b-it"
PROVIDERS = ("ollama", "workers_ai", "mock")


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = DEFAULT_OLLAMA_MODEL
    timeout: int = 180


@dataclass
class WorkersAIConfig:
    """Cloudflare Workers AI;帳號與金鑰只從環境變數讀,不寫進設定檔。"""

    model: str = DEFAULT_WORKERS_AI_MODEL
    account_id_env: str = "CF_ACCOUNT_ID"
    api_token_env: str = "CF_API_TOKEN"
    timeout: int = 60


@dataclass
class PathsConfig:
    inbox: Path = BASE_DIR / "data/inbox"
    archive: Path = BASE_DIR / "data/archive"
    review: Path = BASE_DIR / "data/review"
    failed: Path = BASE_DIR / "data/failed"
    logs: Path = BASE_DIR / "logs"
    uploads: Path | None = None  # 網頁上傳暫存;未指定則為 inbox 旁的 uploads/(不被 watcher 監控)
    db: Path | None = None  # SQLite 位置;未指定則放在 logs/ 下(已 gitignore)

    @property
    def db_path(self) -> Path:
        return self.db if self.db is not None else self.logs / "app.db"

    @property
    def uploads_path(self) -> Path:
        return self.uploads if self.uploads is not None else self.inbox.parent / "uploads"


@dataclass
class AppConfig:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    workers_ai: WorkersAIConfig = field(default_factory=WorkersAIConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    provider: str = "ollama"          # ollama / workers_ai / mock
    # 隱私分流:這些類型不論 provider 為何,一律只在本機推論(藥袋屬特種個資)
    local_only_doc_types: tuple[str, ...] = ("藥袋",)
    auto_threshold: float = 0.80
    group_by_month: bool = True
    filename_template: str = "{date}_{doc_type}_{vendor}_{amount}"
    supported_extensions: tuple[str, ...] = (
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".pdf",
    )
    target_doc_types: tuple[str, ...] = ("發票", "收據", "帳單", "公文", "藥袋")

    def ensure_dirs(self) -> None:
        """建立所有工作資料夾(已存在則略過)。"""
        for p in (
            self.paths.inbox,
            self.paths.archive,
            self.paths.review,
            self.paths.failed,
            self.paths.logs,
            self.paths.uploads_path,
            self.paths.db_path.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> AppConfig:
    """從 YAML 載入設定;檔案不存在時使用預設值。

    YAML 沒寫的鍵一律取 dataclass 的預設值(例 OllamaConfig.host),同一個預設只寫在上面一處。
    """
    cfg_path = path or (BASE_DIR / "config.yaml")
    if not cfg_path.exists():
        return AppConfig()

    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def _abs(p: str | Path) -> Path:
        candidate = Path(p)
        return candidate if candidate.is_absolute() else BASE_DIR / candidate

    model_raw = raw.get("model", {})
    ollama_raw = raw.get("ollama", {})
    workers_raw = raw.get("workers_ai", {})
    paths_raw = raw.get("paths", {})
    archive_raw = raw.get("archive", {})
    confidence_raw = raw.get("confidence", {})

    provider = model_raw.get("provider", AppConfig.provider)
    if provider not in PROVIDERS:
        raise ValueError(f"未知的 model.provider:{provider!r}(可用:{', '.join(PROVIDERS)})")

    return AppConfig(
        ollama=OllamaConfig(
            host=ollama_raw.get("host", OllamaConfig.host),
            model=ollama_raw.get("model", OllamaConfig.model),
            timeout=int(ollama_raw.get("timeout", OllamaConfig.timeout)),
        ),
        workers_ai=WorkersAIConfig(
            model=workers_raw.get("model", WorkersAIConfig.model),
            account_id_env=workers_raw.get("account_id_env", WorkersAIConfig.account_id_env),
            api_token_env=workers_raw.get("api_token_env", WorkersAIConfig.api_token_env),
            timeout=int(workers_raw.get("timeout", WorkersAIConfig.timeout)),
        ),
        paths=PathsConfig(
            inbox=_abs(paths_raw.get("inbox", PathsConfig.inbox)),
            archive=_abs(paths_raw.get("archive", PathsConfig.archive)),
            review=_abs(paths_raw.get("review", PathsConfig.review)),
            failed=_abs(paths_raw.get("failed", PathsConfig.failed)),
            logs=_abs(paths_raw.get("logs", PathsConfig.logs)),
            uploads=_abs(paths_raw["uploads"]) if paths_raw.get("uploads") else None,
            db=_abs(paths_raw["db"]) if paths_raw.get("db") else None,
        ),
        provider=provider,
        local_only_doc_types=tuple(model_raw.get("local_only_doc_types", AppConfig.local_only_doc_types)),
        auto_threshold=float(confidence_raw.get("auto_threshold", AppConfig.auto_threshold)),
        group_by_month=bool(archive_raw.get("group_by_month", AppConfig.group_by_month)),
        filename_template=archive_raw.get("filename_template", AppConfig.filename_template),
        supported_extensions=tuple(
            e.lower() for e in raw.get("supported_extensions", [])
        ) or AppConfig.supported_extensions,
        target_doc_types=tuple(raw.get("target_doc_types", []))
        or AppConfig.target_doc_types,
    )
