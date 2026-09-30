from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from forge.config.store import list_config_categories, list_config_for_display, set_config

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    value: str | None = None


@router.get("")
def get_all_config() -> list[dict]:
    return list_config_for_display()


@router.get("/categories")
def get_config_categories() -> list[dict]:
    """How the settings screen groups and orders the fields in GET /api/config."""
    return list_config_categories()


@router.put("/{key}")
def update_config(key: str, body: ConfigUpdate) -> dict:
    set_config(key, body.value)
    return {"key": key, "updated": True}
