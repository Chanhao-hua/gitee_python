"""APScheduler integration for recurring crawler skills."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler


_scheduler: BackgroundScheduler | None = None
_job_results: dict[str, dict[str, Any]] = {}


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    return _scheduler


def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()


def schedule_crawl_job(
    source: str,
    keyword: str,
    interval_minutes: int = 30,
    limit: int = 30,
    mode: str = "live",
) -> dict[str, Any]:
    start_scheduler()
    interval_minutes = max(1, int(interval_minutes))
    job_id = f"crawl:{source}:{keyword}:{interval_minutes}".replace(" ", "_")
    scheduler = get_scheduler()
    scheduler.add_job(
        _execute_crawl_job,
        "interval",
        minutes=interval_minutes,
        id=job_id,
        replace_existing=True,
        kwargs={
            "source": source,
            "keyword": keyword,
            "limit": limit,
            "mode": mode,
            "job_id": job_id,
        },
    )
    job = scheduler.get_job(job_id)
    _job_results[job_id] = {
        "job_id": job_id,
        "source": source,
        "keyword": keyword,
        "status": "scheduled",
        "last_result_count": 0,
        "last_error": None,
        "last_run_at": None,
        "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }
    return _job_results[job_id]


def get_job_status() -> list[dict[str, Any]]:
    scheduler = get_scheduler()
    jobs = []
    for job in scheduler.get_jobs():
        info = dict(_job_results.get(job.id, {}))
        info.setdefault("job_id", job.id)
        info["next_run_at"] = job.next_run_time.isoformat() if job.next_run_time else None
        jobs.append(info)
    return jobs


def _execute_crawl_job(
    source: str,
    keyword: str,
    limit: int,
    mode: str,
    job_id: str,
) -> None:
    from agent.skills import run_crawl

    try:
        result = run_crawl(source=source, keyword=keyword, limit=limit, mode=mode)
        _job_results[job_id] = {
            **_job_results.get(job_id, {}),
            "status": "ok" if result.get("ok") else "fallback",
            "last_result_count": result.get("inserted", 0),
            "last_error": result.get("error"),
            "last_run_at": datetime.now().replace(microsecond=0).isoformat(),
        }
    except Exception as exc:  # pragma: no cover - scheduler safety net
        _job_results[job_id] = {
            **_job_results.get(job_id, {}),
            "status": "error",
            "last_result_count": 0,
            "last_error": str(exc),
            "last_run_at": datetime.now().replace(microsecond=0).isoformat(),
        }
