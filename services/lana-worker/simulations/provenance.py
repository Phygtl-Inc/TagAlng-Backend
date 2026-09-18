"""
provenance.py — which models actually produced a run's numbers.

WHY THIS EXISTS
---------------
Pouya, 2026-09-10: he spent days tuning prompts against gpt-4o-mini before discovering
production ran a different model; the scores were fine on the right one. Nothing in the output
surfaced which path had run. Separately, his event matcher silently falls back to word matching
when the model call fails — it returns results, nothing scored them, and nothing records it.

This harness had the same hole. `reco_eval` reported a 45% fallback rate with the model named
only in a docstring, so a reader of the report could not tell what the number described. And the
runs behind it were made with `LANA_LLM_FALLBACK` unset, meaning a retryable failure could have
been re-served by a different provider mid-run.

    A number without the model that produced it is not a measurement, it is an anecdote.

Every harness that writes a report calls `header_lines()` and puts the result near the top, so a
later run can be compared to an earlier one honestly — and so "did the fix work?" has a before
number to compare against.

    from provenance import header_lines          # simulations/ on sys.path
    L.extend(header_lines(judge_model=JUDGE_MODEL))
"""

from __future__ import annotations

import os

# Model choices that are pinned in code rather than read from env, per harness. Recorded so the
# report names them even though nothing can change them at runtime.
_UNSET = "(unset)"


def key_fingerprint() -> str:
    """Last four characters of the API key actually in use.

    WHY A REPORT NEEDS THIS. A developer can easily have a personal `OPENAI_API_KEY` exported in
    their shell alongside the repo's key in `.env.local`. The SDK reads `os.environ`, so whichever
    one ends up there wins — and `load_dotenv(..., override=False)` leaves the shell's in place.

    That happened here on 2026-09-15/16: a personal key with no credits shadowed the repo key,
    every call 429'd, and because `extract_entities_from_message` swallows both its OpenAI and
    its Vertex failure, the run looked like an extractor problem for a day.

    Four characters identify which credential was used without disclosing it. Every entry point
    in this suite loads with `override=True`, so this should always be the repo key — printing
    it is how you find out when it is not.
    """
    # ASCII on purpose. This string reaches `console_line`, and a Windows cp950 console
    # raises UnicodeEncodeError on "..." -- a provenance line must never be the thing that
    # kills the run it was added to make legible.
    k = os.environ.get("OPENAI_API_KEY", "")
    return f"..{k[-4:]}" if len(k) > 8 else ("(unset)" if not k else "(short)")


def shadowed_key_warning(env_path: str | None = None) -> str | None:
    """Non-None when the key in `.env.local` is not the key that will actually be used."""
    try:
        from pathlib import Path

        from dotenv import dotenv_values

        p = Path(env_path) if env_path else Path(__file__).resolve().parents[3] / ".env.local"
        want = (dotenv_values(p).get("OPENAI_API_KEY") or "").strip()
        have = os.environ.get("OPENAI_API_KEY", "").strip()
        if want and have and want != have:
            return (f"the key in use (..{have[-4:]}) is NOT the one in {p.name} "
                    f"(..{want[-4:]}) - a shell export is shadowing the repo key")
    except Exception:  # noqa: BLE001 — a diagnostic must never break a run
        return None
    return None


def snapshot(**extra: str) -> dict[str, str]:
    """Provider, generator model, fallback state, key fingerprint, plus the caller's own pins."""
    out: dict[str, str] = {
        "provider": os.environ.get("LANA_LLM_PROVIDER", _UNSET),
        "fallback": os.environ.get("LANA_LLM_FALLBACK", _UNSET),
        "api_key": key_fingerprint(),
    }
    try:
        # `app` is not on every harness's sys.path (policy_eval adds only its own dir), and a
        # provenance line that says "not resolvable" is exactly the hole this module exists to
        # close. This file lives in simulations/, so the worker root is one level up.
        import sys
        from pathlib import Path

        _worker = str(Path(__file__).resolve().parents[1])
        if _worker not in sys.path:
            sys.path.insert(0, _worker)
        # The model under test: whatever the product would use for synthesis on this config.
        from app.orchestrator.llm import router_model, synthesizer_model

        out["synth_model"] = synthesizer_model()
        out["router_model"] = router_model()
    except Exception as exc:  # noqa: BLE001 — offline/dry harnesses need no LLM config
        out["synth_model"] = f"(not resolvable: {exc})"
    out.update({k: str(v) for k, v in extra.items() if v})
    return out


def fallback_is_disabled() -> bool:
    return os.environ.get("LANA_LLM_FALLBACK", "").strip().lower() in ("0", "false", "no")


def header_lines(**extra: str) -> list[str]:
    """Markdown bullet(s) naming the models behind this run. Put them near the top of a report."""
    p = snapshot(**extra)
    named = "  ·  ".join(f"{k.replace('_', ' ')} `{v}`" for k, v in p.items() if k != "fallback")
    lines = [f"- **Ran against:** {named}  ·  `LANA_LLM_FALLBACK={p['fallback']}`"]
    if not fallback_is_disabled():
        lines.append(
            "  - ⚠️ **Cross-provider fallback is not disabled.** A retryable failure can be "
            "silently re-served by a different provider, so the model named above may not be "
            "the model that produced every result in this run. Set `LANA_LLM_FALLBACK=0`."
        )
    shadow = shadowed_key_warning()
    if shadow:
        lines.append(f"  - ⚠️ **Wrong credential:** {shadow}. Results may reflect a dead or "
                     f"unrelated account rather than the project's. `unset OPENAI_API_KEY`.")
    return lines


def console_line(**extra: str) -> str:
    p = snapshot(**extra)
    warn = "" if fallback_is_disabled() else "  [!] cross-provider fallback NOT disabled"
    shadow = shadowed_key_warning()
    if shadow:
        warn += f"  [!] {shadow}"
    return ("[provenance] " + " ".join(f"{k}={v}" for k, v in p.items()) + warn)
