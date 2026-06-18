from __future__ import annotations

import os

import pytest


def test_database_starts_empty_without_seed(tmp_path):
    os.environ["DEMO_DB_PATH"] = str(tmp_path / "live.db")
    from storage.db import get_database_stats, init_database

    init_database(seed=False)
    stats = get_database_stats()
    assert stats["total"] == 0


def test_agent_fallback_schedules_live_jobs(tmp_path):
    os.environ["DEMO_DB_PATH"] = str(tmp_path / "live.db")
    orchestrator_module = pytest.importorskip("agent.orchestrator")
    from storage.db import init_database

    init_database(seed=False)
    orchestrator = orchestrator_module.AgentOrchestrator()
    orchestrator.api_key = None
    result = orchestrator.handle_command(
        "每30分钟抓取 iPhone 16 苏宁和哔哩哔哩价格、商家、评论数量和具体评论"
    )
    assert result["ok"] is True
    assert result["routing"] == "fallback_json_action"
    jobs = result["result"]["jobs"]
    assert len(jobs) == 2
    assert {job["source"] for job in jobs} == {"bilibili", "suning"}


def test_bilibili_title_cleanup():
    from spiders.bilibili_spider import _clean_title

    assert _clean_title('<em class="keyword">iPhone</em> 15 深度评测') == "iPhone 15 深度评测"


def test_product_comments_are_stored_separately(tmp_path):
    db_path = tmp_path / "live.db"
    comments_db_path = tmp_path / "comments.db"
    from storage.db import init_database, insert_products, query_comments, query_products

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
                "comment_text": "手机很好用，续航也不错 || 拍照清晰",
                "rank": None,
            }
        ],
        db_path=db_path,
        comments_db_path=comments_db_path,
    )

    rows = query_products(keyword="手机", db_path=db_path)
    assert "comment_text" not in rows[0]
    assert "rating_tags" not in rows[0]
    comments = query_comments(model="手机", merchant="真实商家", db_path=comments_db_path)
    assert [row["comment_text"] for row in comments] == ["手机很好用，续航也不错", "拍照清晰"]


def test_product_comments_can_be_written_from_comment_list(tmp_path):
    db_path = tmp_path / "live.db"
    comments_db_path = tmp_path / "comments.db"
    from storage.db import insert_products, query_comments, query_products

    insert_products(
        [
            {
                "source": "bilibili",
                "keyword": "iPhone 15",
                "title": "iPhone 15 深度评测",
                "price": None,
                "merchant": "BV1W64y1A7cn",
                "comment_count": 2,
                "comments": ["第一条公开评论", "第二条公开评论"],
                "rank": 1,
            }
        ],
        db_path=db_path,
        comments_db_path=comments_db_path,
    )

    products = query_products(keyword="iPhone 15", db_path=db_path)
    assert "comment_text" not in products[0]
    comments = query_comments(model="iPhone 15", merchant="BV1W64y1A7cn", db_path=comments_db_path)
    assert [row["comment_text"] for row in comments] == ["第一条公开评论", "第二条公开评论"]


def test_repeated_insert_upserts_instead_of_duplicating(tmp_path):
    db_path = tmp_path / "live.db"
    comments_db_path = tmp_path / "comments.db"
    from storage.db import (
        get_database_stats,
        init_database,
        insert_products,
        query_products,
    )

    init_database(seed=False, db_path=db_path)
    base = {
        "source": "bilibili",
        "keyword": "手机",
        "title": "Xiaomi 15 体验视频",
        "price": None,
        "merchant": "BV1xx411c7mD",
        "comment_count": 200,
        "comment_text": "拍照清晰，续航满意",
        "rank": 1,
    }
    insert_products([base], db_path=db_path, comments_db_path=comments_db_path)
    updated = {
        **base,
        "price": None,
        "comment_count": 250,
        "comment_text": "屏幕清晰，运行流畅",
    }
    insert_products([updated], db_path=db_path, comments_db_path=comments_db_path)

    stats = get_database_stats(db_path=db_path)
    assert stats["total"] == 1
    rows = query_products(keyword="手机", db_path=db_path)
    assert rows[0]["merchant"] == "BV1xx411c7mD"
    assert rows[0]["comment_count"] == 250
    assert "rating_tags" not in rows[0]
    assert "comment_text" not in rows[0]
