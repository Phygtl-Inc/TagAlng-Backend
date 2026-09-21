"""local_guard.py — classify the configured Supabase target, and REFUSE to write to it
unless it is a local stack.

WHY THIS EXISTS
---------------
All six sim persona accounts (p1-sim … p6-sim) live in the SHARED dev project. Two problems
follow, and only the second one is about safety:

  1. Contention. `create_session(force_new=True)` calls `abandon_other_active_sessions`
     (app/db.py:340,363), so two people running the suite at once kill each other's sessions
     — the symptom is `400 session_not_active` on turn 2. The fix is a per-developer LOCAL
     stack (see LOCAL_STACK.md), not "store the results somewhere else": the conversation
     itself writes session/message rows.

  2. Blast radius. The suite already contains genuinely destructive writes aimed at whatever
     SUPABASE_URL happens to be in `.env.local` — `simulation._seed_claims` DELETEs every
     `user_identity_claims` row for a user, `circles_zip/live_seed.py` inserts and deletes
     `circle_affiliations`, `local_world.py` rewrites `zip_unlock` / `users` columns. Today
     `.env.local` points at the shared dev project by default. A misdirected run therefore
     wipes a teammate's claims silently and successfully.

So: every destructive/seeding path calls `require_local()` FIRST. Non-local target -> refuse,
loudly, with the URL it classified and how to override. This LAYERS ON TOP of the existing
`SIM_ALLOW_WRITES=1` gate; it does not replace or weaken it. Both must hold.

CLASSIFICATION IS FAIL-CLOSED
-----------------------------
Only a host we positively recognise as a local Supabase stack is `local`. A hosted project
(`*.supabase.co`) is `dev`. Everything else — a LAN IP, a tunnel hostname, a typo, an empty
string — is `unknown`, which is refused exactly like `dev`. Guessing "probably local" for an
unrecognised host is precisely the mistake that would wipe a shared project.

OVERRIDE
--------
`SIM_ALLOW_NONLOCAL_WRITES=1` bypasses the target check for people who genuinely need to seed
the shared dev project (that is what live_seed.py was written for originally). It is
deliberately a different variable from `SIM_ALLOW_WRITES` so that the usual "allow writes"
muscle memory can never silently also mean "…on the shared project". Every overridden call
prints a loud banner.

Self-tested in `simulations/selftest.py` (classifier truth table + a proof that the guarded
paths refuse against a hosted URL).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

LOCAL = "local"
DEV = "dev"
UNKNOWN = "unknown"

#: Set to 1/true/yes to allow a seeding/destructive path to run against a NON-local target.
OVERRIDE_ENV = "SIM_ALLOW_NONLOCAL_WRITES"

# Hostnames that are a local Supabase stack. Matched EXACTLY (never as a suffix): a suffix
# match would make `https://localhost.example.com` or `https://127.0.0.1.nip.io` read as
# local, and both resolve off-box.
_LOCAL_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "[::1]",
    # `supabase start` runs the API behind Kong. Inside the compose network the host is the
    # container name, which is what a containerised harness would be pointed at.
    "kong",
    "supabase_kong_tagalng",   # <service>_kong_<project_id>; project_id="tagalng" (config.toml:4)
    "supabase-kong",
    # Docker Desktop's loopback alias — a container reaching the host's `supabase start`.
    "host.docker.internal",
})

# Hosted Supabase. `.co` is today's domain; `.in`/`.net` are kept because older projects and
# some regions still answer there, and misreading one of those as `unknown` would be harmless
# (both are refused) while misreading it as local would not.
_HOSTED_SUFFIXES = (".supabase.co", ".supabase.in", ".supabase.net")


class NonLocalTargetError(RuntimeError):
    """A seeding/destructive operation was aimed at something that is not a local stack."""


@dataclass(frozen=True)
class Target:
    """What the configured SUPABASE_URL is, and why we think so."""

    url: str
    kind: str          # LOCAL | DEV | UNKNOWN
    host: str
    reason: str

    @property
    def is_local(self) -> bool:
        return self.kind == LOCAL

    def __str__(self) -> str:  # what shows up in refusal messages
        return f"{self.kind.upper()} ({self.url or '<unset>'}) — {self.reason}"


def classify(url: str | None) -> str:
    """LOCAL / DEV / UNKNOWN for a Supabase URL. Pure — no env, no network."""
    return describe(url).kind


def describe(url: str | None) -> Target:
    """classify(), but carrying the host and the human reason for the verdict."""
    raw = (url or "").strip()
    if not raw:
        return Target("", UNKNOWN, "", "SUPABASE_URL is unset or empty")

    parts = urlsplit(raw if "//" in raw else f"//{raw}")
    host = (parts.hostname or "").strip().lower()
    # urlsplit strips the brackets off an IPv6 literal; keep the raw form recognisable too.
    if not host:
        return Target(raw, UNKNOWN, "", f"could not parse a hostname out of {raw!r}")
    if parts.scheme and parts.scheme not in ("http", "https"):
        return Target(raw, UNKNOWN, host, f"unsupported scheme {parts.scheme!r} (want http/https)")

    if host in _LOCAL_HOSTS:
        return Target(raw, LOCAL, host, f"{host} is a local Supabase stack host")
    if host.endswith(".localhost"):
        return Target(raw, LOCAL, host, f"{host} is under the reserved .localhost TLD")
    if any(host.endswith(sfx) for sfx in _HOSTED_SUFFIXES):
        return Target(raw, DEV, host, f"{host} is a HOSTED Supabase project (shared with the team)")
    # Everything else — LAN IPs, tunnels, staging proxies, typos. Refused, on purpose.
    return Target(raw, UNKNOWN, host,
                  f"{host} is not a recognised local stack host and not a hosted supabase.co "
                  f"project — treated as UNKNOWN and refused (fail closed)")


def current_url() -> str:
    """The SUPABASE_URL the process is actually configured with. Read at CALL time, not import
    time, so a harness that loads `.env.local` after importing this module still gets it right."""
    return os.environ.get("SUPABASE_URL", "")


def current_target() -> Target:
    return describe(current_url())


def override_enabled(env: dict[str, str] | None = None) -> bool:
    src = os.environ if env is None else env
    return str(src.get(OVERRIDE_ENV, "")).strip().lower() in ("1", "true", "yes")


def require_local(operation: str, *, url: str | None = None,
                  env: dict[str, str] | None = None) -> Target:
    """Gate a destructive or seeding operation on the target being a LOCAL stack.

    Returns the Target when allowed. Raises NonLocalTargetError otherwise.

    `operation` is a short human phrase used verbatim in the refusal, e.g.
    "DELETE user_identity_claims for P3" — the reader must be able to tell from the message
    alone what would have happened.
    """
    target = describe(current_url() if url is None else url)
    if target.is_local:
        return target
    if override_enabled(env):
        print(
            "\n"
            "  !!  ================================================================\n"
            f"  !!  {OVERRIDE_ENV}=1 — writing to a NON-LOCAL target on purpose.\n"
            f"  !!  operation : {operation}\n"
            f"  !!  target    : {target}\n"
            "  !!  This is a SHARED project. Other people's rows are in scope.\n"
            "  !!  ================================================================\n"
        )
        return target
    raise NonLocalTargetError(
        f"REFUSING to {operation}: the configured Supabase target is not a local stack.\n"
        f"  target : {target}\n"
        f"  why    : this operation writes to (or deletes from) whatever SUPABASE_URL points at.\n"
        f"           The sim persona accounts live in the SHARED dev project, so a misdirected\n"
        f"           run destroys a teammate's data.\n"
        f"  fix    : start a local stack and point .env.local at it — see\n"
        f"           services/lana-worker/simulations/LOCAL_STACK.md\n"
        f"  escape : set {OVERRIDE_ENV}=1 if you really do mean to write to this target."
    )


# ---------------------------------------------------------------------------
# Auth diagnostics — task-3 support, shared by simulation.py and policy_eval/live_policy.py
# ---------------------------------------------------------------------------

def auth_failure_help(*, email: str, password: str, anon_key: str,
                      status: int | None = None, body: str = "",
                      url: str | None = None) -> str:
    """Turn a failed Supabase password grant into an ACTIONABLE message.

    A raw `400 Bad Request {"error":"invalid_grant"}` is indistinguishable between the three
    things that are actually wrong in practice, and they have completely different fixes:

      * pointed at the wrong PROJECT   -> the account exists, just not there
      * SIM_PASSWORD empty/wrong       -> nobody has the shared secret; locally you SET it
      * account never seeded           -> the local stack has no p*-sim rows yet

    So we name which one(s) are plausible instead of surfacing the 400.
    """
    target = describe(current_url() if url is None else url)
    lines = [
        f"Supabase password grant FAILED for {email or '<no email>'}"
        + (f" (HTTP {status})" if status is not None else ""),
        f"  target        : {target}",
        f"  SIM_PASSWORD  : {'set (%d chars)' % len(password) if password else 'MISSING/EMPTY'}",
        f"  anon key      : {'set' if anon_key else 'MISSING/EMPTY'}",
    ]
    causes: list[str] = []
    if not anon_key:
        causes.append(
            "SUPABASE_ANON_KEY is empty — the grant cannot even be authorised. Copy the anon "
            "key printed by `supabase start` (or the project's key) into .env.local.")
    if not password:
        causes.append(
            "SIM_PASSWORD is empty. On a LOCAL stack you choose it yourself: run\n"
            "        psql \"$LOCAL_DB_URL\" -v sim_password=\"$SIM_PASSWORD\" "
            "-f supabase/seed_sim_accounts.sql\n"
            "      and put the same value in .env.local. Against the shared dev project it is a "
            "team secret and is NOT in anyone's checked-in env — use a local stack instead.")
    if target.kind == LOCAL:
        causes.append(
            "the local stack may not be SEEDED: `supabase start` creates an empty auth schema, "
            "and the p1-sim…p6-sim accounts only exist after seed_sim_accounts.sql is applied. "
            "Check with:  psql \"$LOCAL_DB_URL\" -c \"select email from auth.users where email "
            "like 'p%-sim@phygtl.dev';\"")
    else:
        causes.append(
            f"SUPABASE_URL points at a {target.kind.upper()} target, not your local stack. If you "
            f"meant to run locally, LOCAL_STACK.md has the two lines to change in .env.local.")
    if password and anon_key and target.kind == LOCAL:
        causes.append(
            "if the accounts ARE seeded, SIM_PASSWORD does not match the `-v sim_password=` value "
            "the seed was applied with — re-run the seed with the password from .env.local "
            "(it is re-runnable and resets encrypted_password).")

    lines.append("  likely cause  :")
    lines.extend(f"    - {c}" for c in causes)
    if body:
        lines.append(f"  server said   : {body[:400]}")
    lines.append("  docs          : services/lana-worker/simulations/LOCAL_STACK.md")
    return "\n".join(lines)


__all__ = [
    "LOCAL", "DEV", "UNKNOWN", "OVERRIDE_ENV",
    "NonLocalTargetError", "Target",
    "classify", "describe", "current_url", "current_target",
    "override_enabled", "require_local", "auth_failure_help",
]
