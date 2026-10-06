from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from elstin.config.store import list_config_categories, list_config_for_display, set_config
from elstin.services.model_options import list_model_options

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


@router.get("/models")
async def get_model_options(project_id: int | None = None) -> dict:
    """Suggestions for the model settings: the provider's own listing, else the
    models elstin has a price for. `error` says why the listing was not used.
    With `project_id`, the listing comes from that project's provider."""
    return await list_model_options(project_id)


@router.put("/{key}")
def update_config(key: str, body: ConfigUpdate) -> dict:
    set_config(key, body.value)
    return {"key": key, "updated": True}
