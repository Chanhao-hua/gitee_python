from __future__ import annotations

import os


def test_database_starts_empty_without_seed(tmp_path):
    os.environ["DEMO_DB_PATH"] = str(tmp_path / "live.db")
    from storage.db import get_database_stats, init_database

    init_database(seed=False)
    stats = get_database_stats()
    assert stats["total"] == 0


def test_agent_fallback_schedules_live_jobs(tmp_path):
    os.environ["DEMO_DB_PATH"] = str(tmp_path / "live.db")
    from agent.orchestrator import AgentOrchestrator
    from storage.db import init_database

    init_database(seed=False)
    orchestrator = AgentOrchestrator()
    orchestrator.api_key = None
    result = orchestrator.handle_command(
        "每30分钟抓取 iPhone 16 京东和苏宁价格、商家、评论数量和评价标签"
    )
    assert result["ok"] is True
    assert result["routing"] == "fallback_json_action"
    jobs = result["result"]["jobs"]
    assert len(jobs) == 2
    assert {job["source"] for job in jobs} == {"jd", "suning"}


def test_jd_requires_scrapingbee_key(monkeypatch):
    monkeypatch.delenv("SCRAPINGBEE_API_KEY", raising=False)
    from spiders.jd_spider import crawl

    try:
        crawl("iPhone 16", limit=3, allow_live=True)
    except RuntimeError as exc:
        assert "SCRAPINGBEE_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("JD crawler must not fabricate data without ScrapingBee credentials")


def test_query_returns_rating_tags_not_comment_text(tmp_path):
    db_path = tmp_path / "live.db"
    from storage.db import init_database, insert_products, query_products

    init_database(seed=False, db_path=db_path)
    insert_products(
        [
            {
                "source": "suning",
                "product_id": "1001",
                "keyword": "手机",
                "title": "真实商品标题",
                "price": 3999.0,
                "merchant": "真实商家",
                "comment_count": 128,
                "rating_tags": "系统流畅、外观漂亮",
                "rank": None,
            }
        ],
        db_path=db_path,
    )

    rows = query_products(keyword="手机", db_path=db_path)
    assert rows[0]["rating_tags"] == "系统流畅、外观漂亮"
    assert "comment_text" not in rows[0]


def test_repeated_insert_upserts_instead_of_duplicating(tmp_path):
    db_path = tmp_path / "live.db"
    from storage.db import (
        get_database_stats,
        init_database,
        insert_products,
        query_products,
    )

    init_database(seed=False, db_path=db_path)
    base = {
        "source": "jd",
        "product_id": "100012345",
        "keyword": "手机",
        "title": "Xiaomi 15 12+512G",
        "price": 4999.0,
        "merchant": "小米官方旗舰店",
        "comment_count": 200,
        "rating_tags": "拍照好、续航久",
        "rank": 1,
    }
    insert_products([base], db_path=db_path)
    updated = {**base, "price": 4799.0, "comment_count": 250, "rating_tags": "性价比高、屏幕清晰"}
    insert_products([updated], db_path=db_path)

    stats = get_database_stats(db_path=db_path)
    assert stats["total"] == 1
    rows = query_products(keyword="手机", db_path=db_path)
    assert rows[0]["price"] == 4799.0
    assert rows[0]["comment_count"] == 250
    assert rows[0]["rating_tags"] == "性价比高、屏幕清晰"
