"""Pre-flight gates. Some abort the run; some only degrade assertions to blocked.

`SPEC_P0_SIGNUP.md` §PRE-FLIGHT GATES and `LANA_ZERO_BUG_PROGRAM_FINAL.md` §1.
The distinction is the whole point:

  * **ABORT** — running would either expose data or produce a meaningless
    result. G4 (RLS on `simulations` + an enrolled admin) is an abort because the
    harness writes verbatim user utterances into that table.
  * **DEGRADE** — a known gap makes certain assertions unobservable. G1 (the
    `/complete` 502) degrades every completion assertion to
    `blocked-by-known-delta`. That is not a reason to skip the night; it is a
    reason not to file the same bug nine times.

The registry audit is also here, and it aborts: a TEMPORARY entry whose PR has
merged will silently swallow a real regression (`KNOWN_DELTA_REGISTRY.md` §4.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .identity import Db
from .registry import Registry
from .worker import WorkerClient


@dataclass
class Gate:
    gate_id: str
    ok: bool
    detail: str
    aborts: bool = False
    degrades: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "ABORT" if self.aborts else "degrade"


@dataclass
class Preflight:
    gates: list[Gate] = field(default_factory=list)
    pinned_columns: dict[str, set[str]] = field(default_factory=dict)
    worker_meta: dict[str, Any] = field(default_factory=dict)
    project_ref: str = ""
    detector: str = ""

    @property
    def aborting(self) -> list[Gate]:
        return [g for g in self.gates if not g.ok and g.aborts]

    @property
    def active_deltas(self) -> tuple[str, ...]:
        out: list[str] = []
        for g in self.gates:
            if not g.ok:
                out.extend(g.degrades)
        return tuple(dict.fromkeys(out))

    def as_json(self) -> dict[str, Any]:
        return {
            "project_ref": self.project_ref,
            "worker": self.worker_meta,
            "detector": self.detector,
            "gates": [
                {"gate": g.gate_id, "status": g.status, "detail": g.detail, "degrades": list(g.degrades)}
                for g in self.gates
            ],
            "pinned_columns": {t: sorted(c) for t, c in self.pinned_columns.items()},
            "active_deltas": list(self.active_deltas),
        }


def run_preflight(cfg: Config, worker: WorkerClient, db: Db, registry: Registry, *, merged_prs: set[int]) -> Preflight:
    pf = Preflight(project_ref=cfg.project_ref)

    from .language import detector_version

    pf.detector = detector_version()

    # ---- which project are we actually pointed at -----------------------------
    # _CODE_TRUTH_2026-07-30.md opens by warning that a previous session audited
    # dev and reported it as prod. Record it, loudly, every run.
    pf.gates.append(
        Gate(
            "ENV",
            True,
            f"supabase project_ref={cfg.project_ref} ({'PROD' if cfg.is_prod else 'dev'}), "
            f"worker={cfg.worker_base_url}",
        )
    )

    # ---- worker identity -----------------------------------------------------
    try:
        root = worker.root()
        health = worker.health()
        pf.worker_meta = {"root": root, "health": health}
    except Exception as exc:
        pf.gates.append(Gate("WORKER", False, f"worker unreachable: {exc}", aborts=True))
        return pf

    # ---- G4 · RLS on simulations + an enrolled admin (ABORT) ------------------
    # SPEC_P0_SIGNUP.md: "ABORT the run. simulations holds verbatim utterances."
    try:
        admins = db.select("admin_allowlist", columns="user_id")
        sim_cols = db.columns_of("simulations")
        has_harness_cols = {"section_id", "assertions_json", "verdict", "score"} <= sim_cols
        if not has_harness_cols:
            pf.gates.append(
                Gate(
                    "G4/T2",
                    False,
                    "simulations lacks the harness columns (section_id, assertions_json, verdict, "
                    "score). PR #125 has not been applied — there is nowhere to write a verdict.",
                    aborts=True,
                )
            )
        elif not admins:
            pf.gates.append(
                Gate(
                    "G4",
                    False,
                    "admin_allowlist has 0 rows. PR #119's read policy is fail-closed, so nobody "
                    "can read the results this run would write. Enrol an admin first.",
                    aborts=True,
                    degrades=("D-20",),
                )
            )
        else:
            pf.gates.append(Gate("G4", True, f"admin_allowlist has {len(admins)} row(s); harness columns present"))
    except Exception as exc:
        pf.gates.append(Gate("G4", False, f"could not verify: {exc}", aborts=True))

    # ---- T3 · teardown must exist before we write anything (ABORT) ------------
    # The handover is explicit: "do this before T1 ever runs against prod."
    try:
        actor_cols = db.columns_of("swarm_run_actors")
        if not actor_cols:
            raise RuntimeError("swarm_run_actors not present")
        pf.gates.append(Gate("T3", True, "swarm_run_actors present — teardown can resolve this run"))
    except Exception:
        pf.gates.append(
            Gate(
                "T3",
                False,
                "swarm_run_actors is absent, so cleanup_swarm_run() cannot resolve what this run "
                "created. PR #126 has not been applied. Refusing to write test identities into a "
                "database with 31 real users and no way to sweep them.",
                aborts=True,
            )
        )

    # ---- G1 · the /complete 502 (DEGRADE) ------------------------------------
    # D-12. Detected from the deployed model stack rather than by calling
    # /complete, because calling it is the thing that 502s.
    lana_model = str((pf.worker_meta.get("health") or {}).get("lana_model", ""))
    provider = str((pf.worker_meta.get("health") or {}).get("llm_provider", ""))
    if lana_model.startswith("gemini") and provider == "openai":
        pf.gates.append(
            Gate(
                "G1",
                False,
                f"llm_provider={provider} with lana_model={lana_model}: the D-12 shape. "
                "_extract_model() hands a Gemini model to an OpenAI-routed llm_json(), so "
                "/complete returns 502. Every completion assertion is blocked, not failed.",
                degrades=("D-12",),
            )
        )
    else:
        pf.gates.append(Gate("G1", True, f"llm_provider={provider}, lana_model={lana_model}"))

    # ---- G3 · capability embeddings (DEGRADE) --------------------------------
    try:
        caps = db.select("capability_index", columns="id,embedding")
        missing = [c for c in caps if not c.get("embedding")]
        if missing:
            pf.gates.append(
                Gate(
                    "G3",
                    False,
                    f"{len(missing)} of {len(caps)} capability_index rows have no embedding. "
                    "Capability routing is dead: routing.tool_called will be null.",
                    degrades=("D-10",),
                )
            )
        else:
            pf.gates.append(Gate("G3", True, f"all {len(caps)} capability_index rows embedded"))
    except Exception as exc:
        pf.gates.append(Gate("G3", False, f"could not verify: {exc}", degrades=("D-10",)))

    # ---- D-04 / D-13 · supply state (DEGRADE, and it is not a bug) -----------
    try:
        blocks = db.select("blocks", columns="id,state")
        states = sorted({b.get("state") for b in blocks})
        unlocks = db.select("zip_unlock", columns="zip5,unlock_state,verified_active_count")
        closed = [u for u in unlocks if u.get("unlock_state") != "open"]
        if states == ["waitlist"]:
            pf.gates.append(
                Gate(
                    "D-13",
                    False,
                    f"all {len(blocks)} blocks are 'waitlist' — no live/day_zero/racing block "
                    "exists. The baseline vs cold-block arms are NOT distinguishable in prod.",
                    degrades=("D-13",),
                )
            )
        if closed:
            pf.gates.append(
                Gate(
                    "D-04",
                    False,
                    f"{len(closed)} of {len(unlocks)} zip_unlock rows are not open. Every persona "
                    "is structurally supply-blocked. Report 'supply-blocked', never 'disengaged'.",
                    degrades=("D-04",),
                )
            )
    except Exception as exc:
        pf.gates.append(Gate("SUPPLY", False, f"could not verify: {exc}", degrades=("D-04",)))

    # ---- SCHEMA · pin the column sets both specs require ---------------------
    #
    # A failed READ must never be recorded as an absent COLUMN. Both specs
    # resolve the `users.role` / `grammatical_gender` question at run start and
    # pin the answer; if the pin itself fails, every assertion touching those
    # columns would silently degrade to `blocked-by-known-delta` and the section
    # would report a clean-looking night having tested nothing. So: unreadable
    # schema is an ABORT, not an empty set.
    unreadable: list[str] = []
    for table in ("users", "user_identity_claims", "lana_sessions", "circle_affiliations", "local_signals"):
        try:
            cols = db.columns_of(table)
            if not cols:
                unreadable.append(f"{table} (empty column set)")
            else:
                pf.pinned_columns[table] = cols
        except Exception as exc:
            unreadable.append(f"{table} ({type(exc).__name__})")

    if unreadable:
        pf.gates.append(
            Gate(
                "SCHEMA",
                False,
                "could not pin the column set for: "
                + ", ".join(unreadable)
                + ". Both specs require the users.role / grammatical_gender question be resolved at "
                "run start. Treating an unreadable schema as 'column absent' would degrade those "
                "assertions to blocked and report a green night that tested nothing.",
                aborts=True,
            )
        )
    else:
        users = pf.pinned_columns.get("users", set())
        role_ok = "role" in users
        gender_ok = "grammatical_gender" in users
        pf.gates.append(
            Gate(
                "SCHEMA",
                True,
                f"users.role={'present' if role_ok else 'ABSENT'}, "
                f"users.grammatical_gender={'present' if gender_ok else 'ABSENT'}, "
                f"email_verified_at={'present' if 'email_verified_at' in users else 'ABSENT'}",
                degrades=() if (role_ok and gender_ok) else ("D-18",),
            )
        )

    # ---- REGISTRY · a merged TEMPORARY entry swallows real regressions (ABORT)
    stale = registry.audit_temporary(merged_prs)
    if stale:
        pf.gates.append(
            Gate(
                "REGISTRY",
                False,
                "these TEMPORARY registry entries have merged PRs and must be deleted before the "
                "run, or they will convert genuine regressions into blocked-by-known-delta: "
                + ", ".join(f"{d.delta_id} (#{d.pr})" for d in stale),
                aborts=True,
            )
        )
    else:
        pf.gates.append(Gate("REGISTRY", True, f"{len(registry.temporary())} TEMPORARY entries, none merged"))

    return pf
