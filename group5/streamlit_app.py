"""Simplified Streamlit console for the Agent Skill demo."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


API_BASE = os.getenv("AGENT_API_BASE", "http://127.0.0.1:8000")
SOURCE_LABELS = {"jd": "京东", "suning": "苏宁易购", "taobao": "淘宝", "zol": "中关村在线"}


st.set_page_config(
    page_title="数码电商 Agent Skill 调度台",
    page_icon="D",
    layout="wide",
)


def main() -> None:
    _inject_css()
    st.title("数码电商 Agent Skill 调度台")
    st.caption("FastAPI 负责 Agent、skills、定时任务和 SQLite；Streamlit 负责课堂演示控制台。")

    health = _get("/health")
    _render_health(health)

    command_col, data_col = st.columns([0.34, 0.66], gap="large")
    with command_col:
        _render_command_panel()
        _render_job_panel()
    with data_col:
        _render_data_panel()


def _render_health(health: dict[str, Any] | None) -> None:
    if not health:
        st.error("FastAPI 未连接。请先运行：uvicorn api.main:app --reload --port 8000")
        return
    status_cols = st.columns(4)
    status_cols[0].metric("FastAPI", "已连接")
    status_cols[1].metric("Scheduler", health.get("scheduler", "unknown"))
    sqlite_total = health.get("sqlite", {}).get("total", 0)
    status_cols[2].metric("SQLite 数据量", sqlite_total)
    status_cols[3].metric("Gitee AI", health.get("gitee_ai", "unknown"))


def _render_command_panel() -> None:
    st.subheader("Agent 指令")
    default_command = "每30分钟抓取 手机 京东和苏宁价格、商家、评论数量和具体评论"
    command = st.text_area("自然语言指令", value=default_command, height=92)
    if st.button("执行 Agent 指令", use_container_width=True):
        result = _post("/agent/command", {"command": command})
        st.session_state["last_agent_result"] = result

    quick_cols = st.columns(2)
    keyword = st.text_input("关键词", value=st.session_state.get("keyword", "手机"))
    st.session_state["keyword"] = keyword
    if quick_cols[0].button("手动抓取", use_container_width=True):
        st.session_state["last_agent_result"] = _post(
            "/skills/crawl",
            {"source": "jd,suning", "keyword": keyword, "limit": 30, "mode": "live"},
        )
    if quick_cols[1].button("设置定时", use_container_width=True):
        st.session_state["last_agent_result"] = _post(
            "/skills/schedule",
            {
                "source": "jd,suning",
                "keyword": keyword,
                "interval_minutes": 30,
                "limit": 30,
                "mode": "live",
            },
        )

    result = st.session_state.get("last_agent_result")
    if result:
        st.markdown("#### 最近一次 Skill 结果")
        st.info(result.get("message") or result.get("action") or "Skill 已执行")
        with st.expander("查看 JSON 结果"):
            st.json(result)


def _render_job_panel() -> None:
    st.subheader("定时任务")
    jobs = _get("/skills/jobs")
    job_rows = jobs.get("jobs", []) if jobs else []
    if not job_rows:
        st.caption("暂无定时任务。")
        return
    for job in job_rows:
        source_label = SOURCE_LABELS.get(job.get("source"), job.get("source", "未知来源"))
        st.markdown(
            f"""
            <div class="job-row">
                <strong>{source_label} · {job.get('keyword')}</strong>
                <span>{job.get('status', 'scheduled')}</span><br/>
                <small>下次运行：{job.get('next_run_at') or '等待调度'}</small>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_data_panel() -> None:
    st.subheader("数据结果")
    keyword = st.session_state.get("keyword", "手机")
    compare = _get("/data/compare", params={"keyword": keyword})
    products = _get("/data/products", params={"keyword": keyword, "limit": 120})

    if not compare or not products:
        st.warning("等待后端数据。")
        return

    rows = products.get("rows", [])
    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("暂无真实数据。请先手动抓取，或配置淘宝授权真实数据/API 后再执行 Agent 指令。")
        return

    display = df.copy()
    display["平台"] = display["source"].map(SOURCE_LABELS).fillna(display["source"])
    display = display.rename(
        columns={
            "title": "商品标题",
            "price": "价格",
            "merchant": "商家",
            "rating_tags": "评价标签",
            "comment_text": "评论内容",
            "comment_count": "评论数量",
            "rank": "ZOL排行",
            "crawled_at": "更新时间",
        }
    )
    if "评论内容" not in display.columns:
        display["评论内容"] = ""
    table_columns = ["平台", "商品标题", "价格", "商家", "评论数量", "评论内容", "ZOL排行", "更新时间"]
    st.dataframe(
        display[table_columns],
        use_container_width=True,
        height=330,
    )

    agg = pd.DataFrame(compare.get("aggregates", []))
    if not agg.empty:
        agg["平台"] = agg["source"].map(SOURCE_LABELS)
        chart_cols = st.columns([0.58, 0.42], gap="large")
        with chart_cols[0]:
            fig = px.bar(
                agg,
                x="平台",
                y="min_price",
                color="平台",
                title=f"{keyword} 多平台最低价对比",
                labels={"min_price": "最低价"},
            )
            fig.update_layout(height=320, showlegend=False, margin=dict(l=10, r=10, t=48, b=20))
            st.plotly_chart(fig, use_container_width=True)
        with chart_cols[1]:
            st.markdown("#### 数据源概览")
            for item in compare.get("aggregates", []):
                label = SOURCE_LABELS.get(item["source"], item["source"])
                st.metric(label, f"{item['item_count']} 条", f"商家 {item['merchant_count']}")

    st.caption(f"刷新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        response = requests.get(f"{API_BASE}{path}", params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.post(f"{API_BASE}{path}", json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        return {"ok": False, "message": f"请求失败：{exc}"}


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #f7f9fc; }
        h1, h2, h3 { letter-spacing: 0; }
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #e6ebf2;
            border-radius: 8px;
            padding: 12px 14px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04);
        }
        .job-row {
            background: #ffffff;
            border: 1px solid #e6ebf2;
            border-left: 4px solid #0ea5e9;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 10px;
        }
        .job-row span {
            float: right;
            color: #047857;
            font-size: 12px;
            font-weight: 700;
        }
        .stButton > button {
            border-radius: 8px;
            border: 1px solid #d7e0ea;
            background: #ffffff;
            color: #0f172a;
            font-weight: 700;
        }
        .stButton > button:hover {
            border-color: #0ea5e9;
            color: #0369a1;
        }
        textarea, input {
            color: #0f172a !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
