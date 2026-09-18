from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from forge.config.store import list_config_for_display, set_config

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    value: str | None = None


@router.get("")
def get_all_config() -> list[dict]:
    return list_config_for_display()


@router.put("/{key}")
def update_config(key: str, body: ConfigUpdate) -> dict:
    set_config(key, body.value)
    return {"key": key, "updated": True}
