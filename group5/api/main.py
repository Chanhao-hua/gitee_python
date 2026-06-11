"""FastAPI backend for the digital commerce Agent Skill demo."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from agent.orchestrator import AgentOrchestrator
from agent.skills import (
    compare_sources,
    job_status,
    query_products,
    run_crawl,
    schedule_crawl,
)
from jobs.scheduler import start_scheduler
from storage.db import configure_logging, get_database_stats, init_database


app = FastAPI(title="数码电商 Agent Skill 调度 Demo", version="0.1.0")
orchestrator = AgentOrchestrator()


class CommandRequest(BaseModel):
    command: str = Field(..., min_length=1)


class CrawlRequest(BaseModel):
    source: str = "all"
    keyword: str = "手机"
    limit: int = Field(30, ge=1, le=500)
    mode: str = "live"


class ScheduleRequest(BaseModel):
    source: str = "all"
    keyword: str = "手机"
    interval_minutes: int = Field(30, ge=1, le=24 * 60)
    limit: int = Field(30, ge=1, le=500)
    mode: str = "live"


@app.on_event("startup")
def startup() -> None:
    configure_logging()
    init_database(seed=False)
    start_scheduler()


@app.get("/health")
def health() -> dict[str, Any]:
    stats = get_database_stats()
    return {
        "ok": True,
        "fastapi": "connected",
        "scheduler": "running",
        "sqlite": stats,
        "gitee_ai": "configured" if orchestrator.api_key else "fallback_json_action",
    }


@app.post("/agent/command")
def agent_command(payload: CommandRequest) -> dict[str, Any]:
    return orchestrator.handle_command(payload.command)


@app.post("/skills/crawl")
def skill_crawl(payload: CrawlRequest) -> dict[str, Any]:
    return run_crawl(
        source=payload.source,
        keyword=payload.keyword,
        limit=payload.limit,
        mode=payload.mode,
    )


@app.post("/skills/schedule")
def skill_schedule(payload: ScheduleRequest) -> dict[str, Any]:
    return schedule_crawl(
        source=payload.source,
        keyword=payload.keyword,
        interval_minutes=payload.interval_minutes,
        limit=payload.limit,
        mode=payload.mode,
    )


@app.get("/skills/jobs")
def skill_jobs() -> dict[str, Any]:
    return job_status()


@app.get("/data/products")
def data_products(
    keyword: str = Query("手机"),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    return query_products(keyword=keyword, source=source, limit=limit)


@app.get("/data/compare")
def data_compare(keyword: str = Query("手机")) -> dict[str, Any]:
    return compare_sources(keyword=keyword)
