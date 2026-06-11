"""SQLite storage for real crawled product data."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SOURCE_LABELS = {
    "jd": "京东",
    "suning": "苏宁易购",
    "zol": "中关村在线",
}

DEFAULT_DB_PATH = Path("data") / "live_products.db"
DEFAULT_LOG_DIR = Path("logs")


def get_db_path() -> Path:
    return Path(os.getenv("DEMO_DB_PATH", str(DEFAULT_DB_PATH)))


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def configure_logging(log_dir: str | Path | None = None) -> None:
    """Set up a rotating file handler so spiders/jobs leave a paper trail."""
    target = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_demo_configured", False) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        target / "spider.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    handler._demo_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def init_database(seed: bool = False, db_path: str | Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                product_id TEXT,
                keyword TEXT NOT NULL,
                title TEXT NOT NULL,
                price REAL,
                merchant TEXT,
                rating_tags TEXT,
                comment_count INTEGER DEFAULT 0,
                rank INTEGER,
                crawled_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "product_items", "rating_tags", "TEXT")
        _ensure_column(conn, "product_items", "product_id", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_product_source_keyword
            ON product_items(source, keyword)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_product_title
            ON product_items(title)
            """
        )
        # UNIQUE(source, product_id) when product_id is set;
        # for rows without product_id, fall back to (source, title).
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_product_source_pid
            ON product_items(source, product_id)
            WHERE product_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_product_source_title
            ON product_items(source, title)
            WHERE product_id IS NULL
            """
        )
        if seed:
            raise RuntimeError("当前版本禁止 seed 样例数据，请通过真实爬虫写入 SQLite。")


def insert_products(
    items: Iterable[dict[str, Any]], db_path: str | Path | None = None
) -> int:
    rows = list(items)
    if not rows:
        return 0

    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO product_items (
                source, product_id, keyword, title, price, merchant, rating_tags,
                comment_count, rank, crawled_at
            )
            VALUES (
                :source, :product_id, :keyword, :title, :price, :merchant, :rating_tags,
                :comment_count, :rank, :crawled_at
            )
            ON CONFLICT(source, product_id) WHERE product_id IS NOT NULL DO UPDATE SET
                title = excluded.title,
                price = excluded.price,
                merchant = excluded.merchant,
                rating_tags = excluded.rating_tags,
                comment_count = excluded.comment_count,
                rank = excluded.rank,
                crawled_at = excluded.crawled_at
            """,
            [_normalize_item(row) for row in rows],
        )
        # Rows without product_id use a separate UNIQUE on (source,title);
        # fall back to a second pass for those to avoid SQL UNION complexity above.
        conn.executemany(
            """
            UPDATE product_items
            SET price = :price,
                merchant = :merchant,
                rating_tags = :rating_tags,
                comment_count = :comment_count,
                rank = :rank,
                crawled_at = :crawled_at
            WHERE source = :source AND title = :title AND product_id IS NULL
            """,
            [_normalize_item(row) for row in rows if not row.get("product_id")],
        )
    return len(rows)


def query_products(
    keyword: str | None = None,
    source: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_database(seed=False, db_path=db_path)
    limit = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    params: list[Any] = []

    if source and source != "all":
        clauses.append("source = ?")
        params.append(source)

    if keyword:
        like = f"%{keyword}%"
        clauses.append(
            "(keyword LIKE ? OR title LIKE ? OR merchant LIKE ? OR rating_tags LIKE ?)"
        )
        params.extend([like, like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT source, keyword, title, price, merchant, rating_tags,
               comment_count, rank, crawled_at
        FROM product_items
        {where_sql}
        ORDER BY datetime(crawled_at) DESC, source ASC, price ASC
        LIMIT ?
    """
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def compare_sources(
    keyword: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    init_database(seed=False, db_path=db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if keyword:
        like = f"%{keyword}%"
        clauses.append("(keyword LIKE ? OR title LIKE ? OR rating_tags LIKE ?)")
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT source,
                   COUNT(*) AS item_count,
                   MIN(price) AS min_price,
                   ROUND(AVG(price), 2) AS avg_price,
                   COUNT(DISTINCT merchant) AS merchant_count,
                   SUM(comment_count) AS comment_total,
                   MIN(rank) AS best_rank,
                   MAX(crawled_at) AS latest_crawled_at
            FROM product_items
            {where_sql}
            GROUP BY source
            ORDER BY source
            """,
            params,
        ).fetchall()

    aggregates = [_row_to_dict(row) for row in rows]
    return {
        "keyword": keyword or "全部",
        "aggregates": aggregates,
        "source_labels": SOURCE_LABELS,
        "rows": query_products(keyword=keyword, limit=60, db_path=db_path),
    }


def get_database_stats(db_path: str | Path | None = None) -> dict[str, Any]:
    init_database(seed=False, db_path=db_path)
    with get_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM product_items").fetchone()[0]
        by_source = conn.execute(
            """
            SELECT source, COUNT(*) AS count, MAX(crawled_at) AS latest_crawled_at
            FROM product_items
            GROUP BY source
            ORDER BY source
            """
        ).fetchall()
    return {
        "db_path": str(Path(db_path) if db_path else get_db_path()),
        "total": total,
        "by_source": [_row_to_dict(row) for row in by_source],
    }


def _ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": item.get("source", "unknown"),
        "product_id": item.get("product_id"),
        "keyword": item.get("keyword") or item.get("title", ""),
        "title": item.get("title", ""),
        "price": item.get("price"),
        "merchant": item.get("merchant"),
        "rating_tags": item.get("rating_tags") or item.get("evaluation_tags") or "",
        "comment_count": item.get("comment_count") or 0,
        "rank": item.get("rank"),
        "crawled_at": item.get("crawled_at")
        or datetime.now().replace(microsecond=0).isoformat(),
    }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
