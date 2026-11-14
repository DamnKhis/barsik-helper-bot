"""Отдача готового дашборда. Расчёт метрик и /stats — не наша зона."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["dashboard"])

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "bars-dashboard.html"


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
