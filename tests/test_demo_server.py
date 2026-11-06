"""展示伺服器(tools/demo_server.py)測試:示範讀值跟著展示日走,種好資料後首頁看得到帳單與公文的期限。

資料夾全部換到 tmp_path,不碰 data/demo_run 與 data/samples;不連網、不呼叫模型(DemoAnalyzer 是預錄讀值)。
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import demo_server  # noqa: E402
import make_demo_docs  # noqa: E402
from web.render import date_parts  # noqa: E402


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_server, "DEMO_ROOT", tmp_path / "run")
    monkeypatch.setattr(demo_server, "DEMO_DOCS", tmp_path / "docs")
    monkeypatch.setattr(demo_server, "EINVOICE_DIR", tmp_path / "einvoice")      # 沒有發票:seed 會略過
    return tmp_path


def test_readings_use_the_same_dates_as_the_printed_documents(demo):
    as_of = date(2026, 11, 7)
    make_demo_docs.generate(demo_server.DEMO_DOCS, as_of)
    analyzer = demo_server.DemoAnalyzer(as_of)
    dates = make_demo_docs.demo_dates(as_of)
    bill = analyzer.analyze(demo_server.DEMO_DOCS / "範例帳單_電費.png", "帳單")
    assert bill.date == dates["bill_issued"].isoformat() and bill.fields["due_date"] == dates["bill_due"].isoformat()
    assert bill.amount == make_demo_docs.BILL_AMOUNT and bill.source_model == "demo:模擬讀值"
    letter = analyzer.analyze(demo_server.DEMO_DOCS / "範例公文_補正通知.png", "公文")
    assert letter.date == dates["letter_issued"].isoformat()
    bag = analyzer.analyze(demo_server.DEMO_DOCS / "範例藥袋.png", "藥袋")
    assert bag.date == dates["dispensed"].isoformat()


def test_seeded_demo_lists_the_bill_and_the_letter_on_todays_home_page(demo):
    """不管哪一天跑,種好的展示資料在首頁「要記得的事」都看得到電費與公文的期限(日期不會過期)。"""
    today = date.today()
    make_demo_docs.generate(demo_server.DEMO_DOCS, today)
    cfg, analyzer = demo_server.demo_config(), demo_server.DemoAnalyzer(today)
    demo_server.seed(cfg, analyzer)

    from web.app import create_app
    html = TestClient(create_app(cfg, analyzer=analyzer)).get("/").text
    section = html[html.index('id="remind-title"'):html.index('id="recent-title"')]
    due = make_demo_docs.demo_dates(today)["bill_due"]
    assert "範例電力公司" in section and f"{due.month}/{due.day} 前" in section
    assert "範例市稅務局" in section
