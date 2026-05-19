"""Supabase Storage client (upload + signed URL generation).

We hit the REST API directly with httpx rather than pulling in the full
`supabase-py` package — this is two endpoints, no auth refresh, no
session state. Using the storage REST API keeps the dependency footprint
small.

Auth uses the service-role key (not the anon key) so we can write to
private buckets. The service-role key is never exposed to the browser;
the admin route generates a fresh signed URL on every render.
"""

from __future__ import annotations

import httpx

from logging_config import log
from settings import settings

RECORDING_BUCKET = "call-recordings"


def _is_configured() -> bool:
    return bool(settings.SUPABASE_URL) and bool(settings.SUPABASE_SERVICE_ROLE_KEY)


def _headers() -> dict[str, str]:
    # Both Authorization and apikey are accepted; using both is the
    # documented pattern for service-role calls.
    return {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
    }


async def upload_recording(call_id: str, wav_bytes: bytes) -> str | None:
    """Upload `wav_bytes` to call-recordings/<call_id>.wav. Returns the
    object key on success, None on failure (logged but never raises — a
    missing recording must not break post_call).
    """
    if not _is_configured():
        log.info("storage.skip_no_config", call_id=call_id)
        return None
    if not wav_bytes:
        log.info("storage.skip_empty", call_id=call_id)
        return None

    key = f"{call_id}.wav"
    url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{RECORDING_BUCKET}/{key}"
    headers = {
        **_headers(),
        "Content-Type": "audio/wav",
        # `x-upsert: true` lets a re-run of post_call replace an
        # existing recording rather than 409 — useful when retrying
        # after a partial post_call failure.
        "x-upsert": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.post(url, headers=headers, content=wav_bytes)
        if resp.status_code >= 400:
            log.error(
                "storage.upload_failed",
                call_id=call_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
            return None
        log.info("storage.upload_ok", call_id=call_id, bytes=len(wav_bytes))
        return key
    except Exception:  # noqa: BLE001
        log.exception("storage.upload_exception", call_id=call_id)
        return None


async def signed_url(key: str, expires_in: int = 3600) -> str | None:
    """Generate a short-lived signed URL for `key` in the recordings
    bucket. Used by the admin call-detail route on every render so the
    URL is always fresh; default 1-hour expiry is plenty for a single
    page view but limits risk if the URL leaks.
    """
    if not _is_configured() or not key:
        return None
    sign_url = (
        f"{settings.SUPABASE_URL.rstrip('/')}"
        f"/storage/v1/object/sign/{RECORDING_BUCKET}/{key}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(sign_url, headers=_headers(), json={"expiresIn": expires_in})
        if resp.status_code >= 400:
            log.warning("storage.sign_failed", key=key, status=resp.status_code, body=resp.text[:200])
            return None
        body = resp.json()
        # API returns `{"signedURL": "/object/sign/<bucket>/<key>?token=..."}`
        path = body.get("signedURL") or body.get("signedUrl")
        if not path:
            return None
        # The path is relative to the storage host root; prepend the base.
        return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1{path}"
    except Exception:  # noqa: BLE001
        log.exception("storage.sign_exception", key=key)
        return None
