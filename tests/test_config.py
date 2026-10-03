"""設定檔載入測試:YAML 沒寫的鍵一律用 dataclass 預設值(預設值只寫在一處)。"""
import pytest

from src.config import BASE_DIR, AppConfig, OllamaConfig, PathsConfig, load_config


def _load(tmp_path, text: str) -> AppConfig:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_missing_file_uses_defaults(tmp_path):
    assert load_config(tmp_path / "沒有這個檔.yaml") == AppConfig()


def test_empty_yaml_equals_dataclass_defaults(tmp_path):
    assert _load(tmp_path, "") == AppConfig()


def test_yaml_overrides_only_given_keys(tmp_path):
    cfg = _load(tmp_path, """\
ollama:
  timeout: "30"
paths:
  inbox: "x/inbox"
confidence:
  auto_threshold: 0.9
""")
    assert cfg.ollama == OllamaConfig(timeout=30)            # 沒寫的 host、model 用預設
    assert cfg.paths.inbox == BASE_DIR / "x/inbox"           # 相對路徑以專案根目錄為準
    assert cfg.paths.archive == PathsConfig().archive
    assert cfg.auto_threshold == 0.9
    assert cfg.local_only_doc_types == AppConfig().local_only_doc_types


def test_unknown_provider_rejected(tmp_path):
    with pytest.raises(ValueError, match="provider"):
        _load(tmp_path, "model:\n  provider: other\n")
