from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter



logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/me")
async def me():
    logger.info("Getting user me")
    return {"user_id": str(uuid.uuid4())}