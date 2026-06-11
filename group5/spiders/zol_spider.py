"""ZOL live crawler placeholder.

ZOL is kept as an optional third source, but it no longer returns demo data.
"""

from __future__ import annotations


def crawl(keyword: str, limit: int = 30, allow_live: bool = False):
    raise RuntimeError("ZOL 真实抓取未在本次需求中启用；当前仅实现淘宝/苏宁真实数据入口。")
