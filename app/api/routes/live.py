"""Live dashboard feed (P2.1 #1): decision stream over SSE + TPS / latency summary."""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import require_pipeline
from app.security.auth import Principal
from app.security.deps import require_role, stream_principal

router = APIRouter(prefix="/api/live", tags=["live"])


@router.get("/stream")
async def stream(principal: Principal = Depends(stream_principal)) -> StreamingResponse:
    """Server-Sent Events: one ``decision`` event per scored transaction."""
    if not principal.has_role("analist"):
        raise HTTPException(status_code=403, detail="En az 'analist' rolü gerekli")
    pipeline = require_pipeline()
    queue = pipeline.subscribe_live()

    async def events() -> AsyncIterator[str]:
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                data = json.dumps(item, ensure_ascii=False, default=str)
                yield f"event: decision\ndata: {data}\n\n"
        finally:
            pipeline.unsubscribe_live(queue)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/summary", dependencies=[Depends(require_role("analist"))])
async def summary() -> dict[str, Any]:
    """Throughput and synchronous scoring latency over the recent window."""
    pipeline = require_pipeline()
    recent = list(pipeline.recent_live)
    latencies = sorted(e["latency_ms"] for e in recent if e.get("latency_ms"))
    now = asyncio.get_running_loop().time()
    last_minute = [e for e in recent if now - e["_t"] <= 60]

    def pct(q: float) -> float | None:
        if not latencies:
            return None
        return round(latencies[min(len(latencies) - 1, int(q * len(latencies)))], 3)

    counts: dict[str, int] = {}
    for e in recent:
        counts[str(e.get("decision"))] = counts.get(str(e.get("decision")), 0) + 1
    return {
        "window": len(recent),
        "tps_1m": round(len(last_minute) / 60, 3),
        "latency_ms": {
            "p50": round(statistics.median(latencies), 3) if latencies else None,
            "p95": pct(0.95),
            "p99": pct(0.99),
        },
        "decisions": counts,
        "open_cases": (await pipeline.cases.counts()),
        "llm_mode": pipeline.llm.mode,
    }
