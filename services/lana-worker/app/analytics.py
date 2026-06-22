"""Amplitude product analytics (server-side, HTTP V2) — fire-and-forget.

No-ops when AMPLITUDE_API_KEY is unset. Sends server-truth conversion events (event
published, signal saved, match created) keyed by the SAME Supabase user_id the browser
uses, so the two stitch into one timeline. Runs on a daemon thread so it never adds
latency to — or breaks — a Lana turn.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx

_URL = "https://api2.amplitude.com/2/httpapi"


def _send(api_key: str, payload: dict[str, Any]) -> None:
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(_URL, json=payload)
    except Exception:  # noqa: BLE001 - analytics must never affect the app
        import logging

        logging.getLogger(__name__).debug("amplitude_send_failed", exc_info=True)


def track(
    event_type: str,
    *,
    user_id: str | None,
    event_properties: dict[str, Any] | None = None,
) -> None:
    """Queue an Amplitude event for `user_id`. Best-effort: returns immediately and
    swallows all errors. No-op without a key or user_id."""
    api_key = os.environ.get("AMPLITUDE_API_KEY", "").strip()
    if not api_key or not user_id:
        return
    payload = {
        "api_key": api_key,
        "events": [
            {
                "user_id": str(user_id),
                "event_type": event_type,
                "event_properties": {
                    k: v for k, v in (event_properties or {}).items() if v is not None
                },
            }
        ],
    }
    try:
        threading.Thread(target=_send, args=(api_key, payload), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
