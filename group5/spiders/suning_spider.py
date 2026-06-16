"""Suning live crawler.

Reads Suning's public search page, requests the public price endpoint, and
extracts evaluation counts plus public review body text from the review endpoint.

Improvements over the original:
- User-Agent rotation via fake-useragent (with graceful fallback to a fixed pool).
- Configurable request delay to avoid burst patterns.
- Retry with exponential backoff for transient HTTP failures.
- Price URL falls back across multiple known templates if the primary fails.
- Step-level logging into logs/spider.log so failures are diagnosable.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

def _make_ua_provider() -> Callable[[], str]:
    try:
        from fake_useragent import UserAgent

        ua = UserAgent(fallback=UA_POOL[0])
        return lambda: ua.random
    except Exception:
        return lambda: random.choice(UA_POOL)


_UA_PROVIDER = _make_ua_provider()


REQUEST_DELAY_RANGE = (
    float(os.getenv("SUNING_DELAY_MIN", "0.5")),
    float(os.getenv("SUNING_DELAY_MAX", "1.5")),
)
MAX_RETRIES = int(os.getenv("SUNING_MAX_RETRIES", "3"))


def _build_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": _UA_PROVIDER(),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _sleep_jitter() -> None:
    low, high = REQUEST_DELAY_RANGE
    time.sleep(random.uniform(low, high))


def _request_with_retry(
    method: str, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout)
            response.raise_for_status()
            logger.debug("%s %s -> %s", method, url, response.status_code)
            return response
        except requests.RequestException as exc:
            last_exc = exc
            backoff = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            logger.warning(
                "Retry %d/%d for %s after error: %s (sleeping %.2fs)",
                attempt,
                MAX_RETRIES,
                url,
                exc,
                backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"请求重试 {MAX_RETRIES} 次后仍失败: {url} ({last_exc})")


def crawl(keyword: str, limit: int = 30, allow_live: bool = False) -> list[dict]:
    if not allow_live:
        raise RuntimeError("苏宁爬虫已切换为真实数据模式，请使用 mode=live。")

    logger.info("Suning crawl start: keyword=%r limit=%d", keyword, limit)
    url = f"https://search.suning.com/{quote(keyword)}/"
    response = _request_with_retry("GET", url, headers=_build_headers(referer="https://www.suning.com/"))
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "lxml")

    rows = []
    for node in soup.select("li[doctype]"):
        product = _parse_product_node(node, keyword)
        if not product:
            continue
        _sleep_jitter()
        try:
            price = _fetch_price(product)
        except Exception as exc:
            logger.info("Skip pid=%s: price fetch failed (%s)", product["_product_id"], exc)
            continue
        if price is None:
            continue

        _sleep_jitter()
        try:
            comment_text = _fetch_comment_text(product)
        except Exception as exc:
            logger.info("Skip pid=%s: comment fetch failed (%s)", product["_product_id"], exc)
            continue
        if not comment_text:
            continue

        product["price"] = price
        product["comment_text"] = comment_text
        rows.append(_public_product_fields(product))
        if len(rows) >= limit:
            break

    if not rows:
        raise RuntimeError(
            "苏宁公开页面可访问，但本次未解析到同时包含真实价格、商家、评论数量、评论正文的商品。"
        )
    logger.info("Suning crawl ok: %d rows", len(rows))
    return rows


def _parse_product_node(node, keyword: str):
    hidden = node.select_one(".hidenInfo")
    price_node = node.select_one(".def-price")
    title_node = node.select_one(".title-selling-point a")
    store_node = node.select_one(".store-stock")
    evaluate_node = node.select_one(".info-evaluate")
    if not hidden or not title_node or not store_node or not evaluate_node:
        return None

    datapro = hidden.get("datapro", "")
    product_id_match = re.search(r"\|\|([0-9]+)\|\|", datapro)
    product_id = product_id_match.group(1) if product_id_match else None
    vendor = hidden.get("vendor") or "0000000000"
    mdm_group_id = price_node.get("mdmgroupid", "") if price_node else ""
    if not product_id:
        return None

    title = _clean_text(title_node.get_text(" ", strip=True))
    if not _looks_like_digital_product(title):
        return None
    merchant = _clean_text(store_node.get_text(" ", strip=True))
    evaluate_text = _clean_text(evaluate_node.get_text(" ", strip=True))
    comment_count = _parse_count(evaluate_text)
    if comment_count <= 0:
        return None

    return {
        "source": "suning",
        "keyword": keyword,
        "title": title,
        "price": None,
        "merchant": merchant,
        "rating_tags": "",
        "comment_text": "",
        "comment_count": comment_count,
        "rank": None,
        "crawled_at": datetime.now().replace(microsecond=0).isoformat(),
        "_product_id": product_id,
        "_vendor": vendor,
        "_mdm_group_id": mdm_group_id,
    }


def _fetch_price(product: dict) -> float | None:
    """Try several known Suning price endpoints; return first numeric hit."""
    product_id = product["_product_id"]
    product_id_18 = product_id.zfill(18)
    vendor = product["_vendor"]
    mdm_group_id = product["_mdm_group_id"]
    referer = f"https://product.suning.com/{vendor}/{product_id}.html"
    timestamp_ms = int(time.time() * 1000)

    urls = [
        # Original full template, kept first (works most often when params match).
        (
            "https://pas.suning.com/"
            f"nspcsale_0_{product_id_18}_{product_id_18}_{vendor}_"
            f"10_010_0100101_20089_1000000_9017_10106_Z001___"
            f"{mdm_group_id}_0.13_0___000278188__.html"
            f"?callback=pcData&_={timestamp_ms}"
        ),
        # Simpler price endpoint that doesn't need the mdm group id.
        (
            f"https://pas.suning.com/nspcsale_0_{product_id_18}_{product_id_18}_{vendor}_"
            "10_010_0100101_20089_1000000_9017_10106_Z001___R9000900_0.0_0___1__.html"
            f"?callback=pcData&_={timestamp_ms}"
        ),
        # Fallback: product page itself sometimes embeds price in JSON-LD.
        f"https://product.suning.com/{vendor}/{product_id}.html",
    ]

    for url in urls:
        try:
            response = _request_with_retry(
                "GET", url, headers=_build_headers(referer=referer), timeout=15
            )
            price = _extract_price_from_response(response, url)
            if price is not None:
                return price
        except Exception as exc:
            logger.debug("Price endpoint failed (%s): %s", url[:80], exc)
            continue
    return None


def _extract_price_from_response(response: requests.Response, url: str) -> float | None:
    body = response.text
    if "pcData(" in body:
        try:
            data = json.loads(_strip_jsonp(body, "pcData"))
        except json.JSONDecodeError:
            return None
        sale_info = data.get("data", {}).get("price", {}).get("saleInfo", [])
        if not sale_info:
            return None
        sale = sale_info[0]
        for key in (
            "promotionPrice",
            "netPrice",
            "pgPrice",
            "proPrice",
            "bigPromoPrice",
            "singlePrice",
            "refPrice",
        ):
            price = _parse_price(sale.get(key))
            if price is not None:
                return price
        return None

    # Product detail HTML fallback.
    match = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([\d.]+)', body)
    if match:
        return float(match.group(1))
    match = re.search(r'"price"\s*:\s*"?([\d.]+)', body)
    if match:
        return float(match.group(1))
    return None


def _fetch_comment_text(product: dict) -> str:
    product_id = product["_product_id"].zfill(18)
    vendor = product["_vendor"]
    referer = f"https://product.suning.com/{vendor}/{product_id}.html"
    url = (
        "https://review.suning.com/ajax/cluster_review_lists/"
        f"general-{vendor}-{product_id}-0000000000-total-1-default-10-----reviewList.htm"
        "?callback=reviewList"
    )
    response = _request_with_retry(
        "GET", url, headers=_build_headers(referer=referer), timeout=15
    )
    data = json.loads(_strip_jsonp(response.text, "reviewList"))

    comments: list[str] = []
    for review in data.get("commodityReviews", []):
        text = _extract_suning_review_text(review)
        if text:
            comments.append(text)

    return " || ".join(comments[: int(os.getenv("SUNING_COMMENT_LIMIT", "5"))])


def _extract_suning_review_text(review: dict) -> str:
    for key in (
        "content",
        "commodityReviewContent",
        "reviewContent",
        "qualityStarContent",
        "againReviewContent",
    ):
        text = _clean_comment_text(review.get(key))
        if text:
            return text
    return ""


def _public_product_fields(product: dict) -> dict:
    return {
        key: product[key]
        for key in (
            "source",
            "keyword",
            "title",
            "price",
            "merchant",
            "rating_tags",
            "comment_text",
            "comment_count",
            "rank",
            "crawled_at",
        )
    } | {"product_id": product.get("_product_id")}


def _strip_jsonp(text: str, callback: str) -> str:
    body = text.strip()
    prefix = f"{callback}("
    if body.startswith(prefix):
        body = body[len(prefix) : body.rfind(")")]
    return body


def _parse_price(value) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _parse_count(value: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)(万)?", value.replace(",", ""))
    if not match:
        return 0
    count = float(match.group(1))
    if match.group(2):
        count *= 10000
    return int(count)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _clean_comment_text(value) -> str:
    text = _clean_text(str(value or ""))
    if not text:
        return ""
    blocked = ("此用户没有填写评价", "此用户未填写评价内容", "默认好评")
    if any(marker in text for marker in blocked):
        return ""
    return text


def _looks_like_digital_product(title: str) -> bool:
    lowered = title.lower()
    excluded = (
        "图书",
        "书籍",
        "开发入门",
        "开发实战",
        "编程",
        "ios经典应用",
        "简史",
        "摄影指南",
        "故障排除",
        "维修实战",
        "9787",
        "[n]",
        "[m]",
    )
    if any(word in lowered for word in excluded):
        return False
    included = (
        "iphone",
        "苹果",
        "手机",
        "数据线",
        "充电",
        "type-c",
        "华为",
        "小米",
        "荣耀",
        "oppo",
        "vivo",
    )
    return any(word in lowered for word in included)


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="Run the Suning crawler once and print JSON rows.")
    parser.add_argument("keyword", nargs="?", default="手机", help="Search keyword.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum rows to print.")
    parser.add_argument("--save", action="store_true", help="Save crawled rows to SQLite.")
    args = parser.parse_args()

    try:
        from storage.db import configure_logging

        configure_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    rows = crawl(keyword=args.keyword, limit=args.limit, allow_live=True)
    if args.save:
        inserted = _save_rows(rows)
        logger.info("Saved %d Suning rows to SQLite", inserted)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def _save_rows(rows: list[dict]) -> int:
    _ensure_project_root_on_path()
    from storage.db import init_database, insert_products

    init_database(seed=False)
    return insert_products(rows)


def _ensure_project_root_on_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)


if __name__ == "__main__":
    main()
