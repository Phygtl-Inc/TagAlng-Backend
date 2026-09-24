"""Worker API client with the program's hard rails enforced in code.

The rails in LANA_ZERO_BUG_PROGRAM_FINAL.md §5 are written as instructions for a
human or an LLM agent. An overnight swarm running unattended against a database
with 31 real users in it needs them to be *unreachable*, not *discouraged*:

  * `/hooks/*` is not callable — the endpoint allowlist rejects it before a
    socket is opened. Those endpoints fan out real push and email to real people
    and are not rate limited.
  * `complete_session()` has no `publish` parameter. It always sends
    `{"publish": false}`. `CompleteSessionRequest.publish` defaults to **true**
    in the worker (verified in app/models.py:525), so a forgotten kwarg would
    publish a real event to a real block.
  * Session creation is rate limited to the caps in Config, so our own swarm
    does not trip the bot heuristic that PR #124 exists to add.

There is deliberately no escape hatch on any of the three. If a future section
needs to publish, it should add a separate, obviously-named method that a
reviewer will notice.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config


class RailViolation(RuntimeError):
    """Raised when calling code tries to do something the program forbids."""


class WorkerError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{path} -> HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.path = path


# Every endpoint the swarm is permitted to touch. Anything absent is either
# forbidden (/hooks/*) or simply not needed by a section yet — in which case add
# it here consciously rather than discovering it works by accident.
ALLOWED = {
    "GET /",
    "GET /health",
    "POST /lana/sessions",
    "POST /lana/sessions/{id}",  # GET detail, templated below
    "GET /lana/sessions/{id}",
    "POST /lana/sessions/{id}/messages",
    "POST /lana/sessions/{id}/complete",
    "POST /lana/area/progress",
    "POST /lana/circles/mine",
    "POST /lana/rapport/next-ask",
    "GET /lana/users/{id}/block-log",
}

FORBIDDEN_PREFIXES = ("/hooks",)


class RateLimiter:
    """Two-window token bucket for session creation.

    Not a nicety: SPEC_P0_SIGNUP.md hard rail 6 caps anonymous session creation
    at 3/minute, and PR #124 §4.2.2 flags >40 sessions/hour as scripted. Nine
    personas x several arms will exceed both if issued as fast as httpx allows.
    """

    def __init__(self, per_minute: int, per_hour: int):
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._minute: list[float] = []
        self._hour: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a session may be created. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._minute = [t for t in self._minute if now - t < 60]
                self._hour = [t for t in self._hour if now - t < 3600]
                if len(self._minute) < self._per_minute and len(self._hour) < self._per_hour:
                    self._minute.append(now)
                    self._hour.append(now)
                    return waited
                if len(self._minute) >= self._per_minute:
                    sleep_for = 60 - (now - self._minute[0]) + 0.05
                else:
                    sleep_for = 3600 - (now - self._hour[0]) + 0.05
            sleep_for = max(sleep_for, 0.1)
            time.sleep(sleep_for)
            waited += sleep_for


@dataclass
class Turn:
    """One request/response pair, kept verbatim for the transcript."""

    seq: int
    sent: str
    status: int
    response: dict[str, Any]
    latency_ms: int
    arm: str = "E-VOICE"
    error: str | None = None

    @property
    def assistant_message(self) -> str:
        return self.response.get("assistant_message") or ""

    @property
    def routing(self) -> dict[str, Any]:
        """TurnRouting — the machine-readable decision record
        (_CODE_TRUTH_2026-07-30.md §TIER2). Absent on CreateSessionResponse.
        """
        return self.response.get("routing") or {}

    @property
    def ui_actions(self) -> list[dict[str, Any]]:
        return self.response.get("ui_actions") or []

    @property
    def focus_phrase(self) -> str | None:
        return ((self.response.get("ui") or {}).get("focus_phrase")) or None

    @property
    def preferred_language(self) -> str | None:
        """The locale echo.

        Worth flagging: `_CODE_TRUTH_2026-07-30.md` and `SPEC_P1_LANGUAGE.md`
        K1-2 both state there is "no locale echo in any response". That is
        wrong — `preferred_language` is present on CreateSessionResponse AND on
        SendMessageResponse, where app/models.py:485-487 documents it as
        "echoed every turn so the FE can follow a mid-chat language switch
        (auto-persisted after 2 diverging turns)". P1's S-block can therefore be
        asserted per turn from the API, not only from a users.locale DB read.
        """
        return self.response.get("preferred_language")


class WorkerClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._limiter = RateLimiter(cfg.sessions_per_minute, cfg.sessions_per_hour)
        self._http = httpx.Client(
            base_url=cfg.worker_base_url,
            timeout=cfg.request_timeout_s,
            follow_redirects=False,
        )
        self.sessions_created = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WorkerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------------------------------------------------------------- internals

    def _guard(self, method: str, path: str) -> None:
        for bad in FORBIDDEN_PREFIXES:
            if path.startswith(bad):
                raise RailViolation(
                    f"{method} {path} is forbidden. /hooks/* fans out real push and email to real "
                    "people and is not rate limited (LANA_ZERO_BUG_PROGRAM_FINAL.md §5)."
                )
        # Template the path so /lana/sessions/<uuid>/messages matches the allowlist.
        parts = path.strip("/").split("/")
        templated = "/".join("{id}" if _looks_like_id(p) else p for p in parts)
        key = f"{method} /{templated}" if templated else f"{method} /"
        if key not in ALLOWED:
            raise RailViolation(
                f"{method} {path} is not on the swarm endpoint allowlist (resolved to '{key}'). "
                "Add it to worker.ALLOWED deliberately if a section genuinely needs it."
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        jwt: str | None = None,
        accept_language: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any], int]:
        self._guard(method, path)
        headers: dict[str, str] = {"content-type": "application/json"}
        if jwt:
            headers["authorization"] = f"Bearer {jwt}"
        if accept_language:
            headers["accept-language"] = accept_language

        started = time.monotonic()
        resp = self._http.request(method, path, headers=headers, json=json_body)
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text}
        if not isinstance(body, dict):
            body = {"_raw": body}
        return resp.status_code, body, latency_ms

    # ------------------------------------------------------------------- probes

    def root(self) -> dict[str, Any]:
        _, body, _ = self._request("GET", "/")
        return body

    def health(self) -> dict[str, Any]:
        _, body, _ = self._request("GET", "/health")
        return body

    # ----------------------------------------------------------------- sessions

    def create_session(
        self,
        jwt: str,
        *,
        accept_language: str | None,
        purpose: str = "lana",
        force_new: bool = True,
    ) -> Turn:
        """POST /lana/sessions.

        `purpose` stays "lana" for every section. D-05 / K1-7: `profile_intake`
        runs on Gemini regardless of LANA_LLM_PROVIDER, so using it would
        exercise a second undocumented model path and confound the verdict.
        """
        if purpose != "lana":
            raise RailViolation(
                f"purpose={purpose!r} is not permitted. D-05: the profile_intake path runs on "
                "Gemini despite LANA_LLM_PROVIDER=openai and will confound any language or "
                "extraction verdict. Sections stay on purpose='lana'."
            )
        self._limiter.acquire()
        status, body, ms = self._request(
            "POST",
            "/lana/sessions",
            jwt=jwt,
            accept_language=accept_language,
            json_body={"purpose": purpose, "force_new": force_new},
        )
        self.sessions_created += 1
        return Turn(seq=0, sent="<session-create>", status=status, response=body, latency_ms=ms)

    def send_message(
        self,
        jwt: str,
        session_id: str,
        message: str,
        *,
        seq: int,
        accept_language: str | None,
        arm: str = "E-VOICE",
        intent_hint: str | None = None,
    ) -> Turn:
        payload: dict[str, Any] = {"message": message}
        if intent_hint:
            payload["intent_hint"] = intent_hint
        status, body, ms = self._request(
            "POST",
            f"/lana/sessions/{session_id}/messages",
            jwt=jwt,
            accept_language=accept_language,
            json_body=payload,
        )
        return Turn(seq=seq, sent=message, status=status, response=body, latency_ms=ms, arm=arm)

    def complete_session(self, jwt: str, session_id: str, *, seq: int) -> Turn:
        """POST /lana/sessions/{id}/complete with publish HARD-CODED false.

        There is no parameter to change this. CompleteSessionRequest.publish
        defaults to True in the worker (app/models.py:525) — a session completed
        with default args publishes a real event to a real block.
        """
        status, body, ms = self._request(
            "POST",
            f"/lana/sessions/{session_id}/complete",
            jwt=jwt,
            json_body={"publish": False},  # never make this configurable
        )
        turn = Turn(seq=seq, sent="<complete publish=false>", status=status, response=body, latency_ms=ms)
        # Belt and braces: if the worker ever ignores the flag, stop the run
        # rather than keep publishing on subsequent personas.
        if status == 200 and body.get("published") is True:
            raise RailViolation(
                f"/complete returned published=true for session {session_id} despite "
                "publish=false. A real event may have been created. Halting the run."
            )
        return turn

    def area_progress(self, jwt: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST /lana/area/progress.

        Note this endpoint does read-repair recounting and doubles as the ZIP
        state-transition trigger (_CODE_TRUTH §TIER2), so calling it is a WRITE
        even though it reads like a query. D-27: ZIP 32839 has no zip_unlock row
        and this call may create it — worth observing, not a failure.
        """
        status, resp, _ = self._request("POST", "/lana/area/progress", jwt=jwt, json_body=body)
        return status, resp


def _looks_like_id(part: str) -> bool:
    """A path segment that is a uuid or other opaque id, for allowlist templating."""
    if len(part) >= 16 and ("-" in part or part.isalnum()):
        return not part.replace("-", "").isalpha()
    return False
