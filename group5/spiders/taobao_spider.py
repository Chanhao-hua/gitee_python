"""Taobao live crawler using a visible Playwright profile.

Taobao search/detail pages are heavily login and risk-control gated. This
crawler does not bypass captchas or signatures; it reuses a local browser
profile and reads data already rendered in the page.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin


logger = logging.getLogger(__name__)
DEFAULT_PROFILE_DIR = Path("data") / "_taobao_profile"
DEFAULT_TIMEOUT = 60_000


def crawl(keyword: str, limit: int = 30, allow_live: bool = False) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("Taobao crawler is live-only. Please use mode=live.")
    return _crawl_with_browser(keyword=keyword, limit=limit)


def _crawl_with_browser(keyword: str, limit: int) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Taobao crawler requires playwright.") from exc

    profile_dir = Path(os.getenv("TAOBAO_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)))
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = os.getenv("TAOBAO_HEADLESS", "false").lower() == "true"
    wait_seconds = int(os.getenv("TAOBAO_WAIT_SECONDS", "45"))

    search_url = f"https://s.taobao.com/search?q={quote(keyword)}"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="zh-CN",
            viewport={"width": 1366, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            page.wait_for_timeout(8_000)

            rows = _parse_search_page(page, keyword, limit)
            if not rows and not headless and wait_seconds > 0:
                print(
                    "Taobao search did not render product rows. If a login or verification "
                    f"page is visible, finish it now. Waiting {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                page.reload(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
                page.wait_for_timeout(8_000)
                rows = _parse_search_page(page, keyword, limit)

            if not rows:
                dump_path = Path("data") / "_taobao_last_search.html"
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                dump_path.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "Taobao page loaded, but no product rows were parsed. "
                    f"Saved HTML snapshot to {dump_path}. Login or risk verification may be required."
                )

            if os.getenv("TAOBAO_FETCH_COMMENTS", "false").lower() == "true":
                for row in rows:
                    row["comment_text"] = _fetch_comments_from_detail(page, row["product_id"])
                    _delay()
            return rows
        finally:
            context.close()


def _parse_search_page(page: Any, keyword: str, limit: int) -> list[dict[str, Any]]:
    rows = _parse_from_json_blobs(page.content(), keyword, limit)
    if rows:
        return rows
    return _parse_from_dom(page, keyword, limit)


def _parse_from_json_blobs(html: str, keyword: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    decoder = json.JSONDecoder()
    for key in ("auctions", "itemsArray", "itemlist", "mods"):
        for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*', html):
            start = match.end()
            try:
                value, _ = decoder.raw_decode(html[start:])
            except json.JSONDecodeError:
                continue
            for item in _walk_dicts(value):
                row = _row_from_taobao_item(item, keyword)
                if row and row["product_id"] not in seen:
                    seen.add(row["product_id"])
                    rows.append(row)
                    if len(rows) >= limit:
                        return rows
    return rows


def _parse_from_dom(page: Any, keyword: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cards = page.locator(
        "a[href*='item.taobao.com/item.htm'], a[href*='detail.tmall.com/item.htm'], "
        "a[href*='item.htm?id=']"
    )
    for index in range(cards.count()):
        card = cards.nth(index)
        href = card.get_attribute("href") or ""
        product_id = _extract_item_id(href)
        if not product_id or product_id in seen:
            continue
        text = _clean_text(card.inner_text(timeout=1000))
        if not text:
            continue
        rows.append(
            {
                "source": "taobao",
                "product_id": product_id,
                "keyword": keyword,
                "title": text.split("¥", 1)[0][:160],
                "price": _parse_price(text),
                "merchant": "",
                "rating_tags": "",
                "comment_text": "",
                "comment_count": _parse_count(text),
                "rank": len(rows) + 1,
                "crawled_at": _now(),
            }
        )
        seen.add(product_id)
        if len(rows) >= limit:
            break
    return rows


def _fetch_comments_from_detail(page: Any, product_id: str) -> str:
    detail_url = f"https://item.taobao.com/item.htm?id={product_id}"
    page.goto(detail_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    page.wait_for_timeout(8_000)
    for selector in (
        "[class*='rate'] [class*='content']",
        "[class*='comment'] [class*='content']",
        "[class*='Review'] [class*='content']",
        "[class*='evaluate']",
    ):
        comments = [_clean_comment_text(text) for text in page.locator(selector).all_inner_texts()]
        comments = [text for text in comments if text]
        if comments:
            return " || ".join(comments[:5])
    return ""


def _row_from_taobao_item(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    product_id = _first_text(item, "nid", "item_id", "itemId", "auctionId", "id")
    title = _first_text(item, "raw_title", "title", "short_title", "name")
    if not product_id or not title:
        return None
    price = _parse_price(_first_text(item, "view_price", "price", "priceWap", "salePrice"))
    merchant = _first_text(item, "nick", "shopName", "sellerNick", "user_nick")
    sales_text = _first_text(item, "view_sales", "realSales", "sold", "sales")
    return {
        "source": "taobao",
        "product_id": product_id,
        "keyword": keyword,
        "title": _clean_text(title),
        "price": price,
        "merchant": _clean_text(merchant),
        "rating_tags": "",
        "comment_text": "",
        "comment_count": _parse_count(sales_text),
        "rank": None,
        "crawled_at": _now(),
    }


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _extract_item_id(value: str) -> str:
    match = re.search(r"[?&]id=(\d+)", value)
    return match.group(1) if match else ""


def _parse_price(value: Any) -> float | None:
    text = str(value or "").replace(",", "")
    price_match = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
    if price_match:
        return float(price_match.group(1))
    if re.search(r"(price|价)", text, flags=re.I):
        match = re.search(r"\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None
    return None


def _parse_count(value: Any) -> int:
    text = str(value or "").replace(",", "").replace("+", "")
    match = re.search(r"(\d+(?:\.\d+)?)(万|千)?", text)
    if not match:
        return 0
    count = float(match.group(1))
    if match.group(2) == "万":
        count *= 10000
    elif match.group(2) == "千":
        count *= 1000
    return int(count)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_comment_text(value: Any) -> str:
    text = _clean_text(value)
    blocked = ("此用户没有填写评价", "默认好评", "系统默认")
    if not text or any(word in text for word in blocked):
        return ""
    return text


def _delay() -> None:
    low = float(os.getenv("TAOBAO_DELAY_MIN_SECONDS", "8"))
    high = float(os.getenv("TAOBAO_DELAY_MAX_SECONDS", "20"))
    time.sleep(max(low, min(high, low if high < low else __import__("random").uniform(low, high))))


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="Run the Taobao crawler once and print JSON rows.")
    parser.add_argument("keyword", nargs="?", default="手机", help="Search keyword.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum rows to print.")
    parser.add_argument("--save", action="store_true", help="Save crawled rows to SQLite.")
    parser.add_argument("--comments", action="store_true", help="Try to fetch rendered detail comments.")
    args = parser.parse_args()

    if args.comments:
        os.environ["TAOBAO_FETCH_COMMENTS"] = "true"
    rows = crawl(keyword=args.keyword, limit=args.limit, allow_live=True)
    if args.save:
        from storage.db import init_database, insert_products

        init_database(seed=False)
        insert_products(rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def _ensure_project_root_on_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)


if __name__ == "__main__":
    main()
