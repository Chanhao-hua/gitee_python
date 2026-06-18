from __future__ import annotations

import argparse
import re
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
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "zol_goods.db"

BAD_GOODS_NAMES = {
    "口碑榜",
    "每日新品",
    "玩转手机",
    "视频频道",
    "评测图解",
    "维修库",
    "解决方案库",
    "试用中心",
    "产品微动态",
    "参数",
    "图片",
    "点评",
    "评测",
    "综合介绍",
    "去点评 >",
    "查看更多",
    "更多",
}

PHONE_BRAND_ONLY = {
    "vivo",
    "华为",
    "oppo",
    "荣耀",
    "苹果",
    "小米",
    "iqoo",
    "红米",
    "一加",
    "真我",
    "三星",
    "moto",
    "努比亚",
    "联想",
    "诺基亚",
    "魅族",
    "中兴",
    "wiko",
    "索尼移动",
    "谷歌",
    "vertu",
    "麦芒",
    "金立",
    "黑鲨",
    "hi nova",
    "rog",
    "天语",
    "征服",
    "酷派",
    "纽曼",
    "agm",
    "u-magic",
    "飞利浦",
    "nzone",
    "多亲",
    "华硕",
    "海信",
    "克里特",
    "小辣椒",
    "朵唯",
    "unihertz",
    "鼎桥通信",
    "柔宇",
    "黑莓",
    "中国电信",
    "htc",
    "美图",
    "lg",
    "索爱",
}

NOTEBOOK_BRAND_ONLY = {
    "联想",
    "惠普",
    "华硕",
    "戴尔",
    "宏碁",
    "机械革命",
    "thinkpad",
    "rog",
    "苹果",
    "神舟",
    "华为",
    "荣耀",
    "小米",
    "微软",
    "雷神",
    "机械师",
    "外星人",
    "msi",
}

NOTEBOOK_EXCLUDE_IN_PHONE = (
    "book",
    "thinkpad",
    "thinkbook",
    "matebook",
    "magicbook",
    "macbook",
    "surface",
    "vostro",
    "latitude",
    "inspiron",
    "ideapad",
    "yoga",
    "elitebook",
    "chromebook",
    "ezbook",
    "笔记本",
    "电脑",
    "酷睿",
    "锐龙",
    "rtx",
)

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
    data = [(category, name) for name in _filter_goods_names(category, goods_list)]
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
        if is_valid_goods_name(category, name):
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
    seen: set[str] = set()
    filtered: list[str] = []
    for row in rows:
        name = _clean_goods_name(row[0])
        if name and name not in seen and is_valid_goods_name(category, name):
            seen.add(name)
            filtered.append(name)
    return filtered


def _filter_goods_names(category: str, goods_list: list[str]) -> list[str]:
    seen: set[str] = set()
    filtered: list[str] = []
    for name in goods_list:
        cleaned = _clean_goods_name(name)
        if cleaned and cleaned not in seen and is_valid_goods_name(category, cleaned):
            seen.add(cleaned)
            filtered.append(cleaned)
    return filtered


def is_valid_goods_name(category: str, name: str) -> bool:
    text = _clean_goods_name(name)
    if not text or text in BAD_GOODS_NAMES:
        return False
    lowered = text.lower()
    if re.fullmatch(r"\[\s*共\d+款\s*\]", text):
        return False
    if re.fullmatch(r"共\d+款", text):
        return False
    if lowered in PHONE_BRAND_ONLY or lowered in NOTEBOOK_BRAND_ONLY:
        return False
    if len(text) < 4:
        return False

    if category == "phone":
        return _looks_like_phone_model(text)
    if category == "notebook":
        return _looks_like_notebook_model(text)
    return True


def _looks_like_phone_model(name: str) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in NOTEBOOK_EXCLUDE_IN_PHONE):
        return False
    phone_markers = (
        "iphone",
        "nova",
        "pura",
        "畅享",
        "redmi",
        "vivo",
        "oppo",
        "reno",
        "find",
        "iqoo",
        "一加",
        "ace",
        "真我",
        "realme",
        "moto",
        "galaxy",
        "小米",
        "华为",
        "荣耀",
        "magic",
        "note",
    )
    has_marker = any(marker in lowered for marker in phone_markers)
    has_model_number = bool(re.search(r"\d", name))
    has_capacity = bool(re.search(r"\d+\s*(gb|g|tb|t)", lowered))
    return has_marker and (has_model_number or has_capacity)


def _looks_like_notebook_model(name: str) -> bool:
    lowered = name.lower()
    notebook_markers = (
        "book",
        "thinkpad",
        "rog",
        "macbook",
        "灵耀",
        "天选",
        "小新",
        "拯救者",
        "暗影精灵",
        "战",
        "无界",
        "matebook",
        "戴尔",
        "惠普",
        "华硕",
        "联想",
        "机械革命",
        "神舟",
        "酷睿",
        "锐龙",
        "rtx",
    )
    has_marker = any(marker in lowered for marker in notebook_markers)
    has_model_number = bool(re.search(r"\d", name))
    return has_marker and has_model_number


def _clean_goods_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


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
