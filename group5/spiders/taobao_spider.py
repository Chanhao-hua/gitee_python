"""Taobao crawler using Scrapling with an optional saved Taobao profile.

The crawler can reuse an existing browser profile, but it never opens a manual
login window or waits for login. It only stores product rows and comments that
Scrapling can render from the current profile state.
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
from typing import Any, Callable
from urllib.parse import quote


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "data" / "_taobao_profile"
DEFAULT_ZOL_DB = PROJECT_ROOT / "data" / "zol_goods.db"
DEFAULT_ZOL_CATEGORY = "phone"
DEFAULT_ZOL_LIMIT = 3
DEFAULT_LIMIT_PER_MODEL = 1
DEFAULT_CRAWL_LIMIT = 350
DEFAULT_MAX_PAGES = 14


def crawl(
    keyword: str,
    limit: int = 30,
    allow_live: bool = False,
    fetch_comments: bool | None = None,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("Taobao crawler is live-only. Please use mode=live.")
    return _crawl_with_scrapling(
        keyword=keyword,
        limit=limit,
        fetch_comments=fetch_comments,
        row_callback=row_callback,
    )


def crawl_from_zol_models(
    zol_db_path: str | Path = DEFAULT_ZOL_DB,
    zol_category: str = DEFAULT_ZOL_CATEGORY,
    zol_limit: int = DEFAULT_ZOL_LIMIT,
    limit_per_model: int = DEFAULT_LIMIT_PER_MODEL,
    save: bool = True,
    fetch_comments: bool = True,
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
    for index, model in enumerate(models, start=1):
        print(f"淘宝 ZOL 型号 {index}/{len(models)}：{model}")
        rows = crawl(
            keyword=model,
            limit=limit_per_model,
            allow_live=True,
            fetch_comments=fetch_comments,
        )
        all_rows.extend(rows)
        if save:
            _save_rows(rows)
        if index < len(models):
            _delay()
    return all_rows


def _crawl_with_scrapling(
    keyword: str,
    limit: int,
    fetch_comments: bool | None = None,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as exc:
        raise RuntimeError("淘宝 Scrapling 爬虫需要安装 scrapling[fetchers]。") from exc

    logging.getLogger("scrapling").setLevel(logging.WARNING)
    headless = os.getenv("TAOBAO_SCRAPLING_HEADLESS", "true").lower() == "true"
    real_chrome = os.getenv("TAOBAO_SCRAPLING_REAL_CHROME", "false").lower() == "true"
    user_data_dir = _scrapling_user_data_dir()
    wait_ms = int(os.getenv("TAOBAO_SCRAPLING_WAIT_MS", "10000"))
    max_pages = int(os.getenv("TAOBAO_SCRAPLING_MAX_PAGES", str(DEFAULT_MAX_PAGES)))
    should_fetch_comments = (
        os.getenv("TAOBAO_FETCH_COMMENTS", "true").lower() == "true"
        if fetch_comments is None
        else fetch_comments
    )

    html_pages: list[str] = []

    def collect_pages(page: Any) -> None:
        page.wait_for_timeout(wait_ms)
        for page_no in range(max_pages):
            for _ in range(8):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(400)
            html_pages.append(page.content())
            if page_no >= max_pages - 1:
                return
            next_buttons = page.locator("button.next-next")
            if next_buttons.count() <= 0:
                return
            next_button = next_buttons.last
            if not next_button.is_enabled(timeout=1000):
                return
            next_button.click(timeout=8000)
            page.wait_for_timeout(wait_ms)

    search_url = f"https://s.taobao.com/search?q={quote(keyword)}&tab=all"
    try:
        response = StealthyFetcher.fetch(
            search_url,
            headless=headless,
            real_chrome=real_chrome,
            user_data_dir=user_data_dir,
            wait=1000,
            timeout=120_000,
            network_idle=False,
            google_search=False,
            locale="zh-CN",
            page_action=collect_pages,
        )
    except Exception as exc:
        logger.warning("Scrapling 淘宝搜索请求失败：%s", exc)
        return []

    if not html_pages:
        html_pages.append(_response_body_text(response))

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for html in html_pages:
        if _looks_like_blocked_page(html):
            if not rows:
                _dump_debug_html(html, "_taobao_scrapling_blocked.html")
            break

        page_rows = _parse_products_from_html(html, keyword, limit - len(rows))
        for row in page_rows:
            product_id = row["product_id"]
            if product_id in seen:
                continue
            seen.add(product_id)
            row["rank"] = len(rows) + 1
            if should_fetch_comments:
                row["comment_text"] = _fetch_comments_with_scrapling(
                    product_id=product_id,
                    headless=headless,
                    real_chrome=real_chrome,
                    user_data_dir=user_data_dir,
                    wait_ms=wait_ms,
                )
            rows.append(row)
            if row_callback:
                row_callback(row, len(rows))
            if len(rows) >= limit:
                return rows
        if not page_rows:
            break
    return rows


def _fetch_comments_with_scrapling(
    product_id: str,
    headless: bool,
    real_chrome: bool,
    user_data_dir: str | None,
    wait_ms: int,
) -> str:
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        return ""

    detail_urls = (
        f"https://detail.tmall.com/item.htm?id={product_id}",
        f"https://item.taobao.com/item.htm?id={product_id}",
    )
    comments: list[str] = []
    seen: set[str] = set()
    for detail_url in detail_urls:
        try:
            response = StealthyFetcher.fetch(
                detail_url,
                headless=headless,
                real_chrome=real_chrome,
                user_data_dir=user_data_dir,
                wait=max(3000, wait_ms // 2),
                timeout=90_000,
                network_idle=False,
                google_search=False,
                locale="zh-CN",
                capture_xhr="mtop",
                page_action=_open_review_tab,
            )
        except Exception:
            continue

        texts = [_response_body_text(response)]
        for xhr in getattr(response, "captured_xhr", None) or []:
            texts.append(_xhr_text(xhr))

        for text in texts:
            for comment in _parse_comments_from_text(text):
                if comment not in seen:
                    seen.add(comment)
                    comments.append(comment)
                if len(comments) >= 5:
                    return " || ".join(comments)
        if not comments and _looks_like_detail_blocked(response, texts[0]):
            filename = f"_taobao_detail_comment_blocked_{product_id}.html"
            _dump_debug_html(texts[0], filename)
            logger.warning("淘宝商品 %s 详情评论页被登录/风控拦截，已保存 %s", product_id, filename)
            break
    return " || ".join(comments)


def _open_review_tab(page: Any) -> None:
    page.wait_for_timeout(8_000)
    if _page_url_looks_blocked(page.url):
        return
    for _ in range(8):
        page.mouse.wheel(0, 1300)
        page.wait_for_timeout(500)

    clicked = False
    for label in ("用户评价", "评价"):
        try:
            locator = page.get_by_text(label, exact=False)
            if locator.count() > 0:
                locator.first.click(timeout=5_000)
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        try:
            clicked = bool(
                page.evaluate(
                    """
                    () => {
                        const nodes = Array.from(document.querySelectorAll('a,button,div,span'));
                        const node = nodes.find((el) => (el.textContent || '').includes('用户评价'));
                        if (!node) return false;
                        node.scrollIntoView({block: 'center'});
                        node.click();
                        return true;
                    }
                    """
                )
            )
        except Exception:
            clicked = False

    page.wait_for_timeout(10_000 if clicked else 4_000)
    for _ in range(5):
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(500)


def _parse_products_from_html(html: str, keyword: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select("a[id^='item_id_'][href*='item.htm']"):
        product_id = re.sub(r"\D+", "", card.get("id", ""))
        if not product_id:
            product_id = _extract_item_id(card.get("href") or "")
        if not product_id or product_id in seen:
            continue

        title_node = card.select_one("[class*='title--'][title]") or card.select_one("[title]")
        title = title_node.get("title", "") if title_node else ""
        if not title:
            title = _clean_text(card.get_text(" ", strip=True))
        title = _clean_text(title)
        if not title:
            continue

        price_text = (
            _node_text(card.select_one("[class*='priceInt--']"))
            + _node_text(card.select_one("[class*='priceFloat--']"))
        )
        sales_text = _node_text(card.select_one("[class*='realSales--']"))
        rows.append(
            {
                "source": "taobao",
                "product_id": product_id,
                "keyword": keyword,
                "title": title[:160],
                "price": _parse_price(price_text),
                "merchant": _node_text(card.select_one("[class*='shopNameText--']")),
                "rating_tags": "",
                "comment_text": "",
                "comment_count": _parse_count(sales_text),
                "rank": len(rows) + 1,
                "crawled_at": _now(),
            }
        )
        seen.add(product_id)
        if len(rows) >= limit:
            break
    return rows


def _parse_comments_from_text(text: str) -> list[str]:
    comments: list[str] = []
    seen: set[str] = set()
    for raw in _extract_comment_candidates(text):
        comment = _clean_comment_text(raw)
        if _is_useful_comment(comment) and comment not in seen:
            seen.add(comment)
            comments.append(comment)
        if len(comments) >= 5:
            return comments

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return comments

    soup = BeautifulSoup(text, "lxml")
    selectors = (
        "[class*='rate'] [class*='content']",
        "[class*='comment'] [class*='content']",
        "[class*='review'] [class*='content']",
        "[class*='Review'] [class*='content']",
        "[class*='evaluate']",
        "[class*='Comment']",
    )
    for selector in selectors:
        for node in soup.select(selector):
            comment = _clean_comment_text(node.get_text(" ", strip=True))
            if _is_useful_comment(comment) and comment not in seen:
                seen.add(comment)
                comments.append(comment)
            if len(comments) >= 5:
                return comments
    return comments


def _extract_comment_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for key in ("content", "rateContent", "reviewContent", "commentContent", "feedback"):
        pattern = rf'"{key}"\s*:\s*"((?:\\.|[^"\\]){{6,400}})"'
        for match in re.finditer(pattern, text):
            raw = match.group(1)
            try:
                candidates.append(json.loads(f'"{raw}"'))
            except json.JSONDecodeError:
                candidates.append(raw)
    return candidates


def _response_body_text(response: Any) -> str:
    body = getattr(response, "body", "")
    return body.decode("utf-8", errors="ignore") if isinstance(body, (bytes, bytearray)) else str(body)


def _xhr_text(xhr: Any) -> str:
    value = ""
    if isinstance(xhr, dict):
        value = xhr.get("text") or xhr.get("body") or xhr.get("response") or ""
    else:
        value = (
            getattr(xhr, "text", None)
            or getattr(xhr, "body", None)
            or getattr(xhr, "response", None)
            or ""
        )
    return value.decode("utf-8", errors="ignore") if isinstance(value, (bytes, bytearray)) else str(value)


def _looks_like_detail_blocked(response: Any, html: str) -> bool:
    return _page_url_looks_blocked(str(getattr(response, "url", ""))) or _looks_like_blocked_page(html)


def _page_url_looks_blocked(url: str) -> bool:
    text = url.lower()
    return any(marker in text for marker in ("login.taobao.com", "login.tmall.com", "punish", "captcha"))


def _scrapling_user_data_dir() -> str | None:
    if os.getenv("TAOBAO_USE_PROFILE", "true").lower() == "false":
        return None
    profile_dir = Path(os.getenv("TAOBAO_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)))
    if not profile_dir.exists():
        return None
    return str(profile_dir)


def _looks_like_blocked_page(html: str) -> bool:
    if _parse_products_from_html(html, "检测", 1):
        return False
    markers = (
        "J_MIDDLEWARE_FRAME_WIDGET",
        "_____tmd_____",
        "FAIL_SYS_SESSION_EXPIRED",
        "login.taobao.com/member/login",
        "安全验证",
        "验证码",
        "亲，请登录",
    )
    return any(marker in html for marker in markers)


def _is_useful_comment(text: str) -> bool:
    if len(text) < 6 or len(text) > 240:
        return False
    blocked = (
        "登录",
        "验证码",
        "安全验证",
        "注册协议",
        "register.agreement",
        "不信任当前设备",
        "账号管理页",
        "保持后即可直接访问",
        "请点击",
        "购物车",
        "立即购买",
        "收藏",
        "客服",
        "http",
        "function",
        "{",
        "}",
    )
    return not any(word in text for word in blocked)


def _node_text(node: Any) -> str:
    return _clean_text(node.get_text(" ", strip=True)) if node else ""


def _extract_item_id(value: str) -> str:
    match = re.search(r"[?&]id=(\d+)", value)
    return match.group(1) if match else ""


def _parse_price(value: Any) -> float | None:
    text = str(value or "").replace(",", "")
    price_match = re.search(r"(?:¥|￥|价格|price)?\s*(\d{2,6}(?:\.\d{1,2})?)", text, flags=re.I)
    return float(price_match.group(1)) if price_match else None


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


def _dump_debug_html(html: str, filename: str) -> None:
    path = PROJECT_ROOT / "data" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _delay() -> None:
    low = float(os.getenv("TAOBAO_DELAY_MIN_SECONDS", "3"))
    high = float(os.getenv("TAOBAO_DELAY_MAX_SECONDS", "8"))
    time.sleep(max(low, min(high, low if high < low else __import__("random").uniform(low, high))))


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _choose_keyword_interactively() -> str:
    print("请选择爬取方式：")
    print("  1) 爬型号")
    print("  2) 爬类型")
    mode = input("请输入 [1/2，默认 2]：").strip() or "2"
    if mode == "1":
        return input("请输入要爬取的型号：").strip() or "手机"

    print("请选择类型：")
    print("  1) 手机")
    print("  2) 电脑")
    category = input("请输入 [1/2，默认 1]：").strip() or "1"
    return "笔记本电脑" if category == "2" else "手机"


def _print_failed_progress(start_index: int, target: int) -> None:
    for index in range(start_index, target + 1):
        print(f"抓取第{index}条数据，抓取失败", flush=True)


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="运行淘宝 Scrapling 爬虫。")
    parser.add_argument("keyword", nargs="?", help="直接指定型号或类型关键词。")
    parser.add_argument("--limit", type=int, default=DEFAULT_CRAWL_LIMIT, help="抓取条数，默认 350。")
    parser.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES, help="最多翻页数，默认 14。")
    args = parser.parse_args()
    os.environ["TAOBAO_SCRAPLING_MAX_PAGES"] = str(max(1, args.pages))

    keyword = args.keyword or _choose_keyword_interactively()
    rows: list[dict[str, Any]] = []

    def save_and_print(row: dict[str, Any], index: int) -> None:
        try:
            _save_rows([row])
            print(f"抓取第{index}条数据，抓取成功", flush=True)
        except Exception:
            print(f"抓取第{index}条数据，抓取失败", flush=True)

    try:
        rows = crawl(
            keyword=keyword,
            limit=args.limit,
            allow_live=True,
            fetch_comments=True,
            row_callback=save_and_print,
        )
    except Exception:
        pass
    if len(rows) < args.limit:
        _print_failed_progress(len(rows) + 1, args.limit)


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


if __name__ == "__main__":
    main()
