"""Bilibili crawler driven by ZOL product models.

The crawler uses public Bilibili web APIs after warming a browser-like session.
Each stored product row represents one video: title is the video title, merchant
stores the BV id, and comments are written directly into product_comments.db.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZOL_DB = PROJECT_ROOT / "data" / "zol_goods.db"
DEFAULT_CATEGORY = "phone,notebook"

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
REPLY_API = "https://api.bilibili.com/x/v2/reply"

REQUEST_DELAY_RANGE = (
    float(os.getenv("BILIBILI_DELAY_MIN", "0.6")),
    float(os.getenv("BILIBILI_DELAY_MAX", "1.4")),
)
MAX_RETRIES = int(os.getenv("BILIBILI_MAX_RETRIES", "3"))
COMMENTS_PER_VIDEO = int(os.getenv("BILIBILI_COMMENTS_PER_VIDEO", "20"))
MAX_SEARCH_PAGES = int(os.getenv("BILIBILI_MAX_SEARCH_PAGES", "10"))


def crawl(
    keyword: str,
    limit: int = 30,
    allow_live: bool = False,
    fetch_comments: bool = True,
    comments_per_video: int = COMMENTS_PER_VIDEO,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("B 站爬虫已切换为真实数据模式，请使用 mode=live。")

    keyword = _clean_text(keyword)
    if not keyword:
        raise ValueError("关键词不能为空。")

    session = _create_session(keyword)
    rows: list[dict[str, Any]] = []
    seen_bvids: set[str] = set()
    max_pages = max(1, MAX_SEARCH_PAGES)
    limit = max(1, int(limit))
    comments_per_video = max(1, int(comments_per_video))

    logger.info("Bilibili crawl start: keyword=%r limit=%d", keyword, limit)
    for page in range(1, max_pages + 1):
        videos = _search_videos(session, keyword, page=page)
        if not videos:
            break

        for video in videos:
            bvid = _clean_text(video.get("bvid"))
            if not bvid or bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)

            title = _clean_title(video.get("title"))
            aid = video.get("aid") or _fetch_aid(session, bvid)
            if not aid or not title:
                continue

            comments: list[str] = []
            comment_total = 0
            if fetch_comments:
                _sleep_jitter()
                comments, comment_total = _fetch_comments(
                    session,
                    aid=int(aid),
                    limit=comments_per_video,
                    referer=f"https://www.bilibili.com/video/{bvid}/",
                )
                if not comments:
                    continue

            row = {
                "source": "bilibili",
                "keyword": keyword,
                "title": title,
                "price": None,
                "merchant": bvid,
                "comment_count": comment_total or len(comments),
                "comments": comments,
                "rank": len(rows) + 1,
                "crawled_at": datetime.now().replace(microsecond=0).isoformat(),
            }
            rows.append(row)
            if row_callback:
                row_callback(row, len(rows))
            if len(rows) >= limit:
                break

        if len(rows) >= limit:
            break
        _sleep_jitter()

    if not rows:
        raise RuntimeError("B 站公开接口可访问，但本次未抓到带评论的视频数据。")
    logger.info("Bilibili crawl ok: %d rows", len(rows))
    return rows


def crawl_from_zol_models(
    zol_db_path: str | Path = DEFAULT_ZOL_DB,
    categories: str = DEFAULT_CATEGORY,
    total_limit: int = 150,
    limit_per_model: int = 3,
    model_limit: int = 80,
    save: bool = True,
    comments_per_video: int = COMMENTS_PER_VIDEO,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    _ensure_project_root_on_path()
    from spiders.zol_spider import URL_LIST, crawl_category, init_db, read_goods_names

    category_list = [item.strip() for item in categories.split(",") if item.strip()]
    if not category_list:
        category_list = ["phone", "notebook"]

    zol_db_path = Path(zol_db_path)
    init_db(zol_db_path)
    models: list[str] = []
    for category in category_list:
        category_models = read_goods_names(category=category, limit=model_limit, db_path=zol_db_path)
        if not category_models:
            matching = next((item for item in URL_LIST if item["category"] == category), None)
            if matching:
                crawl_category(matching["category"], matching["url"], db_path=zol_db_path)
                category_models = read_goods_names(
                    category=category,
                    limit=model_limit,
                    db_path=zol_db_path,
                )
        models.extend(category_models)

    models = _dedupe(models)
    if not models:
        raise RuntimeError(f"没有在 {zol_db_path} 中找到可用的 ZOL 手机/电脑型号。")

    rows: list[dict[str, Any]] = []
    total_limit = max(1, int(total_limit))
    limit_per_model = max(1, int(limit_per_model))
    for model in models:
        if len(rows) >= total_limit:
            break
        remaining = total_limit - len(rows)

        def model_callback(row: dict[str, Any], local_index: int) -> None:
            if row_callback:
                row_callback(row, len(rows) + local_index)

        try:
            model_rows = crawl(
                keyword=model,
                limit=min(limit_per_model, remaining),
                allow_live=True,
                fetch_comments=True,
                comments_per_video=comments_per_video,
                row_callback=model_callback,
            )
        except Exception as exc:
            logger.warning("Bilibili crawl failed for ZOL model=%s: %s", model, exc)
            continue
        rows.extend(model_rows)
        if save and not row_callback:
            _save_rows(model_rows)
        _sleep_jitter()

    return rows


def _create_session(keyword: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://search.bilibili.com",
            "Referer": f"https://search.bilibili.com/all?keyword={quote(keyword)}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
    )
    cookie_header = os.getenv("BILIBILI_COOKIE", "").strip()
    if cookie_header:
        session.headers["Cookie"] = cookie_header
    _warm_session(session, keyword)
    return session


def _warm_session(session: requests.Session, keyword: str) -> None:
    warm_urls = (
        "https://www.bilibili.com/",
        f"https://search.bilibili.com/all?keyword={quote(keyword)}",
    )
    for url in warm_urls:
        try:
            session.get(url, timeout=15)
        except requests.RequestException as exc:
            logger.debug("Bilibili warm request failed: %s (%s)", url, exc)


def _search_videos(session: requests.Session, keyword: str, page: int) -> list[dict[str, Any]]:
    data = _get_json(
        session,
        SEARCH_API,
        params={"search_type": "video", "keyword": keyword, "page": page},
        referer=f"https://search.bilibili.com/all?keyword={quote(keyword)}",
    )
    result = (data.get("data") or {}).get("result") or []
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def _fetch_aid(session: requests.Session, bvid: str) -> int | None:
    data = _get_json(
        session,
        VIEW_API,
        params={"bvid": bvid},
        referer=f"https://www.bilibili.com/video/{bvid}/",
    )
    aid = (data.get("data") or {}).get("aid")
    try:
        return int(aid)
    except (TypeError, ValueError):
        return None


def _fetch_comments(
    session: requests.Session,
    aid: int,
    limit: int,
    referer: str,
) -> tuple[list[str], int]:
    comments: list[str] = []
    total = 0
    page_size = min(20, max(1, limit))
    page = 1
    while len(comments) < limit:
        data = _get_json(
            session,
            REPLY_API,
            params={"type": 1, "oid": aid, "sort": 2, "pn": page, "ps": page_size},
            referer=referer,
        )
        payload = data.get("data") or {}
        total = int((payload.get("page") or {}).get("count") or total or 0)
        replies = payload.get("replies") or []
        if not replies:
            break
        for reply in replies:
            message = ((reply.get("content") or {}).get("message") or "").strip()
            message = _clean_comment(message)
            if message and message not in comments:
                comments.append(message)
            if len(comments) >= limit:
                break
        if len(replies) < page_size:
            break
        page += 1
        _sleep_jitter()
    return comments, total


def _get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    referer: str,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"Referer": referer}
            response = session.get(url, params=params, headers=headers, timeout=15)
            if response.status_code == 412 and attempt == 1:
                _warm_session(session, str(params.get("keyword") or ""))
                _sleep_jitter()
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or f"B 站接口返回 code={data.get('code')}")
            return data
        except Exception as exc:
            last_exc = exc
            backoff = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            logger.debug("Bilibili request retry %d/%d: %s", attempt, MAX_RETRIES, exc)
            time.sleep(backoff)
    raise RuntimeError(f"B 站接口请求失败：{url} ({last_exc})")


def _choose_interactively() -> tuple[str, str]:
    print("请选择爬取方式：")
    print("  1) 爬型号")
    print("  2) 爬类型")
    mode = input("请输入 [1/2，默认 2]：").strip() or "2"
    if mode == "1":
        return "model", input("请输入要爬取的型号：").strip()

    print("请选择类型：")
    print("  1) 手机")
    print("  2) 电脑")
    category = input("请输入 [1/2，默认 1]：").strip() or "1"
    return "category", "notebook" if category == "2" else "phone"


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="运行哔哩哔哩爬虫。")
    parser.add_argument("keyword", nargs="?", help="直接指定型号关键词。")
    parser.add_argument("--category", choices=["phone", "notebook"], help="按 ZOL 类型定向抓取。")
    parser.add_argument("--limit", type=int, default=150, help="抓取视频条数，默认 150。")
    parser.add_argument("--limit-per-model", type=int, default=3, help="每个 ZOL 型号最多抓取视频数。")
    parser.add_argument("--model-limit", type=int, default=80, help="每个 ZOL 类型最多读取型号数。")
    parser.add_argument("--comments-per-video", type=int, default=COMMENTS_PER_VIDEO, help="每个视频最多抓取评论数。")
    parser.add_argument("--zol-db", default=str(DEFAULT_ZOL_DB), help="ZOL 型号库路径。")
    args = parser.parse_args()

    try:
        from storage.db import configure_logging

        configure_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    if args.keyword:
        mode, target = "model", args.keyword
    elif args.category:
        mode, target = "category", args.category
    else:
        mode, target = _choose_interactively()

    saved_count = 0

    def save_and_print(row: dict[str, Any], index: int) -> None:
        nonlocal saved_count
        try:
            _save_rows([row])
            saved_count += 1
            print(f"抓取第{index}条数据，抓取成功", flush=True)
        except Exception:
            print(f"抓取第{index}条数据，抓取失败", flush=True)

    rows: list[dict[str, Any]] = []
    try:
        if mode == "category":
            rows = crawl_from_zol_models(
                zol_db_path=args.zol_db,
                categories=target,
                total_limit=args.limit,
                limit_per_model=args.limit_per_model,
                model_limit=args.model_limit,
                save=False,
                comments_per_video=args.comments_per_video,
                row_callback=save_and_print,
            )
        else:
            rows = crawl(
                keyword=target,
                limit=args.limit,
                allow_live=True,
                fetch_comments=True,
                comments_per_video=args.comments_per_video,
                row_callback=save_and_print,
            )
    except Exception:
        pass

    if len(rows) < args.limit:
        print(f"没有更多符合条件的数据，本次共抓取{saved_count}条。", flush=True)


def _save_rows(rows: list[dict[str, Any]]) -> int:
    _ensure_project_root_on_path()
    from storage.db import init_database, insert_products

    init_database(seed=False)
    return insert_products(rows)


def _clean_title(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return _clean_text(html.unescape(text))


def _clean_comment(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\[[^\]]{1,20}\]", "", text)
    return _clean_text(text)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            rows.append(cleaned)
    return rows


def _sleep_jitter() -> None:
    low, high = REQUEST_DELAY_RANGE
    time.sleep(random.uniform(low, high))


def _ensure_project_root_on_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)


if __name__ == "__main__":
    main()
