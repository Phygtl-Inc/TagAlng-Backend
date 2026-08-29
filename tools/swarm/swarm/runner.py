"""Section x persona x arm orchestration, and the results sink.

One walk = one persona, one section, one arm. It produces exactly one
`simulations` row, which is the reporting unit `LANA_ZERO_BUG_PROGRAM_FINAL.md`
§4 defines and which PR #125 regrained the table's uniqueness to allow.

Registration ordering matters and is not incidental: the test identity is written
into `swarm_run_actors` **before** the first message is sent. If the process dies
mid-walk, teardown can still find and sweep the identity. Registering afterwards
would leak a user on every crash.
"""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .assertions import BLOCKED, ERROR, Evaluator, Result, Scorecard
from .config import Config
from .identity import AnonymousAuth, Db, Identity
from .preflight import Preflight
from .registry import FORBIDDEN_VERDICT_CLASSES, Registry
from .worker import RailViolation, Turn, WorkerClient

SPEC_VERSION = "v1-2026-07-30"


@dataclass
class Walk:
    section_id: str
    persona_id: str
    arm: str
    scorecard: Scorecard = field(default_factory=Scorecard)
    turns: list[Turn] = field(default_factory=list)
    user_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    fatal: str | None = None

    @property
    def user_verdict_class(self) -> str:
        """`supply-blocked` is load-bearing (registry §4.4). Never 'disengaged'."""
        return "supply-blocked"

    def transcript(self) -> list[dict[str, Any]]:
        return [
            {
                "seq": t.seq,
                "sent": t.sent,
                "status": t.status,
                "assistant_message": t.assistant_message,
                "routing": t.routing,
                "ui_actions": [{"label": a.get("label"), "style": a.get("style"), "message": a.get("message")} for a in t.ui_actions],
                "preferred_language": t.preferred_language,
                "latency_ms": t.latency_ms,
                "error": t.error,
            }
            for t in self.turns
        ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SectionRunner:
    def __init__(
        self,
        cfg: Config,
        *,
        worker: WorkerClient,
        auth: AnonymousAuth,
        db: Db,
        registry: Registry,
        preflight: Preflight,
        personas_doc: dict[str, Any],
    ):
        self.cfg = cfg
        self.worker = worker
        self.auth = auth
        self.db = db
        self.registry = registry
        self.preflight = preflight
        self.personas_doc = personas_doc

    # ------------------------------------------------------------------ one walk

    def walk(self, persona: dict[str, Any], *, section_id: str, arm: str) -> Walk:
        pid = persona["persona_id"]
        w = Walk(section_id=section_id, persona_id=pid, arm=arm, started_at=_now())

        ev = Evaluator(
            registry=self.registry,
            persona=persona,
            db_columns=self.preflight.pinned_columns,
            proper_nouns=tuple(persona.get("allowed_places") or []),
        )

        try:
            identity = self.auth.sign_in_anonymously()
            w.user_id = identity.user_id

            # Register BEFORE the first write, so a crash is still sweepable.
            self._register_actor(identity, persona, section_id, arm)

            header = _accept_language(persona)
            create = self.worker.create_session(identity.jwt, accept_language=header)
            w.turns.append(create)
            if create.status != 200:
                w.scorecard.add(Result("session.create", "fail", f"HTTP {create.status}", "HTTP 200"))
                w.finished_at = _now()
                return w
            w.scorecard.add(Result("session.create", "pass", 200, 200))
            session_id = create.response.get("session_id")

            # The opener carries its own asserts and has no user turn before it.
            opener = persona.get("opening_utterance") or {}
            for r in ev.evaluate(create, opener.get("asserts", {}), self.db, identity.user_id, "t1"):
                w.scorecard.add(r)

            # Send the opener, then every subsequent utterance.
            utterances = [opener] + list(persona.get("utterances") or [])
            for idx, utt in enumerate(utterances):
                text = _utterance_text(utt, arm)
                if not text:
                    continue
                ev.note_persona_utterance(text)

                # PR #124 §4.2 caps an anonymous conversation at 12 turns.
                if idx + 1 > self.cfg.anonymous_turn_cap:
                    w.scorecard.add(
                        Result(
                            f"t{utt.get('seq', idx + 1)}.turn_cap",
                            BLOCKED,
                            idx + 1,
                            f"<= {self.cfg.anonymous_turn_cap}",
                            delta_id="D-23",
                            note="beyond the anonymous turn cap — not walked",
                        )
                    )
                    break

                turn = self.worker.send_message(
                    identity.jwt,
                    session_id,
                    text,
                    seq=utt.get("seq", idx + 1),
                    accept_language=header,
                    arm=arm,
                )
                w.turns.append(turn)

                prefix = f"t{turn.seq}"
                for r in ev.evaluate(turn, utt.get("asserts", {}), self.db, identity.user_id, prefix):
                    w.scorecard.add(r)

                # Repeat-question check runs on every turn regardless of whether
                # the fixture asked for it — it is the defect Asjid reported and
                # it is free to observe.
                w.scorecard.add(
                    ev._c_no_repeat_question(turn, "no_repeat_question", True, utt.get("asserts", {}), self.db, identity.user_id, prefix)
                )

            # Completion is its own gate. D-12 blocks it today.
            self._maybe_complete(w, ev, identity, session_id, persona)

        except RailViolation:
            raise  # a rail violation must stop the whole run, not just this walk
        except Exception as exc:
            w.fatal = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
            w.scorecard.add(Result("walk.fatal", ERROR, str(exc), "walk completes"))

        w.finished_at = _now()
        return w

    def _maybe_complete(
        self, w: Walk, ev: Evaluator, identity: Identity, session_id: str, persona: dict[str, Any]
    ) -> None:
        """POST /complete with publish=false, or block it if D-12 is active."""
        if "D-12" in self.preflight.active_deltas:
            w.scorecard.add(
                Result(
                    "complete.http",
                    BLOCKED,
                    "not attempted",
                    "HTTP 200",
                    delta_id="D-12",
                    note="preflight G1 detected the 502 shape; calling /complete would fail for a "
                    "known reason and produce no completion signal",
                )
            )
            return
        turn = self.worker.complete_session(identity.jwt, session_id, seq=99)
        w.turns.append(turn)
        if turn.status != 200:
            w.scorecard.add(Result("complete.http", "fail", f"HTTP {turn.status}", "HTTP 200"))
            return
        w.scorecard.add(Result("complete.http", "pass", 200, 200))
        # C06 / step 12: published must be false and no event may appear.
        w.scorecard.add(
            Result(
                "complete.published_false",
                "pass" if turn.response.get("published") is False else "fail",
                turn.response.get("published"),
                False,
            )
        )
        if persona.get("locale") in ("es", "pt"):
            from . import language

            summary = turn.response.get("mapped_summary") or ""
            n = language.english_sentence_count(summary, proper_noun_allowlist=tuple(persona.get("allowed_places") or []))
            w.scorecard.add(
                Result("complete.mapped_summary_lang", "pass" if n == 0 else "fail", n, 0)
            )

    # ------------------------------------------------------------------ plumbing

    def _register_actor(self, identity: Identity, persona: dict, section_id: str, arm: str) -> None:
        if self.cfg.dry_run:
            return
        self.db.insert(
            "swarm_run_actors",
            [
                {
                    "run_id": self.cfg.run_id,
                    "user_id": identity.user_id,
                    "persona_id": persona["persona_id"],
                    "section_id": section_id,
                    "arm": arm,
                    "is_manifest_account": False,
                }
            ],
            upsert=True,
        )

    def sink(self, w: Walk) -> None:
        """One `simulations` row per (run_id, section_id, persona_id, arm)."""
        if self.cfg.dry_run:
            return
        sc = w.scorecard
        self.db.insert(
            "simulations",
            [
                {
                    "run_id": self.cfg.run_id,
                    "section_id": w.section_id,
                    "persona_id": w.persona_id,
                    "arm": w.arm,
                    "git_sha": self.cfg.git_sha,
                    "spec_version": SPEC_VERSION,
                    "seed_label": f"{w.section_id}:{w.persona_id}:{w.arm}",
                    "bucket": w.user_verdict_class,
                    "transcript_json": w.transcript(),
                    "assertions_json": [r.as_json() for r in sc.results],
                    "score": sc.score,
                    "verdict": sc.verdict,
                    "passed_count": sc.passed,
                    "failed_count": sc.failed,
                    "blocked_count": sc.blocked,
                    "error_count": sc.errored,
                    "delta_ids": sc.delta_ids,
                    "started_at": w.started_at,
                    "finished_at": w.finished_at,
                    "model_versions": {
                        "detector": self.preflight.detector,
                        "worker": self.preflight.worker_meta.get("health"),
                    },
                }
            ],
            upsert=True,
        )

    # --------------------------------------------------------------------- fanout

    def run_section(
        self, section_id: str, *, personas: list[dict[str, Any]], arms: list[str], max_workers: int = 3
    ) -> list[Walk]:
        """Personas run concurrently, bounded by the session rate limiter.

        `max_workers` defaults low on purpose: the limiter already throttles
        session creation to 3/min, so more threads just queue. It exists so a
        slow persona does not serialise the night.
        """
        walks: list[Walk] = []
        jobs: list[tuple[dict, str]] = [(p, a) for a in arms for p in personas]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.walk, p, section_id=section_id, arm=a): (p["persona_id"], a) for p, a in jobs}
            for fut in as_completed(futures):
                pid, arm = futures[fut]
                try:
                    w = fut.result()
                except RailViolation as exc:
                    # Cancel the rest: a rail violation means we may already have
                    # touched something we must not.
                    for f in futures:
                        f.cancel()
                    raise
                except Exception as exc:
                    w = Walk(section_id=section_id, persona_id=pid, arm=arm, started_at=_now(), finished_at=_now())
                    w.fatal = str(exc)
                    w.scorecard.add(Result("walk.fatal", ERROR, str(exc), "walk completes"))
                walks.append(w)
                self.sink(w)
        return walks


def _accept_language(persona: dict[str, Any]) -> str:
    """The only transport for language below the client.

    `accept-language` is accepted on 3 of 34 endpoints and is absent from
    /complete (D-08). Built from the persona's declared locale rather than a
    hardcoded table so a new locale needs no code change.
    """
    loc = persona.get("locale", "en")
    return {
        "en": "en-US,en;q=0.9",
        "es": "es-US,es;q=0.9",
        "pt": "pt-BR,pt;q=0.9",
    }.get(loc, "en-US,en;q=0.9")


def _utterance_text(utt: dict[str, Any], arm: str) -> str:
    """E-VOICE sends `voice_variant`; the other arms send `text`.

    personas.json#cross_cutting_arms: E-VOICE "use utterance.voice_variant in
    place of utterance.text", and is "expected to fail more; that is the
    measurement."
    """
    if arm == "E-VOICE":
        return utt.get("voice_variant") or utt.get("text") or ""
    return utt.get("text") or ""


def summarize(walks: list[Walk]) -> dict[str, Any]:
    """Nightly roll-up. Deltas are reported by frequency (registry §4.5)."""
    delta_freq: dict[str, int] = {}
    for w in walks:
        for did in w.scorecard.delta_ids:
            delta_freq[did] = delta_freq.get(did, 0) + 1

    scored = [w.scorecard.score for w in walks if w.scorecard.score is not None]
    body = {
        "walks": len(walks),
        "passed": sum(w.scorecard.passed for w in walks),
        "failed": sum(w.scorecard.failed for w in walks),
        "blocked": sum(w.scorecard.blocked for w in walks),
        "errored": sum(w.scorecard.errored for w in walks),
        "mean_score": (sum(scored) / len(scored)) if scored else None,
        "delta_frequency": dict(sorted(delta_freq.items(), key=lambda kv: -kv[1])),
        "per_walk": [
            {
                "section": w.section_id,
                "persona": w.persona_id,
                "arm": w.arm,
                "verdict": w.scorecard.verdict,
                "score": w.scorecard.score,
                "p/f/b/e": [w.scorecard.passed, w.scorecard.failed, w.scorecard.blocked, w.scorecard.errored],
                "fatal": w.fatal,
            }
            for w in sorted(walks, key=lambda x: (x.section_id, x.persona_id, x.arm))
        ],
    }

    # SPEC_P1_LANGUAGE.md §SCORE: "A run reporting zero blocked assertions is
    # itself suspect — it means the agent silently passed something it could not
    # have observed. Flag it."
    if body["blocked"] == 0 and body["walks"]:
        body["suspect"] = (
            "zero blocked assertions across the whole run. On the current prod build D-04, D-12 and "
            "D-13 are all active, so this almost certainly means something passed that was never "
            "observed. Do not trust this run."
        )
    return body
