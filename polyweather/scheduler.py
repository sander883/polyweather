"""Background scheduler.

Runs ``scan`` and ``settle`` on a recurring interval so the user can leave
uvicorn running in screen / nohup and not babysit. Each job is best-effort:
exceptions are logged but never raised — the next tick keeps trying.

Manual ``POST /scan`` and ``POST /settle`` continue to work even when the
scheduler is disabled.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from polyweather.config import get_settings
from polyweather.scanner.scan import scan_once
from polyweather.trading.paper import execute_pending_signals
from polyweather.trading.resolver import settle_open_positions

log = logging.getLogger(__name__)

_SCHEDULER: AsyncIOScheduler | None = None


async def _do_scan_cycle() -> None:
    s = get_settings()
    try:
        result = await scan_once()
        log.info(
            "auto-scan: events=%d signals=%d error=%s",
            result.get("events_seen", 0),
            result.get("signals_emitted", 0),
            result.get("error"),
        )
        if s.scheduler_auto_execute and not result.get("error"):
            opened = execute_pending_signals()
            if opened:
                log.info("auto-scan: opened %d paper positions", len(opened))
    except Exception:  # noqa: BLE001
        log.exception("auto-scan failed")


async def _do_settle_cycle() -> None:
    try:
        result = await settle_open_positions(only_past_settle=True)
        if result.get("closed"):
            log.info(
                "auto-settle: closed=%d still_open=%d total_pnl=%.2f",
                result["closed"], result["still_open"], result["total_pnl_usd"],
            )
        else:
            log.debug(
                "auto-settle: nothing to close (still_open=%d)",
                result.get("still_open", 0),
            )
    except Exception:  # noqa: BLE001
        log.exception("auto-settle failed")


def start_scheduler() -> AsyncIOScheduler | None:
    global _SCHEDULER
    s = get_settings()
    if not s.scheduler_enabled:
        log.info("scheduler disabled via config")
        return None
    if _SCHEDULER is not None and _SCHEDULER.running:
        return _SCHEDULER

    sched = AsyncIOScheduler()
    sched.add_job(
        _do_scan_cycle,
        trigger=IntervalTrigger(minutes=s.scheduler_scan_minutes),
        id="auto-scan",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _do_settle_cycle,
        trigger=IntervalTrigger(minutes=s.scheduler_settle_minutes),
        id="auto-settle",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _SCHEDULER = sched
    log.info(
        "scheduler started: scan every %dm, settle every %dm, auto_execute=%s",
        s.scheduler_scan_minutes, s.scheduler_settle_minutes, s.scheduler_auto_execute,
    )

    if s.scheduler_run_on_startup:
        # Fire-and-forget initial cycle so the user gets immediate feedback
        # without waiting for the first interval tick.
        loop = asyncio.get_event_loop()
        loop.create_task(_do_scan_cycle())
        loop.create_task(_do_settle_cycle())

    return sched


def stop_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is not None and _SCHEDULER.running:
        _SCHEDULER.shutdown(wait=False)
    _SCHEDULER = None


def scheduler_status() -> dict:
    s = get_settings()
    if _SCHEDULER is None or not _SCHEDULER.running:
        return {"running": False, "enabled": s.scheduler_enabled}
    jobs = []
    for job in _SCHEDULER.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return {
        "running": True,
        "enabled": s.scheduler_enabled,
        "scan_interval_minutes": s.scheduler_scan_minutes,
        "settle_interval_minutes": s.scheduler_settle_minutes,
        "auto_execute": s.scheduler_auto_execute,
        "jobs": jobs,
    }
