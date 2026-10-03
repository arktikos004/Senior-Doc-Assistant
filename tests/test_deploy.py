"""部署檔(W2-B)的靜態檢查:只綁本機、Python 一律經 venv 的 -m、範例與指南沒有真的網域、帳號或 ID。

Mac 上沒有 Windows PowerShell 可以實跑腳本,這裡檢查最容易寫錯、寫錯了又最難發現的地方;
實際能不能開機自動啟動,要在 GPU 電腦上依 docs/部署指南.md 的驗收清單人工確認。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
GUIDE = ROOT / "docs" / "部署指南.md"
SCRIPTS = sorted((DEPLOY / "windows").glob("*.ps1"))
START = DEPLOY / "windows" / "start-services.ps1"
TUNNEL_EXAMPLE = DEPLOY / "cloudflared" / "config.example.yml"
PUBLISHED = [*SCRIPTS, TUNNEL_EXAMPLE, GUIDE]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_expected_files_exist():
    assert {p.name for p in SCRIPTS} >= {"start-services.ps1", "install-autostart.ps1"}
    assert TUNNEL_EXAMPLE.is_file() and GUIDE.is_file()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_utf8_with_bom(script):
    """Windows PowerShell 5.1 讀沒有 BOM 的檔會用系統編碼(cp950),中文訊息會亂掉,甚至解析失敗。"""
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")


# 雙引號字串裡,"$變數:" 會被當成「磁碟:變數」,緊接的中文字會被當成變數名稱的一部分;要寫成 "${變數}"
_GLUED_VARIABLE = re.compile(r"\$(?!env:)[A-Za-z_]\w*?(?::|[一-鿿])")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_variables_are_not_glued_to_colons_or_chinese(script):
    for number, line in enumerate(read(script).splitlines(), 1):
        if not line.lstrip().startswith("#"):
            assert not _GLUED_VARIABLE.search(line), f"{script.name}:{number}: {line.strip()}"


def test_services_only_listen_on_localhost():
    text = read(START)
    assert re.findall(r'"--host",\s*"([^"]+)"', text) == ["127.0.0.1"]                  # 網頁
    assert re.findall(r'OLLAMA_HOST\s*=\s*"([^"]+)"', text) == ["127.0.0.1:11434"]       # Ollama
    assert re.findall(r"service:\s*(https?://\S+)", read(TUNNEL_EXAMPLE)) == ["http://127.0.0.1:8000"]
    assert "http_status:404" in read(TUNNEL_EXAMPLE)          # 其他主機名稱一律 404


def test_python_always_runs_through_the_venv_interpreter():
    """資料夾改過名後,.venv\\Scripts\\ 裡的 uvicorn.exe、pip.exe 啟動器還記著舊路徑,一律 python -m。"""
    text = read(START)
    assert '".venv\\Scripts\\python.exe"' in text and '@("-m", "uvicorn"' in text
    for path in PUBLISHED:
        assert not re.search(r"Scripts\\(uvicorn|pip|activate)", read(path), re.I), path.name
        for line in read(path).splitlines():
            assert not re.match(r"\s*(pip|pip3|uvicorn)\s", line), f"{path.name}: {line.strip()}"


def test_tunnel_example_has_only_placeholder_ids():
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", read(TUNNEL_EXAMPLE), re.I)
    assert uuids and all(set(u) <= set("0-") for u in uuids)


_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|tw|io|dev)\b", re.I)
# 範例網址只用 example.com(RFC 2606 保留);其他只能是官方文件或下載的網域
# argotunnel.com 是 cloudflared 連出去的 Cloudflare 端點(7844 埠),指南的防火牆疑難排解要寫到它
_ALLOWED_DOMAINS = ("example.com", "cloudflare.com", "cloudflareaccess.com", "argotunnel.com", "ollama.com",
                    "github.com", "microsoft.com", "python.org")


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: p.name)
def test_no_real_domains_accounts_or_ids(path):
    """匿名與個資:不得出現家裡真的網域、帳號 email、Cloudflare 帳號或通道 ID。"""
    text = read(path)
    for host in {h.lower() for h in _HOST.findall(text)}:
        assert any(host == d or host.endswith("." + d) for d in _ALLOWED_DOMAINS), host
    for domain in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text):
        assert domain == "example.com", domain
    assert not re.search(r"\b[0-9a-f]{32}\b", text, re.I)     # Cloudflare 帳號 ID 的長相
