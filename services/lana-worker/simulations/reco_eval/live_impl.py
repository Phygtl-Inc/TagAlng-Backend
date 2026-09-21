"""
live_impl.py — the REAL question-set generation, two ways.

WHAT EACH BACKEND CAN AND CANNOT SEE
------------------------------------
                              inproc                          live (HTTP)
  the model's raw proposal    REAL (`steps_raw`)              never — validate_steps has run
  the validated set           REAL                            REAL (SendMessageResponse.tip_draft)
  pre-filled answers          REAL (extractor `answers`)      REAL (RecoStep.answer)
  static-fallback detection   REAL (raw is empty / set==static)  inferred from the set alone
  the `others_also_said` row  never (no DB -> no tallies)     REAL when the block has tallies
  the ordering hazard (q11)   REAL — `prior_draft` is honoured  not drivable: the flow decides
                                                              its own turn order
  cost                        1 LLM call                      a session + 2-4 turns of Lana

`inproc` is the one to run by default. It calls the same two functions the product calls, in
the same order, and it is the only backend that can see what the model actually proposed
BEFORE the guards repaired it — which is the difference between "Lana wrote a good set" and
"Lana forgot the phone number and validate_steps put it back".

`live` exists because it is the only proof that what this harness measures is what a neighbour
receives: it exercises the router, the entry backstop, the name/category gates, the Places
lookup and the real tallies. It is slower, needs a worker and an account, and cannot pin the
draft state a fixture asks for — so it is the nightly backend, not the gate one.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ports import GeneratedSet, QuestionFixture, QuestionSetPort

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")

# What tip_share.py says on the turn that opens the flow (tip_share.py:562). Used as the
# assistant half of the minimal history handed to the extractor, so the conversation it reads
# is shaped like the real one rather than a bare user string.
_ENTRY_UTTERANCE = "Love that — what do you want to recommend?"


def detect_fallback(
    steps: list[dict[str, Any]], raw: Any, reco_type: str | None
) -> tuple[bool, str]:
    """Did Lana WRITE this set, or is it the type's static seven?

    Returns (generated, why). Two independent signals, because either alone is wrong:

      * `raw` empty  -> validate_steps took its fallback branch by definition
        (reco_question_sets.py: `if not middle:` -> the static set). Decisive when it fires.
      * the middle's field list EQUALS the static set's -> the same outcome by a different
        route. Needed because `raw` can be non-empty and still produce nothing usable: every
        proposed step can be dropped for a missing "?", a blocked ask or a duplicate key, and
        the fallback then fires with `raw` looking healthy.

    A model that independently reproduced the static set field-for-field would read as a
    fallback here. That is the right call anyway — it IS the static set.
    """
    if not raw:
        return False, "the extractor proposed no steps, so validate_steps used the static set"
    try:
        from app.reco_question_sets import TAIL_FIELDS, steps_for
    except Exception:  # noqa: BLE001 — live mode may run without the app importable
        return True, "steps proposed by the extractor"
    static = [s["field"] for s in steps_for(reco_type)]
    middle = [str(s.get("field")) for s in steps if str(s.get("field")) not in TAIL_FIELDS]
    if static and middle == static:
        return False, (
            "every proposed step was dropped by the guards — the final set is the type's "
            "static fallback field-for-field"
        )
    return True, "steps proposed by the extractor and kept by the guards"


class _ExtractFailureWatcher:
    """Catches `tip_share_extract_failed` on app.tip_share's logger for the duration of a call.

    Needed because `_extract_tip_fields` wraps its whole body in `except Exception` and returns
    `({}, None)` (tip_share.py:191-195). A dead API key, a 401, a timeout, a JSON parse failure
    and "the model genuinely returned nothing" are therefore INDISTINGUISHABLE at the call
    site. Without this, a broken key would be scored as `generation_ran: HARD_FAIL` on every
    fixture — a false finding against Lana caused entirely by our own environment, which is
    exactly the class of mistake this suite has shipped before.
    """

    def __init__(self) -> None:
        self.records: list[str] = []
        self._handler = None

    def __enter__(self) -> _ExtractFailureWatcher:
        import logging

        watcher = self

        class _H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
                if record.exc_info:
                    watcher.records.append(f"{record.getMessage()}: {record.exc_info[1]!r}")
                else:
                    watcher.records.append(record.getMessage())

        self._handler = _H(level=logging.WARNING)
        logging.getLogger("app.tip_share").addHandler(self._handler)
        return self

    def __exit__(self, *exc: object) -> None:
        import logging

        if self._handler is not None:
            logging.getLogger("app.tip_share").removeHandler(self._handler)


class InProcQuestionSets:
    """The REAL generation path, called in-process. THE DEFAULT BACKEND.

    Needs OPENAI_API_KEY (and LANA_LLM_PROVIDER=openai, which is what .env.local sets) and
    NOTHING ELSE — no Supabase, no session, no running worker. Verified by tracing every
    import: `import app.tip_share` pulls only app.reco_question_sets and app.reply_compose,
    and the call adds only app.orchestrator.llm. Every DB-touching import in tip_share.py is
    function-local and off this path (app.local_signals:215, app.places:288). `validate_steps`
    is pure — it runs with every LLM and Supabase env var removed.

    TWO CALLS, BECAUSE THE PRODUCT MAKES TWO
    ----------------------------------------
    Call 1 writes the set. On that call `found["answers"]` is ALWAYS {} — with no `reco_type`
    in `prev`, `step_set_of(prev)` is empty and the payload's CURRENT TYPE FIELDS block reads
    literally "(type not known yet — return {} for answers)" (tip_share.py:151). So the
    "already answered" axis (§6) is not measurable from call 1, in either direction.

    Call 2 is the product's turn-2 behaviour: `_extract_tip_fields` runs every turn on the
    accumulated draft (tip_share.py:544-555), and once `step_set` is on the draft the model is
    given the real fields as `answers` targets. That is the call that back-fills what the
    neighbour already said. Skip it with `prefill_probe=False` and `stated_facts` comes back
    UNSCORED rather than passing on an axis nobody measured.

    WHICH MODEL IS UNDER TEST
    -------------------------
    `synthesizer_model()` (llm.py:68) — OPENAI_SYNTH_MODEL, gpt-4o in this repo's .env.local.
    The harness sets LANA_LLM_FALLBACK=0 before calling: both providers are configured here,
    so without the kill switch (llm.py:171) a flaky turn is silently re-served by Vertex
    gemini-2.5-pro and the report would name a model that did not produce the set.
    """

    name = "inproc"

    def __init__(self, *, prefill_probe: bool = True) -> None:
        self._prefill_probe = prefill_probe
        # Loud failure beats a silently cross-provider answer. Set here rather than in the
        # entry point so the guarantee travels with the backend that depends on it.
        os.environ.setdefault("LANA_LLM_FALLBACK", "0")

    def generate(self, fx: QuestionFixture) -> GeneratedSet:
        try:
            from app.reco_question_sets import validate_steps
            from app.tip_share import _extract_tip_fields
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"cannot import the product's generation path: {exc}")

        prev = dict(fx.prior_draft or {})
        history = [
            {"role": "assistant", "content": _ENTRY_UTTERANCE},
            {"role": "user", "content": fx.opening_line},
        ]
        with _ExtractFailureWatcher() as watcher:
            try:
                fields, _ask = _extract_tip_fields(
                    history=history, user_message=fx.opening_line, prev=prev, lang=fx.lang,
                )
            except Exception as exc:  # noqa: BLE001
                return GeneratedSet(error=f"_extract_tip_fields raised: {exc}")
            failures = list(watcher.records)

        # ({}, None) is an ERROR, never a score of zero. See _ExtractFailureWatcher.
        if not fields:
            detail = "; ".join(failures) if failures else (
                "no exception was logged, so the model returned an unusable object rather "
                "than the call failing"
            )
            return GeneratedSet(
                error=f"_extract_tip_fields returned ({{}}, None) — {detail}",
                flags=["extraction produced nothing at all: scored as an ERROR, not as a "
                       "generation failure, because tip_share.py:191-195 swallows a dead key, "
                       "a 401 and a timeout into the same empty return"],
            )
        if failures:
            # A partial result after a logged failure is still a result, but the report has to
            # carry the reason — a retry that fell back is not the same measurement.
            fields = dict(fields)

        merged = {**prev, **fields}
        reco_type = merged.get("reco_type")
        raw = fields.get("steps_raw")

        # tallies=() — no block and no DB here, so the "others also said" row is correctly
        # absent (§5: shown ONLY when neighbours have logged something; an empty one is a dead
        # card). Flagged, not silently omitted: an axis this backend cannot exercise must not
        # read as an axis that passed.
        try:
            steps = validate_steps(raw, reco_type, tallies=())
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"validate_steps raised: {exc}", reco_type=reco_type)

        generated, why = detect_fallback(steps, raw, reco_type)
        flags = ["`others_also_said` is not exercised on inproc — no block, so no tallies "
                 "(correct behaviour, but unmeasured)"]
        flags += [f"app.tip_share logged: {f}" for f in failures]
        if not generated:
            flags.append(f"static fallback: {why}")
        if prev.get("reco_type"):
            flags.append(
                "fixture pins `reco_type` in prior_draft, so the extractor was shown CURRENT "
                "TYPE FIELDS and _STEPS_SPEC told it to return [] for steps "
                "(tip_share.py:89, 151) — this is the ordering hazard, not a generation failure"
            )

        # THE SAVE-PATH HAZARD, observable here and nowhere else. tip_share.py:331 passes
        # `draft.get("reco_type")` to save_local_signal RAW — normalize_type is only ever
        # called inside steps_for/validate_steps. So "Professional" or "restaurants" produces a
        # perfectly good question set and then makes set_signal_reco raise `invalid_reco_type`
        # (20261118120000:89-93), which local_signals.py:154-161 swallows with a warning while
        # the user is told the tip posted. The whole reco_fields payload is discarded silently.
        try:
            from app.reco_question_sets import normalize_type

            if reco_type and normalize_type(reco_type) != reco_type:
                flags.append(
                    f"SAVE HAZARD: reco_type={reco_type!r} normalises to "
                    f"{normalize_type(reco_type)!r}. The question set is built from the "
                    f"normalised value but tip_share.py:331 saves the RAW one, and "
                    f"set_signal_reco rejects anything outside the seven lowercase literals "
                    f"— every captured answer would be dropped without the user being told."
                )
        except Exception:  # noqa: BLE001
            pass

        prefilled: dict[str, str] = {
            k: str(v) for k, v in (fields.get("answers") or {}).items()
        }
        prefill_measured = False
        if self._prefill_probe and steps and reco_type:
            # The product's turn 2, verbatim: the same extractor, on the accumulated draft,
            # now carrying the validated set (tip_share.py:544-555). This is the call that
            # back-fills what the neighbour already said.
            probe_prev = {**merged, "step_set": steps}
            probe_prev.pop("steps_raw", None)
            try:
                fields2, _ = _extract_tip_fields(
                    history=history, user_message=fx.opening_line, prev=probe_prev,
                    lang=fx.lang,
                )
                prefilled.update({k: str(v) for k, v in ((fields2 or {}).get("answers") or {}).items()})
                prefill_measured = bool(fields2)
            except Exception as exc:  # noqa: BLE001
                flags.append(f"prefill probe failed: {exc}")
        if not prefill_measured:
            flags.append(
                "PREFILL NOT MEASURED — `answers` is structurally {} on the generating turn "
                "(tip_share.py:151), so `stated_facts` is UNSCORED without the turn-2 probe"
            )
        return GeneratedSet(
            steps=steps,
            reco_type=reco_type,
            prefilled=prefilled,
            raw_steps=raw if isinstance(raw, list) else None,
            generated=generated,
            prefill_measured=prefill_measured,
            flags=flags,
        )


class LiveQuestionSets:
    """Drive the real tip_share flow over HTTP and read the set off the wire.

    The set is visible: SendMessageResponse.tip_draft (models.py:992) carries TipDraft.steps
    as RecoStep rows (models.py:661), each with field / label / question / kind / placeholder /
    options / required / answer. So everything Arm A checks is observable here except the
    model's raw proposal, which validate_steps has already consumed by the time anything
    leaves the process.

    # FLAGGED: `prior_draft` cannot be honoured. The live flow builds its own draft from the
    # conversation, so a fixture that pins a half-filled draft (q11's ordering hazard) is
    # scored against whatever state the real turns produced. run_eval marks those UNSCORED
    # rather than pretending — see the `honoured_prior_draft` flag below.
    """

    name = "live"

    def __init__(self, *, email: str | None = None, timeout: float = 90.0) -> None:
        self._email = email or os.environ.get("SIM_RECO_EMAIL") or ""
        self._timeout = timeout
        self._jwt: str | None = None

    def _auth(self) -> str:
        if self._jwt:
            return self._jwt
        if not self._email:
            raise RuntimeError(
                "live backend needs an account: set SIM_RECO_EMAIL to one of the seeded sim "
                "personas (see simulations/personas.json)"
            )
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from sim_auth import jwt_for  # noqa: PLC0415 — the sim package is only on sys.path now

        self._jwt = jwt_for(self._email)
        return self._jwt

    def generate(self, fx: QuestionFixture) -> GeneratedSet:
        try:
            jwt = self._auth()
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"auth failed: {exc}")

        headers = {"Authorization": f"Bearer {jwt}"}
        flags: list[str] = []
        if fx.prior_draft:
            flags.append(
                "prior_draft NOT honoured on live — the flow builds its own draft from the "
                "conversation. Any expectation that depends on the pinned draft is unscorable."
            )
        try:
            with httpx.Client(timeout=self._timeout) as client:
                # purpose="lana", NOT "profile_intake": the intake purpose 500s with
                # `resumed_session_empty` on an account that already has history, which every
                # sim persona does. force_new because a resumed session carries the previous
                # run's tip_draft, and the capture would start half-filled.
                #
                # force_new=True calls abandon_other_active_sessions (app/db.py:340,363), so
                # two concurrent runs against one persona kill each other. Safe on a local
                # stack; in CI the `lana-sim-accounts` concurrency group serialises it.
                r = client.post(f"{LANA_BASE_URL}/lana/sessions", headers=headers,
                                json={"purpose": "lana", "force_new": True})
                r.raise_for_status()
                session_id = (r.json() or {}).get("session_id") or (r.json() or {}).get("id")
                if not session_id:
                    return GeneratedSet(error=f"no session_id in {r.text[:200]}")

                # Turn 1 opens the flow: _ENTRY_RE matches "recommend" and the router hands the
                # turn to tip_share (tip_share.py:40). Turn 2 carries the subject, which is what
                # gets past the name/category gates so the set is written (tip_share.py:604).
                draft: dict[str, Any] = {}
                for msg in ("I've got a tip to share", fx.opening_line):
                    resp = client.post(
                        f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
                        headers=headers, json={"message": msg},
                    )
                    resp.raise_for_status()
                    body = resp.json() or {}
                    if body.get("tip_draft"):
                        draft = body["tip_draft"]
                    if (draft.get("steps") or []):
                        break
                try:
                    client.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/complete",
                                headers=headers, json={})
                except Exception:  # noqa: BLE001 — cleanup must not fail the measurement
                    pass
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"live turn failed: {exc}", flags=flags)

        steps = list(draft.get("steps") or [])
        if not steps:
            return GeneratedSet(
                error="the flow produced no question set in two turns — it may not have "
                      "entered tip_share, or the type was never identified",
                reco_type=draft.get("reco_type"), flags=flags,
            )
        generated, why = detect_fallback(steps, steps, draft.get("reco_type"))
        if not generated:
            flags.append(f"static fallback: {why}")
        flags.append("raw model proposal is not observable over HTTP — validate_steps has "
                     "already consumed it (# FLAGGED adapter lossiness, not a defect)")
        return GeneratedSet(
            steps=steps,
            reco_type=draft.get("reco_type"),
            prefilled={
                str(s.get("field")): str(s.get("answer"))
                for s in steps if str(s.get("answer") or "").strip()
            },
            raw_steps=None,
            generated=generated,
            flags=flags,
        )


__all__ = ["InProcQuestionSets", "LiveQuestionSets", "detect_fallback", "QuestionSetPort"]
