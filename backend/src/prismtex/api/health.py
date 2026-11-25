"""Liveness check; exempt from the access token so start.sh and Docker can poll it."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from prismtex import __version__

router = APIRouter(prefix="/api", tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    service: Literal["prismtex"]
    version: str


@router.get("/health")
def health() -> Health:
    return Health(status="ok", service="prismtex", version=__version__)
