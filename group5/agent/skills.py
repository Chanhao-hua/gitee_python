"""Whitelisted Agent skills for crawling, scheduling, and querying data."""

from __future__ import annotations

from typing import Any

from storage.db import (
    SOURCE_LABELS,
    compare_sources as db_compare_sources,
    get_database_stats,
    init_database,
    insert_products,
    query_products as db_query_products,
)


SOURCE_ALIASES = {
    "jd": "jd",
    "京东": "jd",
    "JD": "jd",
    "suning": "suning",
    "苏宁": "suning",
    "苏宁易购": "suning",
    "zol": "zol",
    "中关村": "zol",
    "中关村在线": "zol",
    "all": "all",
    "全部": "all",
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_crawl",
            "description": "手动触发一个或多个数据源爬虫，并把真实结果写入 SQLite。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "jd/suning/zol/all"},
                    "keyword": {"type": "string", "description": "商品关键词"},
                    "limit": {"type": "integer", "default": 30},
                    "mode": {"type": "string", "enum": ["live"], "default": "live"},
                },
                "required": ["source", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_crawl",
            "description": "为一个或多个数据源创建定时爬虫任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "jd/suning/zol/all"},
                    "keyword": {"type": "string"},
                    "interval_minutes": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 30},
                    "mode": {"type": "string", "enum": ["live"], "default": "live"},
                },
                "required": ["source", "keyword", "interval_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_products",
            "description": "按关键词和来源查询 SQLite 商品数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "source": {"type": "string", "description": "可选 jd/suning/zol/all"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_sources",
            "description": "聚合比较京东、苏宁易购、中关村在线的数据。",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_status",
            "description": "查看当前定时爬虫任务状态。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def run_crawl(
    source: str = "all",
    keyword: str = "手机",
    limit: int = 30,
    mode: str = "live",
) -> dict[str, Any]:
    init_database(seed=False)
    sources = _expand_sources(source)
    summaries: list[dict[str, Any]] = []
    total_inserted = 0

    for item_source in sources:
        try:
            rows = _crawl_source(item_source, keyword, limit, mode)
            inserted = insert_products(rows)
            total_inserted += inserted
            summaries.append(
                {
                    "source": item_source,
                    "source_label": SOURCE_LABELS[item_source],
                    "ok": True,
                    "inserted": inserted,
                    "error": None,
                }
            )
        except Exception as exc:
            cached = db_query_products(keyword=keyword, source=item_source, limit=limit)
            summaries.append(
                {
                    "source": item_source,
                    "source_label": SOURCE_LABELS[item_source],
                    "ok": False,
                    "inserted": 0,
                    "fallback_rows": len(cached),
                    "error": str(exc),
                }
            )

    failed = [row for row in summaries if not row["ok"]]
    message = f"真实抓取写入 {total_inserted} 条。"
    if failed:
        message += " 失败来源：" + "、".join(
            f"{row['source_label']}({row['error']})" for row in failed
        )

    return {
        "ok": all(row["ok"] for row in summaries),
        "action": "run_crawl",
        "keyword": keyword,
        "sources": summaries,
        "inserted": total_inserted,
        "mode": mode,
        "message": message,
    }


def schedule_crawl(
    source: str = "all",
    keyword: str = "手机",
    interval_minutes: int = 30,
    limit: int = 30,
    mode: str = "live",
) -> dict[str, Any]:
    from jobs.scheduler import schedule_crawl_job

    jobs = [
        schedule_crawl_job(
            source=item_source,
            keyword=keyword,
            interval_minutes=interval_minutes,
            limit=limit,
            mode=mode,
        )
        for item_source in _expand_sources(source)
    ]
    return {
        "ok": True,
        "action": "schedule_crawl",
        "keyword": keyword,
        "interval_minutes": interval_minutes,
        "jobs": jobs,
        "message": f"已创建 {len(jobs)} 个真实抓取定时任务。",
    }


def query_products(
    keyword: str = "手机",
    source: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    normalized_source = _normalize_source(source) if source else None
    rows = db_query_products(keyword=keyword, source=normalized_source, limit=limit)
    return {
        "ok": True,
        "action": "query_products",
        "keyword": keyword,
        "source": normalized_source or "all",
        "count": len(rows),
        "rows": rows,
    }


def compare_sources(keyword: str = "手机") -> dict[str, Any]:
    result = db_compare_sources(keyword=keyword)
    return {"ok": True, "action": "compare_sources", **result}


def job_status() -> dict[str, Any]:
    from jobs.scheduler import get_job_status

    return {
        "ok": True,
        "action": "job_status",
        "jobs": get_job_status(),
        "database": get_database_stats(),
    }


def execute_skill(action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    registry = {
        "run_crawl": run_crawl,
        "schedule_crawl": schedule_crawl,
        "query_products": query_products,
        "compare_sources": compare_sources,
        "job_status": job_status,
    }
    if action not in registry:
        return {"ok": False, "action": action, "error": "未知 skill，已被白名单拦截。"}
    return registry[action](**args)


def _crawl_source(source: str, keyword: str, limit: int, mode: str) -> list[dict[str, Any]]:
    allow_live = mode == "live"
    if source == "jd":
        from spiders.jd_spider import crawl
    elif source == "suning":
        from spiders.suning_spider import crawl
    elif source == "zol":
        from spiders.zol_spider import crawl
    else:
        raise ValueError(f"不支持的数据源: {source}")
    return crawl(keyword=keyword, limit=limit, allow_live=allow_live)


def _expand_sources(source: str | None) -> list[str]:
    if not source:
        return ["jd", "suning", "zol"]
    source_text = str(source).replace("，", ",").replace("、", ",")
    parts = [part.strip() for part in source_text.split(",") if part.strip()]
    normalized = [_normalize_source(part) for part in parts]
    if not normalized or "all" in normalized:
        return ["jd", "suning", "zol"]
    return normalized


def _normalize_source(source: str | None) -> str:
    key = str(source or "all").strip()
    normalized = SOURCE_ALIASES.get(key.lower()) or SOURCE_ALIASES.get(key)
    if not normalized:
        raise ValueError(f"未知数据源: {source}")
    return normalized
