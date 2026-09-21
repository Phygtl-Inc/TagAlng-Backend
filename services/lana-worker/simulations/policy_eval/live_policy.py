"""
live_policy.py — the two adapters onto REAL Lana: `inproc` (the policy itself) and `live`
(the whole HTTP pipeline).

WHY TWO, AND WHY `inproc` IS A SEPARATE BACKEND KIND RATHER THAN A MODE OF `live`
--------------------------------------------------------------------------------
They observe different things, need different credentials, and — crucially — need different
honesty exemptions in checks.py. Folding them together would mean every check branching on a
hidden sub-flag while still calling itself "live", which is exactly how an exemption becomes
invisible. So `SIM_BACKEND=inproc` is its own kind, and `backend_kind` stays the single string
every check reads.

    kind      what it drives                       kind/why/defer   chips   world can be pinned
    --------  -----------------------------------  ---------------  ------  -------------------
    inproc    app.policy.decide.decide_turn()      REAL             labels  only if injected
    live      POST /lana/sessions/{id}/messages    NOT VISIBLE      labels  no

`live` is deliberately NOT deleted. It is the only backend that exercises what the user
actually receives: the lingo guardrail's applied output, ui_actions as rendered, session
plumbing, and — importantly — the LEGACY path that answers whenever decide_turn returns None.
It also needs nothing but an account (no repo import path, no service-role key), so it is the
one that runs from a laptop or a CI box without DB privileges.

WHAT `inproc` FIXED (the three lies this module used to tell)
-------------------------------------------------------------
1. `why` is REAL — app/policy/decide.py:56, surfaced by routing_dict():67. The blanket
   "runtime emits no rationale, auto-exempt the schema check" is gone for inproc.
2. `kind` is REAL — decide.py:38, one of KINDS (decide.py:31). `_kind_from_routing`'s
   heuristic is no longer used for inproc; it survives ONLY for the HTTP adapter, which
   genuinely cannot see the field.
3. `defer_goal_id` / `distress_turn` are REAL — decide.py:42/55. `distress_turn` in
   particular is why checks.check_chips can now exempt distress turns instead of
   false-positiving on them (_apply_distress_gate at decide.py:331 CLEARS chips on purpose).

WHAT `inproc` STILL CANNOT SEE (# FLAGGED, not fixed)
-----------------------------------------------------
* TYPED CHIP ACTIONS. The real chips are `list[dict[str, str]]` of {label, send}
  (decide.py:39, parse_next_action:269-278) — there is no typed `action` enum in the
  shipped system at all. Chip COUNT and chip LABEL text are fully observable (and are
  checked); the NOT_NOW/escape sub-check is not, and is reported as unobservable rather
  than passed vacuously. NOTE the real parser also truncates to 3 chips (decide.py:272),
  so the ">4 chips" rule can never fire against a real decision.
* `tool`. The real NextAction has no tool field. What it has is `goal_id`, and a capability
  goal's id is "cap:<capability_id>" (goals.py:381). We recover the capability from that
  prefix, which is exact when the model sets goal_id — and audit_offer_goal (decide.py:339)
  exists precisely because it sometimes does not. A bridge_offer with no goal_id therefore
  reads as tool=None here; that is a real product observability gap, logged as
  `decide_turn_offer_without_goal`, not a harness bug.

THE WORLD PROBLEM — AND WHY IT CANNOT BE ALLOWED TO PASS
--------------------------------------------------------
decide_turn takes a `user_id`, not a world. It calls world_state(user_id) itself
(decide.py:511), which reads the DB. So a scenario that pins "quiet area, unverified, no
circle" runs against whatever the sim account's REAL area/verification/circles are. A
"quiet area -> seed, don't offer discovery" scenario evaluated against an OPEN account is
not a weaker test; it is a test of a different question whose PASS would be meaningless.

Both options the brief allows are implemented, and the DEFAULT is the fail-closed one:

  (b) DEFAULT — no injection. The backend reports world_source="account" plus the real
      state tokens it observed, and checks.check_world_fidelity returns UNSCORED. UNSCORED
      is worse than PASS in checks._worst and fails `--gate` in run_eval, so a scenario whose
      world was not honoured can never come out clean. If the account's real tokens happen to
      equal the scenario's, it PASSES with that stated as the reason — an accident that is
      recorded, not one that is hidden.

  (a) OPT-IN — SIM_INPROC_INJECT_WORLD=1 monkeypatches app.policy.world.world_state for the
      duration of the call, so the pinned world IS honoured. world_source="injected"; every
      report line says HARNESS-SUPPLIED WORLD. This is a harness-process patch, not an edit to
      app/ code, and it is narrow: goals, claims, capability rows and the LLM call all still
      run for real against the DB. It is off by default because a run whose world is fabricated
      must be an explicit choice by the person reading the report.

  (c) OPT-IN, LOCAL STACK ONLY (added 2026-08-20) — SIM_INPROC_SEED_WORLD=1 WRITES the pinned
      world for the real user (local_world.apply_world) before each decision, so every read
      decide_turn makes agrees, not only world_state(): goals, claims and capability rows see
      the same world as the area/verification snapshot, which injection cannot achieve.
      world_source="seeded", and the pin is READ BACK through world_state() — if the read-back
      disagrees, check_world_fidelity scores UNSCORED, so a seed that did not take cannot
      masquerade as control.

      This used to be ruled out here, and the reason is worth restating rather than deleting:
      the sim persona accounts live in the SHARED dev project, and seeding a contended account
      from an eval destroys other people's runs. That objection is about SHAREDNESS, not about
      seeding. local_world.py therefore refuses unless SUPABASE_URL is a local stack
      (local_guard.require_local), which is what simulations/LOCAL_STACK.md sets up. Against
      dev this option cannot be switched on by accident.

Env:
  both    LANA_BASE_URL / SUPABASE_URL / SUPABASE_ANON_KEY / SIM_LIVE_EMAIL, plus a credential:
          SIM_PASSWORD, or SUPABASE_SERVICE_ROLE_KEY for sim_auth's magic-link fallback.
  inproc  SUPABASE_SERVICE_ROLE_KEY (app.auth.service_client), an LLM provider key, and
          either SIM_INPROC_USER_ID or SIM_LIVE_EMAIL (resolved to a user id at login).
          Optional: SIM_INPROC_INJECT_WORLD=1, or SIM_INPROC_SEED_WORLD=1 (local stacks only,
          and it wins if both are set).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

from ports import Chip, NextAction, TurnContext, TurnNote, WorldState

# simulations/ — where the shared local_guard / sim_auth / local_world helpers live. policy_eval
# uses bare intra-package imports, so the parent dir has to be added explicitly.
_SIMS_DIR = Path(__file__).resolve().parents[1]
if str(_SIMS_DIR) not in sys.path:
    sys.path.insert(0, str(_SIMS_DIR))

from sim_auth import sign_in  # noqa: E402

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SIM_PASSWORD = os.environ.get("SIM_PASSWORD", "")
SIM_LIVE_EMAIL = os.environ.get("SIM_LIVE_EMAIL", "")

# services/lana-worker — the package root `app` lives under. Needed only by `inproc`.
WORKER_ROOT = Path(__file__).resolve().parents[2]


def _kind_from_routing(routing: dict[str, Any], event_draft: Any, has_actions: bool) -> str:
    """# FLAGGED heuristic, HTTP ADAPTER ONLY: the messages endpoint does not return the
    routing payload's `kind`, so infer a plausible NextAction.kind. NOT authoritative — and
    no longer used for anything else, since `inproc` reads the real field."""
    outcome = (routing or {}).get("outcome") or ""
    intent = (routing or {}).get("intent_class") or ""
    if outcome in ("decline", "out_of_scope", "clarify_out_of_scope"):
        return "reply"
    if event_draft:
        return "bridge_offer"
    if "clarify" in outcome or intent in ("ambiguous", "clarify"):
        return "ask_gap"
    if (routing or {}).get("tool_called"):
        return "bridge_offer"
    if has_actions:
        return "bridge_offer"
    return "reply"


def map_to_next_action(resp: dict[str, Any]) -> NextAction:
    """Pure mapping (unit-testable without a server): HTTP response JSON -> NextAction.
    Every field the response doesn't carry is None/""/CONTINUE and flagged above."""
    routing = resp.get("routing") or {}
    ui_actions = resp.get("ui_actions") or []
    event_draft = resp.get("event_draft")
    chips = [Chip(label=(a.get("label") or ""), action="CONTINUE") for a in ui_actions]  # # FLAGGED action
    return NextAction(
        kind=_kind_from_routing(routing, event_draft, bool(ui_actions)),  # # FLAGGED
        utterance=resp.get("assistant_message", ""),
        tool=routing.get("tool_called"),  # # FLAGGED: a tool name, not necessarily a capability_id
        tool_args_json="",  # # FLAGGED: runtime returns no structured tool arguments
        defer_goal_id=None,
        chips=chips,
        distress_turn=False,  # # FLAGGED: real field, just not on this wire (routing_dict has it)
        why="",  # # FLAGGED: real field, not returned by the messages endpoint
    )


# ---------------------------------------------------------------------------
# Shared auth
# ---------------------------------------------------------------------------

def _password_grant() -> dict[str, Any]:
    """Authenticate as SIM_LIVE_EMAIL. Returns the whole session body — the caller may want
    either the access_token (HTTP adapter) or user.id (inproc needs a real user_id).

    Delegated to sim_auth.sign_in (2026-08-20). What it used to be was a bare password grant
    ending in raise_for_status(), which reports the SAME `400` for three unrelated mistakes:
    wrong project, missing/wrong SIM_PASSWORD, account never seeded. sim_auth names the likely
    one, and falls back to the service-role magic link when SIM_PASSWORD is empty — which is the
    normal state of a local stack, where you pick the password at seed time and may not have set
    one at all. Name kept so nothing else has to change; it is no longer password-ONLY.
    """
    return sign_in(SIM_LIVE_EMAIL or os.environ.get("SIM_LIVE_EMAIL", ""))


class LivePolicy:
    """PolicyPort adapter onto POST /lana/sessions/{id}/messages (the whole shipped pipeline)."""

    def __init__(self) -> None:
        # SIM_PASSWORD is NOT in this list any more: sim_auth can authenticate with the
        # service-role magic link instead, which is how a local stack usually runs. A
        # credential-less environment still fails, just with sim_auth's diagnosed message.
        missing = [k for k, v in {
            "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
            "SIM_LIVE_EMAIL": SIM_LIVE_EMAIL,
        }.items() if not v]
        if missing:
            raise RuntimeError(
                "LivePolicy needs a running Lana + a provisioned account. Missing env: "
                + ", ".join(missing)
                + ". (Use SIM_BACKEND=stub or run_eval.py --dry-run to run without a live backend.)"
            )
        self._jwt = _password_grant()["access_token"]
        # The account's real world is not readable over this API and is certainly not the
        # scenario's — permanently world_source="account" with nothing observed, which
        # check_world_fidelity scores UNSCORED. See the module docstring.
        self.last_note = TurnNote(
            world_source="account",
            observed_state_tokens=None,
            flags=["HTTP adapter: the scenario's pinned world is NOT applied to the account."],
        )

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        # NOTE: scenario.world is NOT pushed to the backend (no seed path from here) — see docstring.
        headers = {"Authorization": f"Bearer {self._jwt}"}
        with httpx.Client(timeout=120) as http:
            sess = http.post(f"{LANA_BASE_URL}/lana/sessions",
                             json={"purpose": "lana", "force_new": True}, headers=headers)
            sess.raise_for_status()
            session_id = sess.json()["session_id"]

            # Replay prior USER turns to rebuild context server-side (assistant turns are the
            # server's own; it regenerates them). Then send the turn under test.
            last: dict[str, Any] = {}
            for t in ctx.recent:
                if t.get("role") == "user":
                    r = http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
                                  json={"message": t["content"]}, headers=headers)
                    r.raise_for_status()
            r = http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
                          json={"message": ctx.user_text}, headers=headers)
            r.raise_for_status()
            last = r.json()

            http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/complete",
                      json={"force": True, "publish": False}, headers=headers)

        return map_to_next_action(last)


# ---------------------------------------------------------------------------
# inproc — the real decide_turn, called directly
# ---------------------------------------------------------------------------

# GUESSED: the harness has no source for verified_active_count / unlock_threshold when it
# fabricates an area, and nothing in the policy prompt reads them as anything but colour
# (world.py:143 passes them straight through). These are plausible fillers for the three
# unlock states, used ONLY on the injected path, where the whole world is already declared
# harness-supplied. threshold 10 is the real column default (20260906120000:205).
_AREA_FILLER = {"closed": 1, "warming": 6, "open": 14}


def world_dict_from(world: WorldState) -> dict[str, Any]:
    """Build the exact dict shape app/policy/world.py:105 returns, from a pinned WorldState.

    Pure and unit-testable — selftest asserts its `states` list equals the WorldState's own
    token set, so an injected world can never silently disagree with what the harness's
    capability checks are comparing against.
    """
    tokens = sorted(world.current_state_tokens())
    return {
        "user": {
            "nickname": None,
            "locale": world.locale,
            "role": world.role,
            "grammatical_gender": world.grammatical_gender,
            "kids_count": None,
            "verified": "verified" in tokens,
        },
        "area": {
            "state": world.zip_unlock_state if world.home_zip else None,
            "count": _AREA_FILLER.get(world.zip_unlock_state, 0),
            "threshold": 10,
        },
        "circles": [
            {
                "key": f"{c.circle_type}:{i}",
                "type": c.circle_type,
                "grounded": bool(c.grounded),
                "confirmed": bool(c.confirmed),
                "place": c.place_name,
            }
            for i, c in enumerate(world.communities)
        ],
        "states": tokens,
    }


def map_real_action(real: Any) -> NextAction:
    """app.policy.decide.NextAction -> ports.NextAction. Pure; selftest exercises it with a
    stand-in object so this mapping is covered without a DB or an LLM.

    `real is None` must NOT reach here — decide_turn returning None means "no decision, the
    legacy path answered", which this harness did not run and therefore cannot score.
    InProcPolicy converts that into a declined TurnNote instead (fail closed).
    """
    kind = str(getattr(real, "kind", "") or "reply")
    goal_id = getattr(real, "goal_id", None)
    # The real NextAction has no `tool`. A capability goal's id is "cap:<capability_id>"
    # (app/policy/goals.py:381), so that prefix is the only faithful source for the
    # capability an offer pitched. Anything else (a rapport gap id, a suggestion id, None)
    # means no capability was named, which is tool=None — not an invented tool.
    tool = None
    if isinstance(goal_id, str) and goal_id.startswith("cap:"):
        tool = goal_id[len("cap:"):] or None
    raw_chips = getattr(real, "chips", None) or []
    chips = [
        # # FLAGGED action: shipped chips are {label, send} — there is no typed action enum
        # anywhere in app/. Label text IS real and is lexicon-checked; the escape sub-check
        # is reported unobservable rather than passed vacuously (checks.check_chips).
        Chip(label=str(c.get("label") or ""), action="CONTINUE")
        for c in raw_chips if isinstance(c, dict) and str(c.get("label") or "").strip()
    ]
    return NextAction(
        kind=kind if kind in ("reply", "ask_gap", "ground_place", "bridge_offer",
                              "capture_defer", "handoff") else "reply",
        utterance=str(getattr(real, "utterance", "") or ""),
        tool=tool,
        tool_args_json="",
        defer_goal_id=(getattr(real, "defer_goal_id", None) or None),
        chips=chips,
        distress_turn=bool(getattr(real, "distress_turn", False)),
        why=str(getattr(real, "why", "") or ""),
    )


class InProcPolicy:
    """PolicyPort driving app.policy.decide.decide_turn() directly, in this process."""

    def __init__(self) -> None:
        if str(WORKER_ROOT) not in sys.path:
            sys.path.insert(0, str(WORKER_ROOT))
        self.inject_world = os.environ.get("SIM_INPROC_INJECT_WORLD", "").strip().lower() in (
            "1", "true", "yes",
        )
        # SIM_INPROC_SEED_WORLD=1 — the local-stack option (see local_world.py). It WRITES the
        # scenario's world for the real user before each decision, so every read decide_turn
        # makes agrees, not just world_state(). local_world refuses unless SUPABASE_URL is a
        # local stack, so this cannot be turned on against the shared dev project by accident.
        self.seed_world = os.environ.get("SIM_INPROC_SEED_WORLD", "").strip().lower() in (
            "1", "true", "yes",
        )
        if self.seed_world and self.inject_world:
            # Both would "honour" the world by different means and disagree in the report.
            # Seeding is the truthful one, so it wins and the other is announced as ignored.
            print("[inproc] SIM_INPROC_SEED_WORLD=1 takes precedence over "
                  "SIM_INPROC_INJECT_WORLD=1 (seeding is real; injection is fabricated).")
            self.inject_world = False
        self.last_note = TurnNote()
        # Import check FIRST: it is free and local, while user-id resolution may make a network
        # round-trip. Failing on the cheap, deterministic precondition gives the clearer error.
        try:
            import app.policy.decide as _decide  # noqa: F401
            import app.policy.world as _world  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"InProcPolicy could not import app.policy from {WORKER_ROOT}: {e}. "
                "(Run from inside policy_eval/ with the worker's deps installed, or use "
                "--backend live / --backend stub / --dry-run.)"
            ) from e
        self.user_id = self._resolve_user_id()

    @staticmethod
    def _resolve_user_id() -> str:
        """A REAL user id is mandatory: every DB read decide_turn makes is keyed on it, and a
        fabricated uuid would return empty rows for goals/claims/circles while still LOOKING
        like a successful run — the silent-empty-world failure this harness exists to catch."""
        explicit = os.environ.get("SIM_INPROC_USER_ID", "").strip()
        if explicit:
            return explicit
        # SIM_PASSWORD deliberately absent — see LivePolicy.__init__.
        missing = [k for k, v in {
            "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
            "SIM_LIVE_EMAIL": SIM_LIVE_EMAIL,
        }.items() if not v]
        if missing:
            raise RuntimeError(
                "InProcPolicy needs SIM_INPROC_USER_ID, or an account to resolve one from. "
                "Missing env: " + ", ".join(missing)
            )
        body = _password_grant()
        uid = str(((body.get("user") or {}).get("id")) or "").strip()
        if not uid:
            raise RuntimeError("password grant returned no user.id — cannot resolve a user_id")
        return uid

    # -- the world seam ----------------------------------------------------
    def _observed_tokens(self) -> set[str] | None:
        """The account's REAL state tokens, read through the same function decide_turn uses."""
        try:
            from app.policy.world import world_state
            return set(world_state(self.user_id).get("states") or [])
        except Exception:  # noqa: BLE001 — an unreadable world is 'unknown', never 'matches'
            return None

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        import app.policy.world as world_mod
        from app.policy.decide import decide_turn as real_decide_turn

        session_ctx: dict[str, Any] = {
            "lang": ctx.world.locale,
            "deferred_goal_ids": [],
            "rolling_summary": None,
            # No streak: each scenario is a fresh single-turn probe, so the ask-ceiling
            # (decide.py:MAX_CONSECUTIVE_ASKS) is deliberately not primed. A scenario that
            # wants to test the ceiling must say so in its own recent[] and set this.
            "policy_ask_streak": ctx.world.extra.get("policy_ask_streak", 0),
        }
        history = [{"role": t.get("role"), "content": t.get("content")} for t in ctx.recent]

        original = world_mod.world_state
        if self.seed_world:
            # Write the pinned world, then read it back through the SAME function decide_turn
            # will use. The read-back is the point: check_world_fidelity scores "seeded" UNSCORED
            # unless observed == pinned, so a seed that silently didn't take cannot present as a
            # controlled run. Any refusal from local_world (non-local target, writes not allowed)
            # propagates — failing the run is correct; quietly degrading to "account" would hide
            # that the operator asked for a controlled world and did not get one.
            from local_world import apply_world

            apply_world(self.user_id, ctx.world)
            observed = self._observed_tokens()
            pinned = ctx.world.current_state_tokens()
            self.last_note = TurnNote(
                world_source="seeded",
                observed_state_tokens=observed,
                flags=["World SEEDED into the local DB for this account before the decision "
                       "(local_world.apply_world). decide_turn read it back through the real "
                       "world_state(); goals, claims, capability rows and the LLM call are real."
                       + ("" if observed == pinned else
                          f" READ-BACK MISMATCH: pinned={sorted(pinned)} "
                          f"observed={sorted(observed) if observed is not None else None}.")],
            )
        elif self.inject_world:
            pinned = world_dict_from(ctx.world)
            world_mod.world_state = lambda _uid, _p=pinned: _p  # type: ignore[assignment]
            self.last_note = TurnNote(
                world_source="injected",
                observed_state_tokens=set(pinned["states"]),
                flags=["HARNESS-SUPPLIED WORLD injected at app.policy.world.world_state — the "
                       "area/verification/circle state below is fabricated by the scenario, NOT "
                       "read from the account. Goals, capability rows and the LLM call are real."],
            )
        else:
            observed = self._observed_tokens()
            self.last_note = TurnNote(
                world_source="account",
                observed_state_tokens=observed,
                flags=["World NOT injected: decide_turn read the account's own world. The "
                       "scenario's pinned world was ignored (set SIM_INPROC_INJECT_WORLD=1 to "
                       "override, and read the flag it prints)."],
            )
        try:
            real = real_decide_turn(
                user_id=self.user_id,
                session_ctx=session_ctx,
                history=history,
                user_message=ctx.user_text,
            )
        finally:
            world_mod.world_state = original  # type: ignore[assignment]

        if real is None:
            # decide.py:499 — None means "no decision"; the caller falls through to the legacy
            # pipeline, which this harness did not run. Nothing about the turn the user would
            # have seen is observable, so it is UNSCORED, never a PASS and never a HARD_FAIL
            # against Lana (the legacy path may well have answered perfectly).
            self.last_note.decision_declined = True
            self.last_note.flags.append(
                "decide_turn returned None (no decision -> legacy fall-through). Use "
                "--backend live to score what the user actually receives on these turns."
            )
            return NextAction(kind="handoff", utterance="", why="",
                              distress_turn=False)
        return map_real_action(real)
