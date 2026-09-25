"""Entity graph, Confirmation of Payee, APP confirmation and scenario APIs (P1.2–P1.4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_pipeline
from app.cases.service import CaseNotFoundError
from app.db import repository as repo
from app.scenarios import SCENARIOS, ScenarioError
from app.security.deps import require_role

router = APIRouter(prefix="/api", tags=["network"])
#: demo scenario injector — mounted only outside prod (see create_app)
scenario_router = APIRouter(prefix="/api", tags=["scenarios"])
analyst = Depends(require_role("analist"))
senior = Depends(require_role("kidemli_analist"))


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knows_payee: bool
    note: str = Field(default="", max_length=1000)


def _graph() -> Any:
    graph = require_pipeline().graph
    if graph is None:
        raise HTTPException(status_code=404, detail="Varlık grafı devre dışı (GRAPH_ENABLED=false)")
    return graph


# --- entity graph ------------------------------------------------------------------------
@router.get("/graph/customer/{customer_id}", dependencies=[analyst])
async def customer_graph(
    customer_id: str,
    hops: int = Query(2, ge=1, le=3),
    max_nodes: int = Query(150, ge=10, le=500),
) -> dict[str, Any]:
    """Neighbourhood of a customer in Cytoscape.js ``elements`` format."""
    elements = _graph().neighbourhood(customer_id, hops=hops, max_nodes=max_nodes)
    if not elements["nodes"]:
        raise HTTPException(status_code=404, detail="Müşteri grafta yok")
    return elements


@router.get("/graph/stats", dependencies=[analyst])
async def graph_stats() -> dict[str, Any]:
    return _graph().stats()


@router.get("/graph/rings", dependencies=[analyst])
async def rings(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        return await repo.list_rings(session, limit)


@router.post("/graph/rings/detect", dependencies=[analyst])
async def detect_rings() -> list[dict[str, Any]]:
    """Run Louvain ring detection now (normally periodic)."""
    _graph()
    return await require_pipeline().refresh_rings()


# --- Confirmation of Payee / APP --------------------------------------------------------
@router.get("/cop", dependencies=[analyst])
async def confirmation_of_payee(
    iban: str = Query(..., min_length=10, max_length=34),
    name: str = Query(..., min_length=1, max_length=160),
) -> dict[str, Any]:
    """IBAN–name check a banking app performs before sending (simulated CoP)."""
    payees = require_pipeline().engine.payees
    if payees is None:
        raise HTTPException(status_code=503, detail="CoP kaydı yüklenmedi")
    return payees.confirm(iban, name).as_dict()


@router.post("/app/confirm/{transaction_id}", dependencies=[analyst])
async def app_confirmation(transaction_id: str, body: ConfirmIn) -> dict[str, Any]:
    """Simulated customer answer to "Bu kişiyi tanıyor musunuz?" for a held payment."""
    try:
        return await require_pipeline().cases.customer_confirmation(
            transaction_id, knows_payee=body.knows_payee, note=body.note
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="Bekletilen işlem/vaka bulunamadı") from None


# --- scenario injector ("Demo modu") -----------------------------------------------------
# Injects synthetic fraud into the live pipeline (cases, blocks, profile
# feedback): senior-only, and not mounted at all when ENVIRONMENT=prod.
@scenario_router.get("/scenarios", dependencies=[senior])
async def list_scenarios() -> list[dict[str, str]]:
    return [{"name": name, **meta} for name, meta in SCENARIOS.items()]


@scenario_router.post("/scenarios/{name}", dependencies=[senior])
async def run_scenario(name: str, customer_id: str | None = None) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise HTTPException(status_code=404, detail="Bilinmeyen senaryo")
    try:
        return await require_pipeline().run_scenario(name, customer_id)
    except ScenarioError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
