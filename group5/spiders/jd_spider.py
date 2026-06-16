"""JD live crawler.

Preferred path:
  Use a user-provided JD_COOKIE plus browser-like headers to fetch public JD
  search pages with requests. The cookie must come from the user's own browser
  session.

Local browser path:
  Use JD_FETCH_METHOD=profile, or pass --method profile, to reuse a local
  Playwright browser profile without printing or copying cookies.

Fallback path:
  If JD_COOKIE is not configured, delegate page rendering to ScrapingBee (or any
  compatible rendering API), then parse the returned HTML locally.

Required env vars:
  JD_COOKIE             Cookie copied from the user's own logged-in browser
or:
  SCRAPINGBEE_API_KEY   ScrapingBee account key used only when JD_COOKIE is absent

Optional:
  JD_FETCH_METHOD       "auto", "profile", "cookie", or "cloud"
  JD_PROFILE_DIR        Playwright user-data directory (default data/_pw_profile)
  JD_USER_AGENT         Override the randomly selected desktop browser UA
  SCRAPINGBEE_ENDPOINT  Override the API URL
  JD_RENDER_JS          "true"/"false" (default true)
  JD_PREMIUM_PROXY      "true"/"false" (default true)

Output rows match the suning_spider schema:
  source, keyword, title, price, merchant, rating_tags, comment_count, rank, crawled_at
"""

from __future__ import annotations

import argparse
import getpass
import json
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
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

SCRAPINGBEE_ENDPOINT = os.getenv(
    "SCRAPINGBEE_ENDPOINT", "https://app.scrapingbee.com/api/v1/"
)
DEFAULT_TIMEOUT = 60
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _jd_delay(
    min_env: str = "JD_DELAY_MIN_SECONDS",
    max_env: str = "JD_DELAY_MAX_SECONDS",
    *,
    default_min: float = 10.0,
    default_max: float = 30.0,
) -> None:
    if not _env_bool("JD_SLOW_MODE", False):
        time.sleep(float(os.getenv("JD_DELAY_SECONDS", "0.3")))
        return
    low = float(os.getenv(min_env, str(default_min)))
    high = float(os.getenv(max_env, str(default_max)))
    if high < low:
        high = low
    delay = random.uniform(low, high)
    logger.info("JD slow-mode delay %.1fs", delay)
    time.sleep(delay)


def crawl(keyword: str, limit: int = 30, allow_live: bool = False) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("JD crawler is live-only. Please use mode=live.")

    method = os.getenv("JD_FETCH_METHOD", "auto").strip().lower() or "auto"
    if method == "profile":
        return _crawl_with_browser_profile(keyword, limit)

    cookie_header = _cookie_header_from_env()
    api_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()

    if method in ("auto", "cookie") and cookie_header:
        html = _fetch_jd_search_html_with_cookie(keyword, cookie_header)
        fetch_comments: Callable[[str], str] = lambda sku: _fetch_comments_with_cookie(
            sku, cookie_header
        )
    elif method in ("auto", "cloud"):
        if not api_key:
            raise RuntimeError(
                "JD live crawl requires JD_COOKIE, JD_FETCH_METHOD=profile, "
                "or SCRAPINGBEE_API_KEY as fallback."
            )
        html = _fetch_jd_search_html_via_scrapingbee(api_key, keyword)
        fetch_comments = lambda sku: _fetch_comments_via_scrapingbee(api_key, sku)
    else:
        raise RuntimeError(
            f"JD fetch method {method!r} is not configured. Provide JD_COOKIE for "
            "method=cookie, SCRAPINGBEE_API_KEY for method=cloud, or use method=profile."
        )

    rows = _parse_jd_search_html(html, keyword, limit)
    if not rows:
        raise RuntimeError(
            "JD returned a page, but no complete product rows were parsed. "
            "The cookie may be expired, JD may have returned a login/risk page, "
            "or the page structure may have changed."
        )

    enriched_rows = []
    for row in rows:
        try:
            comments = fetch_comments(row["_sku"])
            if comments:
                row["comment_text"] = comments
        except Exception as exc:
            logger.warning("Failed to fetch comments for sku=%s: %s", row["_sku"], exc)
        row.pop("_sku", None)
        enriched_rows.append(row)
        _jd_delay()

    return enriched_rows


def _crawl_with_browser_profile(keyword: str, limit: int) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "JD profile mode requires playwright. Install dependencies and run "
            "`python -m playwright install chromium` if needed."
        ) from exc

    profile_dir = Path(os.getenv("JD_PROFILE_DIR", str(Path("data") / "_pw_profile")))
    if not profile_dir.exists():
        raise RuntimeError(f"JD profile directory does not exist: {profile_dir}")

    target = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
    logger.info("Fetching JD search with local browser profile for keyword=%r", keyword)

    with sync_playwright() as playwright:
        headless = os.getenv("JD_HEADLESS", "true").lower() != "false"
        if _env_bool("JD_SLOW_MODE", False) and "JD_HEADLESS" not in os.environ:
            headless = False
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
            user_agent=os.getenv("JD_USER_AGENT") or UA_POOL[0],
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _jd_delay(
                "JD_PAGE_DELAY_MIN_SECONDS",
                "JD_PAGE_DELAY_MAX_SECONDS",
                default_min=3.0,
                default_max=8.0,
            )
            page.goto(target, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT * 1000)
            try:
                page.wait_for_selector("li.gl-item", timeout=15000)
            except PlaywrightTimeoutError:
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

            html = page.content()
            if _looks_like_login_or_risk_page(page.url, html):
                if not _can_wait_for_profile_login(headless):
                    raise RuntimeError(
                        "JD returned a login/risk page in profile mode. Open the same "
                        "profile once, finish JD login/verification, then rerun."
                    )
                _wait_for_manual_profile_login(page, target)
                html = page.content()

            rows = _parse_jd_search_html(html, keyword, limit)
            if not rows:
                dump_path = Path("data") / "_jd_profile_last_search.html"
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                dump_path.write_text(html, encoding="utf-8")
                raise RuntimeError(
                    "JD profile mode loaded a page, but no complete product rows were "
                    f"parsed. Saved HTML snapshot to {dump_path}."
                )

            enriched_rows = []
            for row in rows:
                try:
                    comments = _fetch_comments_with_profile_page(page, row["_sku"])
                    if comments:
                        row["comment_text"] = comments
                except Exception as exc:
                    logger.warning("Failed to fetch comments for sku=%s: %s", row["_sku"], exc)
                row.pop("_sku", None)
                enriched_rows.append(row)
                _jd_delay()
            return enriched_rows
        finally:
            context.close()


def _fetch_comments_with_profile_page(page: Any, sku: str) -> str:
    target = "https://club.jd.com/comment/productPageComments.action"
    response = page.context.request.get(
        target,
        params=_jd_comment_params(sku),
        headers={
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "Referer": f"https://item.jd.com/{sku}.html",
            "User-Agent": os.getenv("JD_USER_AGENT") or UA_POOL[0],
        },
        timeout=DEFAULT_TIMEOUT * 1000,
    )
    if not response.ok:
        return ""
    return _parse_jd_comments(response.text())


def _can_wait_for_profile_login(headless: bool) -> bool:
    return (
        not headless
        and os.getenv("JD_PROFILE_WAIT_LOGIN", "false").lower() == "true"
    )


def _wait_for_manual_profile_login(page: Any, target: str) -> None:
    wait_seconds = int(os.getenv("JD_PROFILE_WAIT_SECONDS", "90"))
    print(f"JD opened a login/risk page. Waiting {wait_seconds}s for manual verification...")
    time.sleep(wait_seconds)
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT * 1000)
    except Exception as exc:
        if "interrupted by another navigation" not in str(exc):
            raise
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    html = page.content()
    if _looks_like_login_or_risk_page(page.url, html):
        raise RuntimeError("JD still shows a login/risk page after manual verification.")


def _cookie_header_from_env() -> str:
    return os.getenv("JD_COOKIE", "").strip()


def _build_jd_headers(referer: str | None = None, *, accept_json: bool = False) -> dict[str, str]:
    user_agent = os.getenv("JD_USER_AGENT") or random.choice(UA_POOL)
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
    }
    if accept_json:
        headers["Accept"] = "application/json,text/javascript,*/*;q=0.8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    else:
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    if referer:
        headers["Referer"] = referer
    return headers


def _fetch_jd_search_html_with_cookie(keyword: str, cookie_header: str) -> str:
    target = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
    headers = _build_jd_headers(referer="https://www.jd.com/")
    headers["Cookie"] = cookie_header

    logger.info("Fetching JD search with user cookie for keyword=%r", keyword)
    response = requests.get(
        target,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        allow_redirects=True,
    )
    response.encoding = response.apparent_encoding or response.encoding
    if response.status_code != 200:
        raise RuntimeError(f"JD search request failed: HTTP {response.status_code}")
    if _looks_like_login_or_risk_page(response.url, response.text):
        raise RuntimeError(
            "JD returned a login/risk page. Refresh JD_COOKIE from a browser session "
            "that can manually search JD normally."
        )
    return response.text


def _fetch_jd_search_html_via_scrapingbee(api_key: str, keyword: str) -> str:
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
            f"ScrapingBee JD request failed: HTTP {response.status_code}. "
            f"Response: {response.text[:200]}"
        )
    return response.text


def _fetch_comments_with_cookie(sku: str, cookie_header: str) -> str:
    target = "https://club.jd.com/comment/productPageComments.action"
    headers = _build_jd_headers(
        referer=f"https://item.jd.com/{sku}.html",
        accept_json=True,
    )
    headers["Cookie"] = cookie_header
    response = requests.get(
        target,
        params=_jd_comment_params(sku),
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code != 200:
        return ""
    return _parse_jd_comments(response.text)


def _fetch_comments_via_scrapingbee(api_key: str, sku: str) -> str:
    target = (
        "https://club.jd.com/comment/productPageComments.action"
        f"?{_encode_query(_jd_comment_params(sku))}"
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
    return _parse_jd_comments(response.text)


def _jd_comment_params(sku: str) -> dict[str, str]:
    return {
        "productId": sku,
        "score": "0",
        "sortType": "5",
        "page": "0",
        "pageSize": os.getenv("JD_COMMENT_PAGE_SIZE", "10"),
        "isShadowSku": "0",
        "fold": "1",
    }


def _encode_query(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


def _parse_jd_comments(body: str) -> str:
    match = re.search(r"\{.*\}", body, re.S)
    if not match:
        return ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""

    comments: list[str] = []
    for review in data.get("comments", []) or []:
        for key in ("content", "afterUserComment", "plusAvailable"):
            value = review.get(key)
            if isinstance(value, dict):
                text = _clean_comment_text(value.get("content", ""))
            else:
                text = _clean_comment_text(value)
            if text:
                comments.append(text)
                break
    return " || ".join(comments[: int(os.getenv("JD_COMMENT_LIMIT", "5"))])


def _parse_review_summary(body: str) -> str:
    match = re.search(r"\{.*\}", body, re.S)
    if not match:
        return ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""

    summaries = data.get("CommentsCount") or []
    if not summaries:
        return ""
    first = summaries[0]
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
        price = _parse_price(
            _text(item.select_one(".p-price i")) or _text(item.select_one(".p-price"))
        )
        merchant = _text(item.select_one(".p-shop a")) or _text(item.select_one(".p-shopnum a"))
        comment_count = _parse_count(
            _text(item.select_one(".p-commit strong a")) or _text(item.select_one(".p-commit"))
        )

        if not merchant:
            raw_item = str(item)
            merchant = "京东自营" if "自营" in raw_item or "京东自营" in title else ""

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
                "comment_text": "",
                "comment_count": comment_count,
                "rank": idx + 1,
                "crawled_at": datetime.now().replace(microsecond=0).isoformat(),
                "_sku": sku,
            }
        )
        if len(rows) >= limit:
            break
    if rows:
        return rows

    for idx, item in enumerate(soup.select(".plugin_goodsCardWrapper[data-sku]")):
        row = _parse_jd_react_card(item, keyword, idx)
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _parse_jd_react_card(item: Any, keyword: str, idx: int) -> dict[str, Any] | None:
    sku = item.get("data-sku") or item.get("data-pid")
    if not sku:
        return None

    title_node = item.select_one('[class*="_goods_title_container"] [title]')
    title = ""
    if title_node:
        title = title_node.get("title", "").strip() or _text(title_node)
    if not title:
        image_title = item.select_one('[class*="_wrapper_o085i_"][title]')
        title = image_title.get("title", "").strip() if image_title else ""

    price_node = item.select_one('[class*="_price_"]')
    price = _parse_price(_text(price_node))

    merchant = _text(item.select_one('[class*="_name_zclqt"] [class*="_limit_zclqt"]'))
    if not merchant:
        merchant = _text(item.select_one('[class*="_name_zclqt"]'))

    comment_count = _parse_react_card_count(_text(item))
    if not merchant and ("自营" in _text(item) or "自营" in title):
        merchant = "京东自营"

    if not title or price is None or not merchant or comment_count == 0:
        return None

    return {
        "source": "jd",
        "product_id": sku,
        "keyword": keyword,
        "title": title,
        "price": price,
        "merchant": merchant,
        "rating_tags": "",
        "comment_text": "",
        "comment_count": comment_count,
        "rank": idx + 1,
        "crawled_at": datetime.now().replace(microsecond=0).isoformat(),
        "_sku": sku,
    }


def _looks_like_login_or_risk_page(url: str, html: str) -> bool:
    lowered_url = url.lower()
    if any(marker in lowered_url for marker in ("passport.jd.com", "risk_handler", "cfe.m.jd.com")):
        return True
    markers = (
        "passport.jd.com/new/login",
        "risk_handler",
        "安全验证",
        "登录京东",
        "请登录",
    )
    return any(marker in html for marker in markers)


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _clean_comment_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    blocked = ("此用户未填写评价内容", "此用户没有填写评价")
    if any(marker in text for marker in blocked):
        return ""
    return text


def _parse_price(value: str) -> float | None:
    normalized = re.sub(r"\s+", "", str(value)).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    return float(match.group(0)) if match else None


def _parse_count(value: str) -> int:
    text = str(value).replace(",", "").replace("+", "")
    match = re.search(r"(\d+(?:\.\d+)?)(万|千)?", text)
    if not match:
        return 0
    count = float(match.group(1))
    if match.group(2) == "万":
        count *= 10000
    elif match.group(2) == "千":
        count *= 1000
    return int(count)


def _parse_react_card_count(value: str) -> int:
    text = str(value).replace(",", "")
    preferred_patterns = (
        r"(\d+(?:\.\d+)?)(万|千)?\+?条评价",
        r"已售\s*(\d+(?:\.\d+)?)(万|千)?\+?",
    )
    for pattern in preferred_patterns:
        match = re.search(pattern, text)
        if match:
            suffix = match.group(2) or ""
            return _parse_count(f"{match.group(1)}{suffix}")
    return _parse_count(text)


def _format_count(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="Run the JD crawler once and print JSON rows.")
    parser.add_argument("keyword", nargs="?", help="Search keyword.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum JD rows per keyword.")
    parser.add_argument("--save", action="store_true", help="Save crawled rows to SQLite.")
    parser.add_argument(
        "--from-zol",
        action="store_true",
        help="Read model names from data/zol_goods.db and crawl JD for each one.",
    )
    parser.add_argument(
        "--zol-category",
        default="phone",
        help="ZOL category key to read when --from-zol is used.",
    )
    parser.add_argument(
        "--zol-db",
        default=str(Path("data") / "zol_goods.db"),
        help="Path to the ZOL SQLite database.",
    )
    parser.add_argument(
        "--zol-limit",
        type=int,
        default=5,
        help="Maximum ZOL model names to use when --from-zol is enabled.",
    )
    parser.add_argument(
        "--method",
        choices=("auto", "profile", "cookie", "cloud"),
        help="Fetch method. Omit this option to choose interactively.",
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="Use visible browser plus randomized JD delays to reduce frequency-control risk.",
    )
    args = parser.parse_args()

    try:
        from storage.db import configure_logging

        configure_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO)

    if args.slow:
        os.environ["JD_SLOW_MODE"] = "true"
        os.environ.setdefault("JD_PROFILE_WAIT_LOGIN", "true")
    _configure_interactive_auth(args.method)
    if args.from_zol:
        rows = crawl_from_zol_models(
            limit_per_model=args.limit,
            zol_limit=args.zol_limit,
            zol_category=args.zol_category,
            zol_db_path=args.zol_db,
            save=args.save,
        )
    else:
        rows = crawl(keyword=args.keyword or "手机", limit=args.limit, allow_live=True)
        if args.save:
            inserted = _save_rows(rows)
            logger.info("Saved %d JD rows to SQLite", inserted)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def crawl_from_zol_models(
    limit_per_model: int = 5,
    zol_limit: int = 5,
    zol_category: str = "phone",
    zol_db_path: str | Path = Path("data") / "zol_goods.db",
    save: bool = False,
) -> list[dict[str, Any]]:
    _ensure_project_root_on_path()
    from spiders.zol_spider import URL_LIST, crawl_category, read_goods_names

    models = read_goods_names(category=zol_category, limit=zol_limit, db_path=zol_db_path)
    if not models:
        matching = next((item for item in URL_LIST if item["category"] == zol_category), None)
        if not matching:
            raise RuntimeError(f"Unknown ZOL category: {zol_category}")
        crawl_category(matching["category"], matching["url"], db_path=zol_db_path)
        models = read_goods_names(category=zol_category, limit=zol_limit, db_path=zol_db_path)

    if not models:
        raise RuntimeError(f"No ZOL model names found in {zol_db_path} for category={zol_category}")

    all_rows: list[dict[str, Any]] = []
    inserted_total = 0
    for index, model in enumerate(models, start=1):
        logger.info("Crawling JD for ZOL model %d/%d: %s", index, len(models), model)
        rows = crawl(keyword=model, limit=limit_per_model, allow_live=True)
        all_rows.extend(rows)
        if save:
            inserted_total += _save_rows(rows)
        _jd_delay(
            "JD_MODEL_DELAY_MIN_SECONDS",
            "JD_MODEL_DELAY_MAX_SECONDS",
            default_min=10.0,
            default_max=30.0,
        )

    if save:
        logger.info("Saved %d JD rows from %d ZOL models", inserted_total, len(models))
    return all_rows


def _save_rows(rows: list[dict[str, Any]]) -> int:
    _ensure_project_root_on_path()
    from storage.db import init_database, insert_products

    init_database(seed=False)
    return insert_products(rows)


def _ensure_project_root_on_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)


def _configure_interactive_auth(method: str | None) -> None:
    selected = method or _prompt_fetch_method()
    if selected == "auto":
        return
    if selected == "profile":
        os.environ["JD_FETCH_METHOD"] = "profile"
        os.environ.pop("JD_COOKIE", None)
        os.environ.pop("SCRAPINGBEE_API_KEY", None)
        return
    if selected == "cookie":
        os.environ["JD_FETCH_METHOD"] = "cookie"
        cookie = getpass.getpass("Paste JD Cookie (input hidden): ").strip()
        if not cookie:
            raise SystemExit("JD Cookie is empty.")
        os.environ["JD_COOKIE"] = cookie
        os.environ.pop("SCRAPINGBEE_API_KEY", None)
        return
    if selected == "cloud":
        os.environ["JD_FETCH_METHOD"] = "cloud"
        os.environ.pop("JD_COOKIE", None)
        if not os.getenv("SCRAPINGBEE_API_KEY", "").strip():
            api_key = getpass.getpass("Paste ScrapingBee API key (input hidden): ").strip()
            if not api_key:
                raise SystemExit("ScrapingBee API key is empty.")
            os.environ["SCRAPINGBEE_API_KEY"] = api_key
        return
    raise SystemExit(f"Unknown method: {selected}")


def _prompt_fetch_method() -> str:
    print("Choose JD crawl method:")
    print("  1) profile - use local Playwright browser login state")
    print("  2) cookie  - use your browser JD Cookie for this run")
    print("  3) cloud   - use ScrapingBee cloud crawler")
    print("  4) auto    - use existing JD_COOKIE or SCRAPINGBEE_API_KEY from environment")
    choice = input("Method [1/2/3/4, default 1]: ").strip().lower()
    if choice in ("", "1", "profile", "p"):
        return "profile"
    if choice in ("2", "cookie", "c"):
        return "cookie"
    if choice in ("3", "cloud", "s", "scrapingbee"):
        return "cloud"
    if choice in ("4", "auto", "a"):
        return "auto"
    raise SystemExit(f"Unknown method choice: {choice}")


if __name__ == "__main__":
    main()
