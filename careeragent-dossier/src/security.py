"""
src/security.py

Inbound authentication for careeragent-dossier.

Every endpoint except GET /health requires a valid X-API-Key header, validated
against DOSSIER_API_KEY (the api<->dossier boundary secret). The comparison is
constant-time so a timing side-channel cannot recover the key.

careeragent-dossier's ONLY client is careeragent-api (the agent). This is the
same pattern every CareerAgent service uses; it is isolated here so it can later
move from a single shared key to a per-caller lookup without touching the
endpoint logic.
"""
import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

# The inbound secret. careeragent-api sends this as its X-API-Key on every tool
# call. Independent of every other boundary key in the system.
API_KEY = os.environ.get("DOSSIER_API_KEY", "")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Security(_api_key_header)) -> str:
    """FastAPI dependency: 401 if the X-API-Key header is missing or wrong."""
    if not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key
