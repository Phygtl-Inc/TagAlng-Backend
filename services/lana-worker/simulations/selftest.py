"""selftest.py — non-vacuity proof for the core pipeline's mechanical checks.

Same contract as circles_zip/selftest.py, policy_eval/selftest.py and rapport/selftest.py: feed
each check a PLANTED violation and assert it fires, and feed it known-good input and assert it does
NOT. A green run is worthless if the checks cannot fail.

Currently covers the mock-user OOC guard (simulation.ooc_violations). Both directions matter
equally here:
  * a missed OOC turn corrupts a transcript that then gets SCORED as if it were real;
  * a false positive INVALIDATES a good run, silently shrinking the sample.

The negative cases are not padding — they are lifted from real persona turns in
scratch/run_2026-08-14T14-45-21Z.json that an earlier draft of the detector wrongly flagged.

Usage:  python selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_guard  # noqa: E402
from local_guard import (  # noqa: E402
    DEV,
    LOCAL,
    UNKNOWN,
    NonLocalTargetError,
    classify,
    require_local,
)
from simulation import ooc_violations  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}")
    if not condition:
        _failures.append(name)


LANA_REPLY = (
    "No neighbor has recommended one yet, so here's what's nearby (from Google — not a neighbor "
    "vouch). Would you like me to ask people near you for a pizza recommendation? It's your call!"
)


def _refuses(url: str, env: dict[str, str]) -> bool:
    try:
        require_local("test op", url=url, env=env)
        return False
    except NonLocalTargetError:
        return True


HOSTED = "https://rjlcyvwogmfmngemhbmn.supabase.co"  # the real shared dev project (.env.local)


def _with_hosted_env(**extra: str):
    """Context manager: point SUPABASE_URL at the shared dev project, override OFF."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _cm():
        keys = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SIM_ALLOW_WRITES",
                local_guard.OVERRIDE_ENV)
        saved = {k: os.environ.get(k) for k in keys}
        os.environ["SUPABASE_URL"] = HOSTED
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "svc-key-not-real"
        os.environ.pop(local_guard.OVERRIDE_ENV, None)
        os.environ.update(extra)
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _cm()


def _seed_claims_refuses() -> bool:
    """Drive the REAL simulation._seed_claims. If that function ever stops calling
    require_local this goes red — and it must, because it DELETEs every claim row for a user."""
    import simulation

    class _P:  # minimal stand-in; the guard must fire long before these are touched
        id = "P3"

        class profile:
            user_id = "51000003-0003-4000-8000-000000000003"

    with _with_hosted_env():
        try:
            simulation._seed_claims(_P())
            return False
        except NonLocalTargetError:
            return True
        except Exception:  # any other failure means the guard did not fire FIRST
            return False


def _live_seed_refuses() -> bool:
    """circles_zip/live_seed._guard_writes with SIM_ALLOW_WRITES=1 already satisfied, so only
    the new local gate can refuse. live_impl is stubbed so importing it does not pull
    .env.local into this process — the same trick circles_zip/selftest.py uses."""
    import types

    if "live_impl" not in sys.modules:
        stub = types.ModuleType("live_impl")
        stub._ALLOW_WRITES = True
        stub._URL = HOSTED
        stub._KEY = "svc-key-not-real"
        stub._headers = lambda *a, **k: {}
        stub._require_creds = lambda: None
        stub._select = lambda *a, **k: []
        sys.modules["live_impl"] = stub
    sys.path.insert(0, str(Path(__file__).resolve().parent / "circles_zip"))
    import live_seed

    with _with_hosted_env(SIM_ALLOW_WRITES="1"):
        try:
            live_seed._guard_writes("INSERT circle_affiliations")
            return False
        except NonLocalTargetError:
            return True
        except Exception:
            return False


def _local_world_refuses() -> bool:
    import local_world

    with _with_hosted_env(SIM_ALLOW_WRITES="1"):
        try:
            local_world.pin_zip_unlock("32832", "open")
            return False
        except NonLocalTargetError:
            return True
        except Exception:
            return False


class _FakeWorld:
    """Duck-typed stand-in for policy_eval's ports.WorldState. local_world.world_pin_plan is
    deliberately getattr-based, so this selftest needs no policy_eval import (that package uses
    bare intra-package imports and would drag its own sys.path rules in here)."""

    def __init__(self, **kw):
        self.home_zip = kw.get("home_zip", "32832")
        self.zip_unlock_state = kw.get("zip_unlock_state", "closed")
        self.verified = kw.get("verified", True)
        self.role = kw.get("role")
        self.grammatical_gender = kw.get("grammatical_gender")
        self.locale = kw.get("locale", "en")
        self.communities = kw.get("communities", [])


def _no_zip_no_area() -> bool:
    import local_world

    # world.py:113 keys the area snapshot off users.home_zip: no ZIP means no area exists at
    # all, so pinning a zip_unlock row would create a state the user could never observe.
    plan = local_world.world_pin_plan(_FakeWorld(home_zip=None))
    return plan["zip_unlock"] is None and plan["user"]["home_zip"] is None


def _plan_matches_tokens() -> bool:
    import local_world

    plan = local_world.world_pin_plan(
        _FakeWorld(zip_unlock_state="open", verified=False, role="parent"))
    return (plan["zip_unlock"] == {"zip5": "32832", "unlock_state": "open"}
            and plan["user"]["verified"] is False
            and plan["user"]["role"] == "parent")


def main() -> int:
    print("[selftest] mock-user OOC guard — planted violations must fire\n")

    # --- HARD: must fire, must block the turn -------------------------------------------------
    hard, _ = ooc_violations("Would you like me to ask your neighbors for a recommendation?", [])
    check("offering a Lana-only action HARD-fails", "offers-lana-action" in hard)

    hard, _ = ooc_violations("Sure, I can put out a request in the neighborhood to see if anyone knows.", [])
    check("speaking as Lana ('I can put out a request') HARD-fails", "speaks-as-lana" in hard)

    hard, _ = ooc_violations("I'm sorry, but that's not something I can assist with at the moment.", [])
    check("refusing as Lana HARD-fails", "refuses-as-lana" in hard)

    hard, _ = ooc_violations("Lana: sure, let's get that set up for you.", [])
    check("a speaker label HARD-fails", "speaker-label" in hard)

    hard, _ = ooc_violations("*sighs* I guess I'll figure it out myself.", [])
    check("a stage direction HARD-fails", "stage-direction" in hard)

    # Verbatim echo — the unambiguous signal. A persona reproducing Lana's own sentence is role
    # bleed, not paraphrase.
    hard, _ = ooc_violations(
        "No neighbor has recommended one yet, so here's what's nearby (from Google — not a "
        "neighbor vouch).", [LANA_REPLY])
    check("echoing Lana's own sentence HARD-fails", "echoes-lana-verbatim" in hard)

    hard, _ = ooc_violations("that's nearby from google not a neighbor", [LANA_REPLY])
    check("...but a SHORT incidental overlap does not (needs 8+ consecutive words)",
          "echoes-lana-verbatim" not in hard)

    # --- NEGATIVE CONTROLS: ordinary persona speech must stay clean ---------------------------
    clean = [
        ("Thanks anyway! I'll see if I can find someone on my own.",
         "a persona declining help and acting for THEMSELVES"),
        ("I don't have a specific place in mind, just looking for a good suggestion nearby.",
         "a plain request"),
        ('You might want to check out local parent groups — the "Moms of [Your City]" ones.',
         "a bracketed PLACEHOLDER is not a stage direction"),
        ("Can you tell me how to join an existing one?", "a direct question to Lana"),
        ("ok never mind", "a terse disengage"),
        ("I want to host a meeting, possibly here in Lake Nona.", "an in-character goal statement"),
    ]
    for msg, why in clean:
        hard, _ = ooc_violations(msg, [LANA_REPLY])
        check(f"NO false positive on {why}", hard == [])

    # --- SOFT tier: flags but never blocks ------------------------------------------------------
    hard, soft = ooc_violations("Let me know if you have any links or contacts for that.", [])
    check("service register is SOFT only (flagged, never invalidating)", hard == [] and soft != [])

    # ==========================================================================================
    # local_guard — the preflight between a misdirected run and a wiped SHARED database.
    #
    # Mechanical in the CLAUDE.md sense: the ground truth is known by construction (a URL either
    # is a loopback/kong host or it is not), so it belongs in code, not in a judge. Both
    # directions matter and the false-NEGATIVE is the dangerous one — calling a hosted project
    # "local" is what deletes a teammate's claims.
    # ==========================================================================================
    print()
    print("[selftest] local_guard - target classification must be exact and fail closed")
    print()

    for url in ("http://localhost:54321", "http://127.0.0.1:54321", "https://localhost",
                "http://0.0.0.0:54321", "http://[::1]:54321", "http://kong:8000",
                "http://supabase_kong_tagalng:8000", "http://host.docker.internal:54321",
                "http://api.localhost:54321", "localhost:54321"):
        check(f"LOCAL: {url}", classify(url) == LOCAL)

    for url in ("https://rjlcyvwogmfmngemhbmn.supabase.co",  # the real dev project (.env.local)
                "https://YOUR_PROJECT.supabase.co", "https://x.supabase.in",
                "https://x.supabase.net"):
        check(f"DEV (hosted, shared): {url}", classify(url) == DEV)

    # Fail closed: every one of these is a plausible typo or alternative setup, and none of
    # them may read as local.
    for url in ("", "   ", "not a url at all", "https://localhost.evil.com",
                "https://127.0.0.1.nip.io", "http://192.168.1.20:54321",
                "http://10.0.0.5:54321", "postgres://localhost:54322/postgres",
                "https://staging.internal.example.com"):
        check(f"UNKNOWN (refused): {url!r}", classify(url) == UNKNOWN)

    # The near-misses spelled out: suffix matching instead of exact matching would be fatal.
    check("a host merely CONTAINING 'localhost' is not local",
          classify("https://localhost.evil.com") != LOCAL)
    check("a host merely CONTAINING '127.0.0.1' is not local",
          classify("https://127.0.0.1.nip.io") != LOCAL)

    # --- the gate ---------------------------------------------------------------------------
    check("require_local ALLOWS a local target",
          require_local("wipe claims", url="http://127.0.0.1:54321", env={}).is_local)

    for url in ("https://rjlcyvwogmfmngemhbmn.supabase.co", "", "http://192.168.1.20:54321"):
        try:
            require_local("DELETE user_identity_claims", url=url, env={})
            check(f"require_local REFUSES {url!r}", False)
        except NonLocalTargetError as exc:
            check(f"require_local REFUSES {url!r}",
                  "REFUSING" in str(exc) and "LOCAL_STACK.md" in str(exc))

    # The override must be its OWN variable: SIM_ALLOW_WRITES ("I meant to write") must never
    # double as permission to write to a shared project ("...over there").
    check("SIM_ALLOW_WRITES=1 alone does NOT unlock a non-local target",
          _refuses(HOSTED, {"SIM_ALLOW_WRITES": "1"}))
    check("SIM_ALLOW_NONLOCAL_WRITES=1 DOES unlock it (the explicit escape hatch)",
          not _refuses(HOSTED, {local_guard.OVERRIDE_ENV: "1"}))

    # --- the guarded call sites must actually ASK -------------------------------------------
    # Classifying correctly is worthless if a write path forgets to call the classifier. These
    # drive the real functions with the shared dev URL in the environment.
    check("simulation._seed_claims REFUSES against a hosted target", _seed_claims_refuses())
    check("circles_zip/live_seed writes REFUSE against a hosted target", _live_seed_refuses())
    check("local_world writes REFUSE against a hosted target", _local_world_refuses())

    # --- local_world's pure world->writes mapping -------------------------------------------
    check("local_world pins no area when the user has no home ZIP", _no_zip_no_area())
    check("local_world's pin plan carries the scenario's own state", _plan_matches_tokens())


    # --- _turn_is_sourced: place_suggestions is a REAL source ---------------------------------
    # Added 2026-08-25. app/main.py:1223 puts Google Places cards on the /messages wire; the
    # harness discarded them, so a turn where the runtime HANDED Lana a venue was indistinguishable
    # from one where she invented it. That is a false positive on the hallucination axis, which is
    # the most expensive place to have one.
    import qa_analyze as _qa

    _bare = {"turn_number": 2, "lana_reply": "Canvas Restaurant & Market is a great spot."}
    check("a turn with NO runtime data is not sourced", not _qa._turn_is_sourced(_bare))

    _with_places = dict(_bare, place_suggestions=[{"name": "Canvas Restaurant & Market",
                                                  "community": False}])
    check("...but the same turn IS sourced once Google places came back",
          _qa._turn_is_sourced(_with_places))

    check("an EMPTY place_suggestions list does not count as a source",
          not _qa._turn_is_sourced(dict(_bare, place_suggestions=[])))

    # Non-vacuity for the detector itself: a price invented on an unsourced turn must still fire,
    # and must stop firing once the turn has a source.
    _hits_bare = _qa.unsourced_specifics([dict(_bare, lana_reply="Membership is $12 a month.")])
    _hits_src = _qa.unsourced_specifics([dict(_with_places, lana_reply="Membership is $12 a month.")])
    check("an invented price still fires on an unsourced turn", len(_hits_bare) == 1)
    check("...and does NOT fire once the turn has a real source", len(_hits_src) == 0)

    # ---- provenance: which credential produced the numbers -------------------------------
    #
    # Added after 2026-09-15/16, when a personal OPENAI_API_KEY exported in a shell shadowed the
    # repo key from .env.local. The shell key was out of credits, every call 429'd, and
    # `extract_entities_from_message` swallows BOTH its OpenAI and its Vertex failure and returns
    # [] — so the symptom was "the extractor found nothing", not "your key is dead". It cost a day.
    #
    # `shadowed_key_warning` is the tripwire. Both directions matter as much as they do for the
    # OOC guard above: a missed shadow means a report quietly describes the wrong account, and a
    # false positive puts a scary warning on top of a perfectly good run.
    import os
    import tempfile

    import provenance as _prov

    _tmp = Path(tempfile.mkdtemp())
    _envf = _tmp / ".env.local"
    _envf.write_text("OPENAI_API_KEY=sk-proj-FILEKEY0000aaaa\n", encoding="utf-8")
    _saved = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ["OPENAI_API_KEY"] = "sk-proj-SHELLKEY111bbbb"
        _w = _prov.shadowed_key_warning(str(_envf))
        check("a shell key shadowing the repo key is detected",
              bool(_w) and "bbbb" in _w and "aaaa" in _w)
        check("...and the fingerprint names the key actually in use",
              _prov.key_fingerprint() == "..bbbb")

        os.environ["OPENAI_API_KEY"] = "sk-proj-FILEKEY0000aaaa"
        check("no warning when the shell key IS the repo key",
              _prov.shadowed_key_warning(str(_envf)) is None)

        os.environ.pop("OPENAI_API_KEY", None)
        check("no warning when nothing is exported (nothing to shadow)",
              _prov.shadowed_key_warning(str(_envf)) is None)
        check("an absent key reads as (unset), not as a fingerprint",
              _prov.key_fingerprint() == "(unset)")

        os.environ["OPENAI_API_KEY"] = "sk-proj-SHELLKEY111bbbb"
        check("a missing env file is not an error — a diagnostic must never break a run",
              _prov.shadowed_key_warning(str(_tmp / "absent.env")) is None)

        # The console line carries the fingerprint, and Windows consoles here are cp950.
        # A provenance line must not be the thing that kills the run it was added to explain.
        try:
            _prov.console_line().encode("cp950")
            _enc = True
        except UnicodeEncodeError:
            _enc = False
        check("console_line survives a cp950 console", _enc)
    finally:
        if _saved is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = _saved

    print()
    if _failures:
        print(f"[selftest] {len(_failures)} FAILED: {_failures}")
        return 1
    print("[selftest] all mechanical detections fire correctly, with no false positives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
