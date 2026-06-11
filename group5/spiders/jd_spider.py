"""JD live crawler via ScrapingBee rendering proxy.

JD's search page blocks direct requests/playwright access from most non-residential
IPs and requires login cookies. We delegate page rendering to ScrapingBee (or any
compatible rendering API), then parse the returned HTML locally for price,
merchant, comment count and review tags.

Required env vars:
  SCRAPINGBEE_API_KEY   ScrapingBee account key
Optional:
  SCRAPINGBEE_ENDPOINT  Override the API URL (default: https://app.scrapingbee.com/api/v1/)
  JD_RENDER_JS          "true"/"false" (default true) - render JS for full search results
  JD_PREMIUM_PROXY      "true"/"false" (default true) - use premium proxy to bypass JD

Output rows match the suning_spider schema:
  source, keyword, title, price, merchant, rating_tags, comment_count, rank, crawled_at
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

SCRAPINGBEE_ENDPOINT = os.getenv(
    "SCRAPINGBEE_ENDPOINT", "https://app.scrapingbee.com/api/v1/"
)
DEFAULT_TIMEOUT = 60


def crawl(keyword: str, limit: int = 30, allow_live: bool = False) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("京东爬虫已切换为真实数据模式，请使用 mode=live。")

    api_key = os.getenv("SCRAPINGBEE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "京东真实数据需要 ScrapingBee 渲染服务。请配置 SCRAPINGBEE_API_KEY 环境变量。"
            "免费额度可在 https://app.scrapingbee.com/account/register 注册获取。"
        )

    html = _fetch_jd_search_html(api_key, keyword)
    rows = _parse_jd_search_html(html, keyword, limit)

    if not rows:
        raise RuntimeError(
            "ScrapingBee 已返回京东搜索页，但未解析出价格/商家/评论数齐全的商品。"
            "可能是页面结构变化或返回了风控页。"
        )

    # JD's product detail comment summary lives at club.jd.com — try to enrich tags.
    enriched_rows = []
    for row in rows:
        try:
            tags = _fetch_review_tags(api_key, row["_sku"])
            if tags:
                row["rating_tags"] = tags
        except Exception as exc:
            logger.warning("Failed to fetch review tags for sku=%s: %s", row["_sku"], exc)
        row.pop("_sku", None)
        enriched_rows.append(row)
        time.sleep(0.3)

    return enriched_rows


def _fetch_jd_search_html(api_key: str, keyword: str) -> str:
    target = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
    params = {
        "api_key": api_key,
        "url": target,
        "render_js": os.getenv("JD_RENDER_JS", "true"),
        "premium_proxy": os.getenv("JD_PREMIUM_PROXY", "true"),
        "country_code": "cn",
        "wait_for": "li.gl-item",
        "wait_browser": "domcontentloaded",
    }

    logger.info("ScrapingBee fetching JD search for keyword=%r", keyword)
    response = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=DEFAULT_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(
            f"ScrapingBee 调用京东失败：HTTP {response.status_code}。"
            f"响应：{response.text[:200]}"
        )
    return response.text


def _fetch_review_tags(api_key: str, sku: str) -> str:
    """Fetch JD comment summary (good_rate, tag list) via club.jd.com.

    JD's club.jd.com/comment/productCommentSummaries.action is JSONP and usually
    works without login. We still go through ScrapingBee to dodge IP blocks.
    """
    target = (
        "https://club.jd.com/comment/productCommentSummaries.action"
        f"?referenceIds={sku}"
    )
    params = {
        "api_key": api_key,
        "url": target,
        "render_js": "false",
        "premium_proxy": "true",
        "country_code": "cn",
    }
    response = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=DEFAULT_TIMEOUT)
    if response.status_code != 200:
        return ""
    body = response.text
    # Extract JSON from JSONP-ish or plain JSON body
    match = re.search(r"\{.*\}", body, re.S)
    if not match:
        return ""
    import json

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""

    summaries = data.get("CommentsCount") or []
    if not summaries:
        return ""
    first = summaries[0]
    # JD does not always return tag arrays in this endpoint; fall back to score keys.
    tags: list[str] = []
    good_rate = first.get("GoodRateShow")
    if good_rate is not None:
        tags.append(f"好评率{good_rate}%")
    after_count = first.get("AfterCount")
    if after_count:
        tags.append(f"追评{_format_count(after_count)}")
    poor_count = first.get("PoorCountStr") or first.get("PoorCount")
    if poor_count:
        tags.append(f"差评{poor_count}")
    return "、".join(tags)


def _parse_jd_search_html(html: str, keyword: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []

    for idx, item in enumerate(soup.select("li.gl-item")):
        sku = item.get("data-sku") or item.get("data-pid")
        if not sku:
            continue

        title = _text(item.select_one(".p-name em")) or _text(item.select_one(".p-name"))
        price = _parse_price(_text(item.select_one(".p-price i")) or _text(item.select_one(".p-price")))
        merchant = _text(item.select_one(".p-shop a")) or _text(item.select_one(".p-shopnum a"))
        comment_count = _parse_count(_text(item.select_one(".p-commit strong a")) or _text(item.select_one(".p-commit")))

        # Fallback when shop is not rendered: JD sometimes hides it for self-operated items.
        if not merchant:
            merchant = "京东自营" if "京东自营" in str(item) or "自营" in (title or "") else ""

        if not title or price is None or not merchant or comment_count == 0:
            continue

        rows.append(
            {
                "source": "jd",
                "product_id": sku,
                "keyword": keyword,
                "title": title,
                "price": price,
                "merchant": merchant,
                "rating_tags": "",
                "comment_count": comment_count,
                "rank": idx + 1,
                "crawled_at": datetime.now().replace(microsecond=0).isoformat(),
                "_sku": sku,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _parse_price(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _parse_count(value: str) -> int:
    text = str(value).replace(",", "").replace("+", "")
    match = re.search(r"(\d+(?:\.\d+)?)(万)?", text)
    if not match:
        return 0
    count = float(match.group(1))
    if match.group(2):
        count *= 10000
    return int(count)


def _format_count(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    if n >= 10000:
        return f"{n/10000:.1f}万"
    return str(n)
