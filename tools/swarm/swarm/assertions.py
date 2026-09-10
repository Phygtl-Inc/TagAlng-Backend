"""Assertion evaluation, and the four-verdict vocabulary.

`personas.json` declares per-utterance `asserts` using a vocabulary of ~58 keys.
This module turns each key into a machine-checkable verdict against the turn's
response and the database.

The verdict rules that matter, from `LANA_ZERO_BUG_PROGRAM_FINAL.md` §4 and
`KNOWN_DELTA_REGISTRY.md` §0:

    score = passed / (passed + failed)     # blocked and error are EXCLUDED

  * `blocked-by-known-delta` — the symptom matches a delta the fixture declared.
    Never a fail, never a bug.
  * `error` — the harness could not make the observation (HTTP 5xx we did not
    expect, a void language check, a missing column). Distinct from `fail`,
    because "we did not measure" is not "the product is wrong".
  * An assert key with no implementation returns `error` naming itself, never
    `pass`. Silently passing an unimplemented check is how a section reports
    green while testing nothing — `SPEC_P1_LANGUAGE.md` §SCORE flags a run with
    zero blocked assertions as "itself suspect" for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import language
from .registry import FORBIDDEN_VERDICT_CLASSES, Registry, declared_deltas

PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked-by-known-delta"
ERROR = "error"


@dataclass
class Result:
    id: str
    verdict: str
    observed: Any = None
    expected: Any = None
    delta_id: str | None = None
    note: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "verdict": self.verdict,
            "observed": _jsonable(self.observed),
            "expected": _jsonable(self.expected),
        }
        if self.delta_id:
            out["delta_id"] = self.delta_id
        if self.note:
            out["note"] = self.note
        return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


@dataclass
class Scorecard:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)

    def count(self, verdict: str) -> int:
        return sum(1 for r in self.results if r.verdict == verdict)

    @property
    def passed(self) -> int:
        return self.count(PASS)

    @property
    def failed(self) -> int:
        return self.count(FAIL)

    @property
    def blocked(self) -> int:
        return self.count(BLOCKED)

    @property
    def errored(self) -> int:
        return self.count(ERROR)

    @property
    def score(self) -> float | None:
        """None, not 0.0, when the denominator is zero.

        A section where every assertion was blocked has no score. Reporting 0.0
        would call it a total failure; on the current prod build all of P1 block
        C is blocked by D-12, so this is today's state, not a hypothetical.
        """
        denom = self.passed + self.failed
        return (self.passed / denom) if denom else None

    @property
    def verdict(self) -> str:
        if self.failed:
            return FAIL
        if self.passed:
            return PASS
        if self.blocked:
            return BLOCKED
        return ERROR

    @property
    def delta_ids(self) -> list[str]:
        return sorted({r.delta_id for r in self.results if r.delta_id})


class Evaluator:
    """Evaluates one utterance's `asserts` block against one turn."""

    def __init__(
        self,
        *,
        registry: Registry,
        persona: dict[str, Any],
        db_columns: dict[str, set[str]],
        proper_nouns: tuple[str, ...],
    ):
        self.registry = registry
        self.persona = persona
        self.db_columns = db_columns
        self.proper_nouns = proper_nouns
        self.locale = persona.get("locale", "en")
        self._questions_seen: list[str] = []
        self._said_by_persona: list[str] = []

    # ------------------------------------------------------------------ helpers

    def note_persona_utterance(self, text: str) -> None:
        """Track what the persona said, so a proper noun or a gendered term the
        USER introduced is not scored against Lana (§CLASSIFIER step 1, G06).
        """
        self._said_by_persona.append(text)

    def _allowlist(self) -> tuple[str, ...]:
        extra: list[str] = list(self.proper_nouns)
        for said in self._said_by_persona:
            extra.extend(w for w in said.split() if len(w) > 3 and w[0].isupper())
        return tuple(extra)

    def _blocked_or_fail(self, aid: str, asserts: dict, observed: Any, expected: Any) -> Result:
        """A failed observation becomes BLOCKED when the fixture declared a delta
        that covers it, otherwise FAIL.

        Matching is on the fixture's own `expected_delta*` declarations, not on
        prose similarity against the registry — see registry.declared_deltas.
        """
        for did in declared_deltas(self.persona, asserts):
            if did in self.registry:
                return Result(aid, BLOCKED, observed, expected, delta_id=did)
        return Result(aid, FAIL, observed, expected)

    # ---------------------------------------------------------------- the checks

    def evaluate(self, turn: Any, asserts: dict[str, Any], db: Any, user_id: str, prefix: str) -> list[Result]:
        out: list[Result] = []

        # An unexpected 5xx voids every other observation on this turn: there is
        # no assistant_message to classify and no row to read.
        if turn.status >= 500:
            aid = f"{prefix}.http"
            if asserts.get("expect_no_500"):
                out.append(self._blocked_or_fail(aid, asserts, f"HTTP {turn.status}", "not 5xx"))
            else:
                out.append(
                    self._blocked_or_fail(aid, asserts, f"HTTP {turn.status}", "HTTP 200")
                )
            return out
        if turn.status != 200:
            out.append(self._blocked_or_fail(f"{prefix}.http", asserts, f"HTTP {turn.status}", "HTTP 200"))
            return out
        out.append(Result(f"{prefix}.http", PASS, 200, 200))

        for key, expected in asserts.items():
            handler = self._HANDLERS.get(key)
            if handler is None:
                # Deliberately not a pass. See module docstring.
                out.append(
                    Result(
                        f"{prefix}.{key}",
                        ERROR,
                        None,
                        expected,
                        note=f"assert key '{key}' has no implementation in assertions.Evaluator",
                    )
                )
                continue
            try:
                res = handler(self, turn, key, expected, asserts, db, user_id, prefix)
            except Exception as exc:  # a harness bug is an error, never a product fail
                res = Result(f"{prefix}.{key}", ERROR, f"{type(exc).__name__}: {exc}", expected)
            if res is not None:
                out.extend(res if isinstance(res, list) else [res])
        return out

    # Each handler: (self, turn, key, expected, asserts, db, user_id, prefix) -> Result | list | None

    def _c_response_lang(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        rep = language.classify(turn.assistant_message, proper_noun_allowlist=self._allowlist())
        if rep.void:
            return Result(
                aid, ERROR, {"short_ratio": round(rep.short_ratio, 2)}, expected,
                note="§CLASSIFIER: >34% of sentences too short to classify — the check is void",
            )
        target = expected if isinstance(expected, str) else self.locale
        ratio = rep.target_ratio(target)
        observed = {"classified": rep.classified, "ratio": ratio}
        if ratio == 1.0:
            return Result(aid, PASS, observed, {"ratio": 1.0, "lang": target})
        return self._blocked_or_fail(aid, asserts, observed, {"ratio": 1.0, "lang": target})

    def _c_response_lang_allowed(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        allowed = expected if isinstance(expected, list) else [expected]
        rep = language.classify(turn.assistant_message, proper_noun_allowlist=self._allowlist())
        if rep.void:
            return Result(aid, ERROR, {"short_ratio": round(rep.short_ratio, 2)}, allowed)
        off = {k: v for k, v in rep.classified.items() if k not in allowed}
        if not off:
            return Result(aid, PASS, rep.classified, allowed)
        return self._blocked_or_fail(aid, asserts, rep.classified, allowed)

    def _c_no_english_leak(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        if expected is False:  # EN personas declare no_english_leak: false
            return Result(aid, PASS, "n/a", False, note="EN persona — no leak to check")
        n = language.english_sentence_count(turn.assistant_message, proper_noun_allowlist=self._allowlist())
        if n == 0:
            return Result(aid, PASS, 0, 0)
        return self._blocked_or_fail(aid, asserts, n, 0)

    def _c_no_spanish_leak(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        rep = language.classify(turn.assistant_message, proper_noun_allowlist=self._allowlist())
        n = rep.count("es")
        return Result(aid, PASS, 0, 0) if n == 0 else self._blocked_or_fail(aid, asserts, n, 0)

    def _c_forbidden_phrases(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        phrases = expected if isinstance(expected, list) else [expected]
        low = (turn.assistant_message or "").lower()
        said = " ".join(self._said_by_persona).lower()
        hits = [p for p in phrases if isinstance(p, str) and p.lower() in low and p.lower() not in said]
        return Result(aid, PASS, [], phrases) if not hits else self._blocked_or_fail(aid, asserts, hits, [])

    def _c_capture_fired(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        got = bool(turn.routing.get("capture_fired"))
        return Result(aid, PASS, got, expected) if got == bool(expected) else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_outcome_not(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        banned = expected if isinstance(expected, list) else [expected]
        got = turn.routing.get("outcome")
        return Result(aid, PASS, got, f"not in {banned}") if got not in banned else self._blocked_or_fail(aid, asserts, got, f"not in {banned}")

    def _c_db_write(self, turn, key, expected, asserts, db, user_id, prefix):
        """D-16: 20 of 34 endpoints return untyped `{}`, so the row IS the
        assertion surface. D-22: `circle_affiliations` has RLS on with 0
        policies, so this must be a service-role read.
        """
        aid = f"{prefix}.{key}"
        table = expected
        rows = db.select(table, **{"user_id": f"eq.{user_id}"}) if table else []
        if rows:
            return Result(aid, PASS, {"table": table, "rows": len(rows)}, {"table": table, "rows": ">=1"})
        return self._blocked_or_fail(aid, asserts, {"table": table, "rows": 0}, {"table": table, "rows": ">=1"})

    def _c_expected_status(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        table = asserts.get("expect_db_write")
        if not table:
            return Result(aid, ERROR, None, expected, note="expected_status without expect_db_write")
        rows = db.select(table, **{"user_id": f"eq.{user_id}"})
        got = sorted({r.get("status") for r in rows if r.get("status")})
        return Result(aid, PASS, got, expected) if expected in got else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_intent(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        rows = db.select("local_signals", **{"user_id": f"eq.{user_id}"})
        got = sorted({r.get("intent") for r in rows if r.get("intent")})
        return Result(aid, PASS, got, expected) if expected in got else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_claim_bucket(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        buckets = sorted({c.get("bucket") for c in db.claims(user_id) if c.get("bucket")})
        return Result(aid, PASS, buckets, expected) if expected in buckets else self._blocked_or_fail(aid, asserts, buckets, expected)

    def _c_ui_present(self, turn, key, expected, asserts, db, user_id, prefix):
        """Lana never dead-ends. P0 C04 / P1 X5b: len(ui_actions) >= 1, and every
        style is in the only real enum the API has.
        """
        aid = f"{prefix}.{key}"
        actions = turn.ui_actions
        if not actions:
            return self._blocked_or_fail(aid, asserts, 0, ">=1 ui_action")
        bad = [a.get("style") for a in actions if a.get("style") not in ("primary", "secondary", "ghost")]
        if bad:
            return Result(aid, FAIL, {"bad_styles": bad}, "style in {primary,secondary,ghost}")
        return Result(aid, PASS, len(actions), ">=1 ui_action")

    def _c_gender_write(self, turn, key, expected, asserts, db, user_id, prefix):
        """CONDITIONAL (P1 G08). Pinned at run start: `users.grammatical_gender`
        DOES exist on prod. If a future schema drops it, this is blocked.
        """
        aid = f"{prefix}.{key}"
        if "grammatical_gender" not in self.db_columns.get("users", set()):
            return Result(aid, BLOCKED, "column absent", expected, delta_id="D-18",
                          note="users.grammatical_gender not present — blocked, not failed")
        row = db.user_row(user_id) or {}
        got = row.get("grammatical_gender")
        if got == expected:
            return Result(aid, PASS, got, expected)
        if got is None:
            # D-18 covers "nothing written". It explicitly does NOT cover the
            # wrong thing written — that boundary is in the registry.
            return Result(aid, BLOCKED, None, expected, delta_id="D-18",
                          note="grammatical_gender NULL after an unambiguous signal — D-18 covers nothing-written")
        return Result(aid, FAIL, got, expected, note="wrong value written — D-18 does not cover this")

    def _c_role_write(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        if "role" not in self.db_columns.get("users", set()):
            return Result(aid, BLOCKED, "column absent", expected, delta_id="D-18")
        got = (db.user_row(user_id) or {}).get("role")
        if got == expected:
            return Result(aid, PASS, got, expected)
        if got is None:
            return Result(aid, BLOCKED, None, expected, delta_id="D-18")
        # Registry D-18 boundary: caregiver coerced to parent, or grandparent
        # coerced to parent, ARE real bugs. File them.
        return Result(aid, FAIL, got, expected,
                      note=f"role coerced to {got!r} instead of {expected!r} — a real bug per D-18's boundary clause")

    def _c_gender_agreement(self, turn, key, expected, asserts, db, user_id, prefix):
        """§GENDER G01/G03: zero masculine-agreement tokens for a feminine user."""
        aid = f"{prefix}.{key}"
        toks = language.gender_tokens(turn.assistant_message, self.locale)
        if self.persona.get("grammatical_gender") == "feminine" and toks["masculine"]:
            return self._blocked_or_fail(aid, asserts, toks, {"masculine": []})
        return Result(aid, PASS, toks, {"masculine": []})

    def _c_no_parent_assumption(self, turn, key, expected, asserts, db, user_id, prefix):
        """G05: a caregiver or grandparent must not be addressed as the parent."""
        aid = f"{prefix}.{key}"
        phrases = ("tu hijo", "tu hija", "tus hijos", "your kids", "your son", "your daughter",
                   "seu filho", "sua filha", "seus filhos")
        low = (turn.assistant_message or "").lower()
        said = " ".join(self._said_by_persona).lower()
        hits = [p for p in phrases if p in low and p not in said]
        return Result(aid, PASS, [], []) if not hits else self._blocked_or_fail(aid, asserts, hits, [])

    def _c_no_feeling_claim(self, turn, key, expected, asserts, db, user_id, prefix):
        """X3 / T6: Lana never claims feelings, experiences or preferences.

        Deliberately narrow: only first-person emotional/experiential predicates.
        "I noticed" and "I remember" are permitted — they are exactly what the
        locked C+D position says she MAY disclose.
        """
        aid = f"{prefix}.{key}"
        claims = (
            "i feel", "i felt", "i'm feeling", "im feeling", "i love", "i loved", "i hate",
            "i enjoy", "i enjoyed", "i miss", "i'm excited", "im excited", "i'm happy",
            "i'm sad", "i prefer", "my favorite", "my favourite", "i want to", "i wish",
            "me siento", "me encanta", "me gusta", "prefiero", "mi favorito", "mi favorita",
            "eu sinto", "eu adoro", "eu gosto", "prefiro", "meu favorito", "minha favorita",
        )
        low = (turn.assistant_message or "").lower()
        hits = [c for c in claims if c in low]
        return Result(aid, PASS, [], []) if not hits else Result(aid, FAIL, hits, [],
                      note="Lana claimed a feeling/preference of her own — X3 guardrail")

    def _c_lingo(self, turn, key, expected, asserts, db, user_id, prefix):
        """G07: grandparent lingo present, parent lingo absent."""
        aid = f"{prefix}.{key}"
        want = expected if isinstance(expected, list) else [expected]
        low = (turn.assistant_message or "").lower()
        found = [w for w in want if isinstance(w, str) and w.lower() in low]
        return Result(aid, PASS, found, want) if found else self._blocked_or_fail(aid, asserts, found, want)

    def _c_preferred_language(self, turn, key, expected, asserts, db, user_id, prefix):
        """The locale echo — see worker.Turn.preferred_language on why the specs
        are wrong to say no echo exists.
        """
        aid = f"{prefix}.{key}"
        got = turn.preferred_language
        if got is None:
            return Result(aid, ERROR, None, expected, note="no preferred_language on this response shape")
        return Result(aid, PASS, got, expected) if got == expected else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_no_repeat_question(self, turn, key, expected, asserts, db, user_id, prefix):
        """P0 B10 — Asjid's repeat-question defect."""
        aid = f"{prefix}.{key}"
        qs = language.normalized_questions(turn.assistant_message)
        dupes = [q for q in qs if q in self._questions_seen]
        self._questions_seen.extend(qs)
        return Result(aid, PASS, [], []) if not dupes else self._blocked_or_fail(aid, asserts, dupes, [])

    def _c_record_only(self, turn, key, expected, asserts, db, user_id, prefix):
        """Keys the fixtures use to record context rather than assert on it.

        `note`, `assertion_id` and the `expected_delta*` family are metadata; the
        delta ids are consumed by _blocked_or_fail, not scored on their own.
        """
        return None

    # ---- mechanically checkable DB / response shape --------------------------

    def _c_expected_block_id(self, turn, key, expected, asserts, db, user_id, prefix):
        """The signal must be scoped to the persona's own block, not the cluster."""
        aid = f"{prefix}.{key}"
        rows = db.select("local_signals", **{"user_id": f"eq.{user_id}"})
        got = sorted({r.get("block_id") for r in rows if r.get("block_id")})
        return Result(aid, PASS, got, expected) if expected in got else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_home_block(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        got = (db.user_row(user_id) or {}).get("home_block_id")
        return Result(aid, PASS, got, expected) if got == expected else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_circle_type(self, turn, key, expected, asserts, db, user_id, prefix):
        """D-22: RLS on with 0 policies, so this has to be the service-role read."""
        aid = f"{prefix}.{key}"
        rows = db.circle_affiliations(user_id)
        got = sorted({r.get("circle_type") for r in rows if r.get("circle_type")})
        return Result(aid, PASS, got, expected) if expected in got else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_circle_block(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        rows = db.circle_affiliations(user_id)
        got = sorted({r.get("block_id") for r in rows if r.get("block_id")})
        return Result(aid, PASS, got, expected) if expected in got else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_place_name(self, turn, key, expected, asserts, db, user_id, prefix):
        """The grounded place must be the one the persona named.

        Checked against `place_suggestions` on the turn and, once grounded, the
        `places` row behind the affiliation. D-21 (`places` nearly empty, so
        grounding falls through to a live Google lookup) makes a miss here a
        supply artefact rather than a routing bug when declared.
        """
        aid = f"{prefix}.{key}"
        offered = [s.get("name") for s in (turn.response.get("place_suggestions") or [])]
        refs = [r.get("place_ref") for r in db.circle_affiliations(user_id) if r.get("place_ref")]
        grounded: list[str] = []
        for ref in refs:
            rows = db.select("places", columns="name", **{"id": f"eq.{ref}"})
            grounded.extend(r.get("name") for r in rows if r.get("name"))
        names = [n for n in offered + grounded if n]
        want = (expected or "").lower()
        if any(want in (n or "").lower() or (n or "").lower() in want for n in names):
            return Result(aid, PASS, names, expected)
        return self._blocked_or_fail(aid, asserts, {"offered": offered, "grounded": grounded}, expected)

    def _c_grounding_offer(self, turn, key, expected, asserts, db, user_id, prefix):
        """A place mention must produce a groundable offer, not just prose."""
        aid = f"{prefix}.{key}"
        offered = turn.response.get("place_suggestions") or []
        actions = turn.ui_actions
        got = {"place_suggestions": len(offered), "ui_actions": len(actions)}
        if not expected:
            return Result(aid, PASS, got, expected)
        if offered or actions:
            return Result(aid, PASS, got, "a place suggestion or a ui_action")
        return self._blocked_or_fail(aid, asserts, got, "a place suggestion or a ui_action")

    def _c_capability_routing(self, turn, key, expected, asserts, db, user_id, prefix):
        """routing.tool_called. Dead until #120 backfills the embeddings (D-10)."""
        aid = f"{prefix}.{key}"
        got = turn.routing.get("tool_called")
        if bool(got) == bool(expected):
            return Result(aid, PASS, got, expected)
        return self._blocked_or_fail(aid, asserts, got, expected)

    def _c_expected_capability(self, turn, key, expected, asserts, db, user_id, prefix):
        aid = f"{prefix}.{key}"
        got = turn.routing.get("tool_called")
        return Result(aid, PASS, got, expected) if got == expected else self._blocked_or_fail(aid, asserts, got, expected)

    def _c_role_persists(self, turn, key, expected, asserts, db, user_id, prefix):
        """The role must not silently revert to `parent` later in the session —
        the D-18 boundary clause makes coercion a real bug.
        """
        aid = f"{prefix}.{key}"
        if "role" not in self.db_columns.get("users", set()):
            return Result(aid, BLOCKED, "column absent", expected, delta_id="D-18")
        got = (db.user_row(user_id) or {}).get("role")
        want = expected if isinstance(expected, str) else self.persona.get("expected_role_inference")
        if got == want:
            return Result(aid, PASS, got, want)
        if got is None:
            return Result(aid, BLOCKED, None, want, delta_id="D-18")
        return Result(aid, FAIL, got, want, note=f"role is {got!r}, expected {want!r} to persist")

    def _c_publish_flag(self, turn, key, expected, asserts, db, user_id, prefix):
        """complete_publish_flag — asserts the rail held, from the response itself."""
        aid = f"{prefix}.{key}"
        got = turn.response.get("published")
        if got is None:
            return Result(aid, ERROR, None, expected, note="no `published` on this response shape")
        return Result(aid, PASS, got, False) if got is False else Result(
            aid, FAIL, got, False, note="a real event may have been published despite publish=false"
        )

    def _c_unimplementable(self, turn, key, expected, asserts, db, user_id, prefix):
        """Keys that need a surface the worker API does not expose.

        Honest `error` with a reason beats a fabricated pass. `SPEC_P1_LANGUAGE.md`
        K1-5/K1-6 make the same call for the voice block: not machine-checkable
        here, do not attempt.
        """
        return Result(
            f"{prefix}.{key}", ERROR, None, expected,
            note=f"'{key}' is not observable from the worker API — needs a browser driver or a human judgement",
        )

    _HANDLERS: dict[str, Callable[..., Any]] = {}


# Wire the table after the class body so the methods exist.
Evaluator._HANDLERS = {
    "response_lang": Evaluator._c_response_lang,
    "complete_response_lang": Evaluator._c_response_lang,
    "response_lang_allowed": Evaluator._c_response_lang_allowed,
    "no_english_leak": Evaluator._c_no_english_leak,
    "no_spanish_leak": Evaluator._c_no_spanish_leak,
    "forbidden_phrases": Evaluator._c_forbidden_phrases,
    "forbidden_phrases_in_summary": Evaluator._c_forbidden_phrases,
    "forbidden_framing": Evaluator._c_forbidden_phrases,
    "forbidden_claim": Evaluator._c_forbidden_phrases,
    "turn_routing.capture_fired": Evaluator._c_capture_fired,
    "turn_routing.outcome_not": Evaluator._c_outcome_not,
    "expect_db_write": Evaluator._c_db_write,
    "expected_status": Evaluator._c_expected_status,
    "expected_intent": Evaluator._c_expected_intent,
    "claim_bucket_expected": Evaluator._c_claim_bucket,
    "expect_no_ui": Evaluator._c_ui_present,
    "expected_gender_write": Evaluator._c_gender_write,
    "expected_role_write": Evaluator._c_role_write,
    "gender_signal": Evaluator._c_gender_agreement,
    "preserve_register": Evaluator._c_gender_agreement,
    "no_parent_assumption": Evaluator._c_no_parent_assumption,
    "no_partner_assumption": Evaluator._c_no_parent_assumption,
    "no_feeling_claim_by_lana": Evaluator._c_no_feeling_claim,
    "guardrail": Evaluator._c_no_feeling_claim,
    "expected_lingo": Evaluator._c_lingo,
    "expected_block_id": Evaluator._c_expected_block_id,
    "expected_home_block_id": Evaluator._c_expected_home_block,
    "expected_circle_type": Evaluator._c_expected_circle_type,
    "expected_circle_block_id": Evaluator._c_expected_circle_block,
    "expected_place_name": Evaluator._c_expected_place_name,
    "expect_circle_grounding_offer": Evaluator._c_grounding_offer,
    "expect_capability_routing": Evaluator._c_capability_routing,
    "expected_capability": Evaluator._c_expected_capability,
    "expected_role_persists": Evaluator._c_role_persists,
    "complete_publish_flag": Evaluator._c_publish_flag,
    # Not observable from the worker API — these emit `error` with a reason
    # rather than a fabricated pass. Each needs either a semantic judgement
    # (is this promise honest?) or a surface the API does not expose.
    # SPEC_P1_LANGUAGE.md K1-5/K1-6 make the same call for the voice block.
    "expect_no_false_promise": Evaluator._c_unimplementable,
    "expect_wrap": Evaluator._c_unimplementable,
    "expect_honest_supply_answer": Evaluator._c_unimplementable,
    "expect_honest_waitlist_framing": Evaluator._c_unimplementable,
    "expect_give_before_ask": Evaluator._c_unimplementable,
    "expect_host_path_offered": Evaluator._c_unimplementable,
    "expect_invite_path": Evaluator._c_unimplementable,
    "expect_threshold_disclosure": Evaluator._c_unimplementable,
    "expected_threshold_value": Evaluator._c_unimplementable,
    "aggregate_disclosure_floor_n": Evaluator._c_unimplementable,
    "expected_cohort_affinity": Evaluator._c_unimplementable,
    "expected_block_scope": Evaluator._c_unimplementable,
    "expected_endpoint": Evaluator._c_unimplementable,
    # Record-only metadata.
    "note": Evaluator._c_record_only,
    "assertion_id": Evaluator._c_record_only,
    "expected_delta": Evaluator._c_record_only,
    "expected_delta_if_absent": Evaluator._c_record_only,
    "expected_delta_if_empty": Evaluator._c_record_only,
    "expected_delta_if_english": Evaluator._c_record_only,
    "expected_delta_if_generic": Evaluator._c_record_only,
    "expected_delta_if_no_ui": Evaluator._c_record_only,
    "expected_delta_if_nonlocal_results": Evaluator._c_record_only,
    "grace_turn": Evaluator._c_record_only,
    "expect_no_500": Evaluator._c_record_only,  # consumed by the 5xx short-circuit
}
