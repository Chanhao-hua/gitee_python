from __future__ import annotations

import argparse
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
}
MAX_WORKERS = 4
DEFAULT_DB_PATH = Path("data") / "zol_goods.db"

URL_LIST = [
    {"category": "phone", "url": "https://top.zol.com.cn/compositor/57/cell_phone.html"},
    {"category": "notebook", "url": "https://top.zol.com.cn/compositor/16/notebook.html"},
    {"category": "desktop_pc", "url": "https://top.zol.com.cn/compositor/27/desktop_pc.html"},
    {"category": "tablet", "url": "https://top.zol.com.cn/compositor/702/tablepc.html"},
    {"category": "monitor", "url": "https://top.zol.com.cn/compositor/84/lcd.html"},
    {"category": "motherboard", "url": "https://top.zol.com.cn/compositor/5/motherboard.html"},
    {"category": "cpu", "url": "https://top.zol.com.cn/compositor/28/cpu.html"},
]


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS zol_goods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                goods_name TEXT NOT NULL,
                UNIQUE(category, goods_name)
            )
            """
        )


def batch_insert(
    category: str,
    goods_list: list[str],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    if not goods_list:
        return 0
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [(category, name) for name in goods_list if name]
    with sqlite3.connect(path) as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO zol_goods (category, goods_name) VALUES (?, ?)",
            data,
        )
        return conn.total_changes - before


def crawl_category(category: str, url: str, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    goods_list: list[str] = []
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")

    goods_items = soup.select("div.rank-list__item div.rank-list__cell.cell-3 div.rank__name a")
    for a_tag in goods_items:
        name = a_tag.get_text(strip=True)
        if name:
            goods_list.append(name)

    inserted = batch_insert(category, goods_list, db_path=db_path)
    print(f"{category}: fetched={len(goods_list)}, inserted={inserted}")
    return inserted


def crawl_all(db_path: str | Path = DEFAULT_DB_PATH, max_workers: int = MAX_WORKERS) -> int:
    init_db(db_path)
    total = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(crawl_category, item["category"], item["url"], db_path)
            for item in URL_LIST
        ]
        for future in futures:
            total += future.result()
    return total


def read_goods_names(
    category: str = "phone",
    limit: int = 30,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[str]:
    path = Path(db_path)
    if not path.exists():
        return []
    limit = max(1, int(limit))
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT goods_name
            FROM zol_goods
            WHERE category = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (category, limit),
        ).fetchall()
    return [row[0] for row in rows]


def crawl(
    keyword: str = "phone",
    limit: int = 30,
    allow_live: bool = False,
) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("ZOL crawler is live-only. Please use mode=live.")

    category = keyword or "phone"
    matching = next((item for item in URL_LIST if item["category"] == category), None)
    if matching:
        crawl_category(matching["category"], matching["url"], DEFAULT_DB_PATH)
    else:
        crawl_all(DEFAULT_DB_PATH)

    names = read_goods_names(category=category, limit=limit, db_path=DEFAULT_DB_PATH)
    return [
        {
            "source": "zol",
            "product_id": None,
            "keyword": category,
            "title": name,
            "price": None,
            "merchant": "ZOL",
            "rating_tags": "",
            "comment_count": 0,
            "rank": index + 1,
        }
        for index, name in enumerate(names)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch ZOL rankings into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite output path.")
    parser.add_argument("--category", default="all", help="Category key, e.g. phone, or all.")
    args = parser.parse_args()

    if args.category == "all":
        total = crawl_all(args.db)
    else:
        init_db(args.db)
        item = next((row for row in URL_LIST if row["category"] == args.category), None)
        if not item:
            choices = ", ".join(row["category"] for row in URL_LIST)
            raise SystemExit(f"Unknown category {args.category!r}. Choices: {choices}, all")
        total = crawl_category(item["category"], item["url"], args.db)
    print(f"Done. Inserted {total} new rows into {args.db}")


if __name__ == "__main__":
    main()
