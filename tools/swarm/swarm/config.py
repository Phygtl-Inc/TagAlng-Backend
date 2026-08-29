"""Run configuration for the zero-bug swarm.

Everything the runner needs comes from the environment. Nothing here has a
production default that could cause an accidental write to the wrong project —
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are required and unguessable, and
`WORKER_BASE_URL` defaults to prod only because prod is the environment the
program targets (LANA_ZERO_BUG_PROGRAM_FINAL.md §5).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# LANA_ZERO_BUG_PROGRAM_FINAL.md §5 — the program runs against PROD.
PROD_WORKER = "https://tagalng-lana-worker-prod-975128128744.us-east1.run.app"
PROD_PROJECT_REF = "kmetmatfxdkrialwrnzj"  # tagalng-prod
DEV_PROJECT_REF = "rjlcyvwogmfmngemhbmn"  # tagalng-dev


class ConfigError(RuntimeError):
    pass


def _git_sha() -> str:
    """The sha of THIS repo, for the simulations.git_sha column.

    Note this is the harness sha, not the deployed worker's. The deployed
    revision is captured separately by preflight from GET / — conflating the two
    is how a run gets attributed to code that was never live.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class Config:
    run_id: str
    supabase_url: str
    service_role_key: str
    worker_base_url: str = PROD_WORKER
    git_sha: str = field(default_factory=_git_sha)

    # Repo-relative location of the design-repo fixtures. The specs, personas
    # and registry live in `[R&D] TagAlng/tests/`, which is a *different* repo;
    # CI checks it out or the operator points at a local path.
    fixtures_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWARM_FIXTURES_DIR", "tests")))

    # SPEC_P0_SIGNUP.md hard rail 6: never more than 3 anonymous sessions per
    # minute from one source IP; PR #124 §4.2.2 flags >40 sessions/hour as
    # scripted. Our own swarm must not look like the abuse it is meant to survive.
    sessions_per_minute: int = 3
    sessions_per_hour: int = 40

    # PR #124 §4.2 caps an anonymous conversation at 12 turns. Personas are 11
    # turns, which fits — but the cap is asserted, not assumed (P0 C05).
    anonymous_turn_cap: int = 12

    request_timeout_s: float = 90.0
    dry_run: bool = False

    @property
    def project_ref(self) -> str:
        """Parsed out of the Supabase URL so the run record can prove which
        project it hit. `_CODE_TRUTH_2026-07-30.md` opens with this warning:
        a previous session audited dev and reported it as prod.
        """
        host = self.supabase_url.split("://", 1)[-1]
        return host.split(".", 1)[0]

    @property
    def is_prod(self) -> bool:
        return self.project_ref == PROD_PROJECT_REF

    @classmethod
    def from_env(cls, run_id: str, *, dry_run: bool = False) -> Config:
        url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if not url:
            raise ConfigError("SUPABASE_URL is required")
        if not key:
            raise ConfigError("SUPABASE_SERVICE_ROLE_KEY is required (the harness writes as service_role)")
        if not run_id or not run_id.strip():
            raise ConfigError("run_id is required — teardown is keyed on it")

        cfg = cls(
            run_id=run_id.strip(),
            supabase_url=url,
            service_role_key=key,
            worker_base_url=os.environ.get("WORKER_BASE_URL", PROD_WORKER).rstrip("/"),
            dry_run=dry_run,
        )

        # A run against neither known project is almost certainly a typo'd URL.
        # Refuse rather than write persona transcripts somewhere unaudited.
        if cfg.project_ref not in (PROD_PROJECT_REF, DEV_PROJECT_REF):
            raise ConfigError(
                f"SUPABASE_URL points at project '{cfg.project_ref}', which is neither "
                f"prod ({PROD_PROJECT_REF}) nor dev ({DEV_PROJECT_REF}). Refusing to run."
            )
        return cfg

    def sim_email(self, persona_id: str, domain: str | None = None) -> str:
        """personas.json#account_convention.email_template."""
        dom = domain or os.environ.get("SIM_EMAIL_DOMAIN", "").strip()
        if not dom:
            raise ConfigError("SIM_EMAIL_DOMAIN is required for any section that signs up (P0)")
        return f"lana-sim+{self.run_id}-{persona_id}@{dom}"
