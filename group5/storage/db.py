"""SQLite storage for crawled products and review comments."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SOURCE_LABELS = {
    "suning": "苏宁易购",
    "vip": "唯品会",
    "bilibili": "哔哩哔哩",
    "zol": "中关村在线",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "live_products.db"
DEFAULT_COMMENTS_DB_PATH = PROJECT_ROOT / "data" / "product_comments.db"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"

PRODUCT_COLUMNS = (
    "id",
    "source",
    "keyword",
    "title",
    "price",
    "merchant",
    "comment_count",
    "rank",
    "crawled_at",
)


def get_db_path() -> Path:
    return Path(os.getenv("DEMO_DB_PATH", str(DEFAULT_DB_PATH)))


def get_comments_db_path() -> Path:
    return Path(os.getenv("COMMENTS_DB_PATH", str(DEFAULT_COMMENTS_DB_PATH)))


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_comments_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_comments_db_path()
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
        _create_product_table(conn)
        _migrate_product_table(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_product_source_title_merchant
            ON product_items(source, title, merchant)
            """
        )
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
        if seed:
            raise RuntimeError("当前版本禁止 seed 样例数据，请通过真实爬虫写入 SQLite。")


def init_comments_database(db_path: str | Path | None = None) -> None:
    with get_comments_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                source TEXT NOT NULL,
                merchant TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                title TEXT,
                crawled_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            DROP INDEX IF EXISTS uniq_comment_model_merchant_text
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_comment_source_merchant_title_text
            ON product_comments(source, merchant, title, comment_text)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comments_model_merchant
            ON product_comments(model, merchant)
            """
        )


def insert_products(
    items: Iterable[dict[str, Any]],
    db_path: str | Path | None = None,
    comments_db_path: str | Path | None = None,
) -> int:
    rows = list(items)
    if not rows:
        return 0

    init_database(seed=False, db_path=db_path)
    normalized = [_normalize_item(row) for row in rows]
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO product_items (
                source, keyword, title, price, merchant, comment_count, rank, crawled_at
            )
            VALUES (
                :source, :keyword, :title, :price, :merchant, :comment_count, :rank, :crawled_at
            )
            ON CONFLICT(source, title, merchant) DO UPDATE SET
                keyword = excluded.keyword,
                price = excluded.price,
                comment_count = excluded.comment_count,
                rank = excluded.rank,
                crawled_at = excluded.crawled_at
            """,
            normalized,
        )

    comment_rows = _comments_from_products(rows)
    if comment_rows:
        insert_comments(comment_rows, db_path=comments_db_path)
    return len(rows)


def insert_comments(
    comments: Iterable[dict[str, Any]],
    db_path: str | Path | None = None,
) -> int:
    rows = [_normalize_comment(row) for row in comments]
    rows = [row for row in rows if row["model"] and row["merchant"] and row["comment_text"]]
    if not rows:
        return 0

    init_comments_database(db_path)
    with get_comments_connection(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO product_comments (
                model, source, merchant, comment_text, title, crawled_at
            )
            VALUES (
                :model, :source, :merchant, :comment_text, :title, :crawled_at
            )
            """,
            rows,
        )
        return conn.total_changes - before


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
        clauses.append("(keyword LIKE ? OR title LIKE ? OR merchant LIKE ?)")
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT source, keyword, title, price, merchant, comment_count, rank, crawled_at
        FROM product_items
        {where_sql}
        ORDER BY datetime(crawled_at) DESC, source ASC, price ASC
        LIMIT ?
    """
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def query_comments(
    model: str | None = None,
    merchant: str | None = None,
    source: str | None = None,
    limit: int = 200,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_comments_database(db_path)
    limit = max(1, min(int(limit), 2000))
    clauses: list[str] = []
    params: list[Any] = []

    if model:
        clauses.append("model LIKE ?")
        params.append(f"%{model}%")
    if merchant:
        clauses.append("merchant LIKE ?")
        params.append(f"%{merchant}%")
    if source and source != "all":
        clauses.append("source = ?")
        params.append(source)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT model, source, merchant, comment_text, title, crawled_at
        FROM product_comments
        {where_sql}
        ORDER BY model ASC, merchant ASC, id ASC
        LIMIT ?
    """
    params.append(limit)
    with get_comments_connection(db_path) as conn:
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
        clauses.append("(keyword LIKE ? OR title LIKE ?)")
        params.extend([like, like])
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


def get_comments_stats(db_path: str | Path | None = None) -> dict[str, Any]:
    init_comments_database(db_path)
    with get_comments_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM product_comments").fetchone()[0]
        by_model_merchant = conn.execute(
            """
            SELECT model, merchant, COUNT(*) AS count, MAX(crawled_at) AS latest_crawled_at
            FROM product_comments
            GROUP BY model, merchant
            ORDER BY model ASC, merchant ASC
            """
        ).fetchall()
    return {
        "db_path": str(Path(db_path) if db_path else get_comments_db_path()),
        "total": total,
        "by_model_merchant": [_row_to_dict(row) for row in by_model_merchant],
    }


def _create_product_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            keyword TEXT NOT NULL,
            title TEXT NOT NULL,
            price REAL,
            merchant TEXT,
            comment_count INTEGER DEFAULT 0,
            rank INTEGER,
            crawled_at TEXT NOT NULL
        )
        """
    )


def _migrate_product_table(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_items)").fetchall()}
    obsolete = {"product_id", "rating_tags", "comment_text"}
    if not obsolete.intersection(columns):
        return

    conn.execute("ALTER TABLE product_items RENAME TO product_items_old")
    _drop_product_indexes(conn)
    _create_product_table(conn)
    if "comment_text" in columns:
        old_comment_rows = [
            _row_to_dict(row)
            for row in conn.execute(
                """
                SELECT source, keyword, title, COALESCE(merchant, '') AS merchant,
                       comment_text, crawled_at
                FROM product_items_old
                WHERE comment_text IS NOT NULL AND comment_text <> ''
                """
            ).fetchall()
        ]
        migrated_comments = _comments_from_products(old_comment_rows)
        if migrated_comments:
            insert_comments(migrated_comments)
    copy_columns = [column for column in PRODUCT_COLUMNS if column in columns and column != "id"]
    insert_sql = ", ".join(copy_columns)
    select_sql = ", ".join(
        "COALESCE(merchant, '') AS merchant" if column == "merchant" else column
        for column in copy_columns
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO product_items ({insert_sql})
        SELECT {select_sql}
        FROM product_items_old
        """
    )
    conn.execute("DROP TABLE product_items_old")


def _drop_product_indexes(conn: sqlite3.Connection) -> None:
    for name in (
        "idx_product_source_keyword",
        "idx_product_title",
        "uniq_product_source_pid",
        "uniq_product_source_title",
        "uniq_product_source_title_merchant",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": item.get("source", "unknown"),
        "keyword": item.get("keyword") or item.get("title", ""),
        "title": item.get("title", ""),
        "price": item.get("price"),
        "merchant": item.get("merchant") or "",
        "comment_count": item.get("comment_count") or 0,
        "rank": item.get("rank"),
        "crawled_at": item.get("crawled_at")
        or datetime.now().replace(microsecond=0).isoformat(),
    }


def _normalize_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": _clean_text(item.get("model") or item.get("keyword") or ""),
        "source": item.get("source", "unknown"),
        "merchant": _clean_text(item.get("merchant") or "未知商家"),
        "comment_text": _clean_text(item.get("comment_text") or ""),
        "title": _clean_text(item.get("title") or ""),
        "crawled_at": item.get("crawled_at")
        or datetime.now().replace(microsecond=0).isoformat(),
    }


def _comments_from_products(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        comments = _extract_product_comments(item)
        for comment in comments:
            rows.append(
                {
                    "model": item.get("keyword") or item.get("title") or "",
                    "source": item.get("source", "unknown"),
                    "merchant": item.get("merchant") or "未知商家",
                    "comment_text": comment,
                    "title": item.get("title") or "",
                    "crawled_at": item.get("crawled_at")
                    or datetime.now().replace(microsecond=0).isoformat(),
                }
            )
    return rows


def _extract_product_comments(item: dict[str, Any]) -> list[str]:
    raw_comments = item.get("comments")
    if isinstance(raw_comments, (list, tuple)):
        comments = [_clean_text(comment) for comment in raw_comments]
        seen: set[str] = set()
        return [
            comment
            for comment in comments
            if comment and not (comment in seen or seen.add(comment))
        ]
    return _split_comment_text(item.get("comment_text"))


def _split_comment_text(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    parts = re.split(r"\s*\|\|\s*|\n+", text)
    seen: set[str] = set()
    comments: list[str] = []
    for part in parts:
        comment = _clean_text(part)
        if comment and comment not in seen:
            seen.add(comment)
            comments.append(comment)
    return comments


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
