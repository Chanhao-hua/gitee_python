"""Vipshop live crawler using a visible Playwright profile."""

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
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "data" / "_vip_profile"
DEFAULT_TIMEOUT = 60_000
DEFAULT_WAIT_SECONDS = 900


def crawl(
    keyword: str,
    limit: int = 150,
    allow_live: bool = False,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    if not allow_live:
        raise RuntimeError("唯品会爬虫已切换为真实数据模式，请使用 mode=live。")
    return _crawl_with_browser(keyword=keyword, limit=limit, row_callback=row_callback)


def _crawl_with_browser(
    keyword: str,
    limit: int,
    row_callback: Callable[[dict[str, Any], int], None] | None = None,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("唯品会爬虫需要安装 playwright。") from exc

    profile_dir = Path(os.getenv("VIP_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)))
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = os.getenv("VIP_HEADLESS", "false").lower() == "true"
    wait_seconds = int(os.getenv("VIP_WAIT_SECONDS", str(DEFAULT_WAIT_SECONDS)))

    search_url = f"https://category.vip.com/suggest.php?keyword={quote(keyword)}"
    rows: list[dict[str, Any]] = []
    seen_product_ids: set[str] = set()

    def on_response(response: Any) -> None:
        if len(rows) >= limit:
            return
        url = response.url
        if "/shopping/pc/product/module/list" not in url and "/shopping/pc/search/product/rank" not in url:
            return
        try:
            text = response.text()
        except Exception:
            return
        for row in _rows_from_vip_response(text, keyword):
            if len(rows) >= limit:
                return
            product_id = row.get("product_id") or ""
            if not product_id or product_id in seen_product_ids:
                continue
            if not _looks_like_digital_product(row.get("title", "")):
                continue
            seen_product_ids.add(product_id)
            row["rank"] = len(rows) + 1
            rows.append(row)
            if row_callback:
                row_callback(row, len(rows))

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            locale="zh-CN",
            viewport={"width": 1366, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", on_response)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            page.wait_for_timeout(5_000)
            _wait_for_manual_auth_if_needed(page, search_url, headless, wait_seconds)
            _scroll_until_enough(page, rows, limit)
            if not rows:
                dump_path = PROJECT_ROOT / "data" / "_vip_last_search.html"
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                dump_path.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(
                    "唯品会页面已打开，但没有解析到商品数据。"
                    f"已保存页面快照：{dump_path}"
                )
            return rows[:limit]
        finally:
            context.close()


def _scroll_until_enough(page: Any, rows: list[dict[str, Any]], limit: int) -> None:
    max_rounds = max(1, int(os.getenv("VIP_SCROLL_ROUNDS", "30")))
    stable_rounds = 0
    last_count = len(rows)
    for _ in range(max_rounds):
        if len(rows) >= limit:
            return
        try:
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(2_000)
        except Exception:
            return
        if len(rows) == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = len(rows)
        if stable_rounds >= 5:
            return


def _rows_from_vip_response(text: str, keyword: str) -> list[dict[str, Any]]:
    data = _loads_json_or_jsonp(text)
    if not isinstance(data, dict):
        return []

    rows: list[dict[str, Any]] = []
    for item in _walk_dicts(data):
        row = _row_from_vip_item(item, keyword)
        if row:
            rows.append(row)
    return rows


def _row_from_vip_item(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    product_id = _first_text(
        item,
        "productId",
        "product_id",
        "pid",
        "goodsId",
        "goods_id",
        "spuId",
        "id",
    )
    title = _first_text(
        item,
        "productName",
        "product_name",
        "goodsName",
        "goods_name",
        "title",
        "name",
        "shortName",
    )
    if not product_id or not title:
        return None
    if not any(key in item for key in ("salePrice", "vipshopPrice", "marketPrice", "brandName", "productName", "goodsName")):
        return None

    merchant = _first_text(item, "brandName", "brand_name", "storeName", "shopName", "vendorName")
    price = _parse_price_from_item(item)
    return {
        "source": "vip",
        "product_id": product_id,
        "keyword": keyword,
        "title": _clean_text(title),
        "price": price,
        "merchant": _clean_text(merchant) or "唯品会",
        "rating_tags": "",
        "comment_text": "",
        "comment_count": _parse_comment_count(item),
        "rank": None,
        "crawled_at": _now(),
    }


def _parse_price_from_item(item: dict[str, Any]) -> float | None:
    for key in (
        "salePrice",
        "vipshopPrice",
        "vipPrice",
        "price",
        "sellPrice",
        "minPrice",
        "marketPrice",
    ):
        price = _parse_price(item.get(key))
        if price is not None:
            return price
    for child in item.values():
        if isinstance(child, dict):
            price = _parse_price_from_item(child)
            if price is not None:
                return price
    return None


def _parse_comment_count(item: dict[str, Any]) -> int:
    for key in ("comments", "commentCount", "commentsCount", "reviewCount", "evaluateCount"):
        count = _parse_count(item.get(key))
        if count:
            return count
    comments_info = item.get("commentsInfo")
    if isinstance(comments_info, dict):
        return _parse_comment_count(comments_info)
    return 0


def _loads_json_or_jsonp(text: str) -> Any:
    body = text.strip()
    if not body:
        return None
    if body.startswith("{") or body.startswith("["):
        return json.loads(body)
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        return json.loads(body[start : end + 1])
    return None


def _wait_for_manual_auth_if_needed(
    page: Any,
    target_url: str,
    headless: bool,
    wait_seconds: int,
) -> None:
    if not _looks_like_login_page(page):
        return
    if headless:
        raise RuntimeError("唯品会需要登录，但 VIP_HEADLESS=true。请使用可见浏览器完成登录。")
    print(f"需要登录唯品会，请在弹出的浏览器中完成操作，最多等待 {wait_seconds} 秒。")
    deadline = time.time() + max(1, wait_seconds)
    while time.time() < deadline:
        page.wait_for_timeout(3_000)
        if not _looks_like_login_page(page):
            page.goto(target_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            page.wait_for_timeout(5_000)
            return
    raise RuntimeError("等待超时，唯品会登录未完成。")


def _looks_like_login_page(page: Any) -> bool:
    url = page.url.lower()
    if "passport.vip.com/login" in url:
        return True
    try:
        title = page.title()
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    markers = ("唯品会网站登录", "扫码登录", "账户登录", "免费注册")
    return any(marker in f"{title}\n{body}" for marker in markers)


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
        if value is not None and value != "":
            return str(value)
    return ""


def _parse_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 100 if number > 100000 else number
    text = str(value).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _parse_count(value: Any) -> int:
    if value is None or value == "":
        return 0
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


def _looks_like_digital_product(title: str) -> bool:
    lowered = title.lower()
    excluded = (
        "手机壳",
        "保护壳",
        "贴膜",
        "数据线",
        "充电器",
        "耳机",
        "鼠标",
        "键盘",
        "支架",
        "包",
    )
    if any(word in lowered for word in excluded):
        return False
    included = (
        "iphone",
        "手机",
        "智能手机",
        "全网通",
        "5g",
        "华为",
        "荣耀",
        "小米",
        "苹果",
        "oppo",
        "vivo",
        "iqoo",
        "redmi",
        "笔记本",
        "电脑",
        "book",
        "thinkpad",
        "macbook",
        "matebook",
        "联想",
        "惠普",
        "华硕",
        "戴尔",
    )
    return any(word in lowered for word in included)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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


def main() -> None:
    _ensure_project_root_on_path()
    parser = argparse.ArgumentParser(description="运行唯品会爬虫。")
    parser.add_argument("keyword", nargs="?", help="直接指定型号或类型关键词。")
    parser.add_argument("--limit", type=int, default=150, help="抓取条数，默认 150。")
    args = parser.parse_args()

    keyword = args.keyword or _choose_keyword_interactively()
    rows: list[dict[str, Any]] = []

    def save_and_print(row: dict[str, Any], index: int) -> None:
        try:
            _save_rows([row])
            print(f"抓取第{index}条数据，抓取成功", flush=True)
        except Exception:
            print(f"抓取第{index}条数据，抓取失败", flush=True)

    try:
        rows = crawl(keyword=keyword, limit=args.limit, allow_live=True, row_callback=save_and_print)
    except Exception:
        pass
    if len(rows) < args.limit:
        print(f"没有更多符合条件的数据，本次共抓取{len(rows)}条。", flush=True)


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
