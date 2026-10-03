"""The known-delta registry — parsed, matched, and audited.

`KNOWN_DELTA_REGISTRY.md` §0: before an agent writes `fail`, it checks the
registry. A symptom that matches an entry becomes `blocked-by-known-delta`, the
`delta_id` goes on the record, and no bug is filed. Blocked is excluded from the
score denominator.

Two things this module refuses to do implicitly:

  * **It will not guess.** Matching is driven by the `expected_delta*` keys the
    fixtures already declare per utterance, and by each persona's
    `expected_deltas` list — not by fuzzy-matching prose against the registry's
    `expected_symptom` paragraphs. A regex over English would silently swallow
    real regressions, which is the exact failure the registry exists to prevent.
  * **It will not let a merged PR keep swallowing failures.** §4.6 requires the
    TEMPORARY block be audited before every run: "Any entry whose PR has merged
    must be deleted before the run starts, or it will swallow a genuine
    regression." `audit_temporary()` does that check against live GitHub state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DeltaClass = Literal["PERMANENT", "TEMPORARY"]

# KNOWN_DELTA_REGISTRY.md §4.4 — "supply-blocked" is a load-bearing term. A
# persona who sends eleven substantive turns into a closed ZIP and receives
# nothing is not disengaged; mislabelling her sends a supply problem to the
# wrong team. The runner refuses to emit any of these.
FORBIDDEN_VERDICT_CLASSES = {"disengaged", "low-intent", "churned", "abandoned"}


@dataclass(frozen=True)
class Delta:
    delta_id: str
    klass: DeltaClass
    summary: str
    sections: tuple[str, ...]
    owner: str
    pr: int | None

    @property
    def is_temporary(self) -> bool:
        return self.klass == "TEMPORARY"


_INDEX_ROW = re.compile(
    r"^\|\s*(D-\d+)\s*\|\s*(PERMANENT|TEMPORARY)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$"
)
_PR_NUM = re.compile(r"#(\d+)")


class Registry:
    def __init__(self, deltas: dict[str, Delta]):
        self.deltas = deltas

    @classmethod
    def load(cls, path: Path) -> Registry:
        """Parse the §1 index table.

        The index is the machine-readable part; the §2/§3 prose bodies are for
        humans deciding whether a symptom really is the delta. If the index and
        the totals line disagree, that is a corrupted registry and we say so
        rather than run with a partial one.
        """
        text = path.read_text(encoding="utf-8")
        deltas: dict[str, Delta] = {}
        for line in text.splitlines():
            m = _INDEX_ROW.match(line)
            if not m:
                continue
            did, klass, summary, sections, owner, pr = m.groups()
            pr_m = _PR_NUM.search(pr)
            deltas[did] = Delta(
                delta_id=did,
                klass=klass,  # type: ignore[arg-type]
                summary=summary.strip(),
                sections=tuple(s.strip() for s in sections.split(",") if s.strip()),
                owner=owner.strip(),
                pr=int(pr_m.group(1)) if pr_m else None,
            )
        if not deltas:
            raise ValueError(f"no delta rows parsed from {path} — the §1 index table shape changed")

        # Cross-check against the stated totals so a silently-truncated parse is
        # loud rather than permissive.
        totals = re.search(r"\*\*Totals:\s*(\d+)\s*entries", text)
        if totals and int(totals.group(1)) != len(deltas):
            raise ValueError(
                f"{path.name} says {totals.group(1)} entries but {len(deltas)} parsed. "
                "Refusing to run with a partially-read registry — it would swallow real failures."
            )
        return cls(deltas)

    def __contains__(self, delta_id: str) -> bool:
        return delta_id in self.deltas

    def get(self, delta_id: str) -> Delta | None:
        return self.deltas.get(delta_id)

    def temporary(self) -> list[Delta]:
        return [d for d in self.deltas.values() if d.is_temporary]

    def for_section(self, section_id: str) -> list[Delta]:
        return [
            d
            for d in self.deltas.values()
            if section_id in d.sections or "ALL" in d.sections or "all" in d.sections
        ]

    def audit_temporary(self, merged_prs: set[int]) -> list[Delta]:
        """Entries whose PR has merged and which must therefore be deleted.

        Returning a non-empty list is a run-blocking condition, not a warning:
        a stale TEMPORARY entry converts a genuine regression into
        `blocked-by-known-delta` and the night reports green.
        """
        return [d for d in self.temporary() if d.pr is not None and d.pr in merged_prs]


def declared_deltas(persona: dict, asserts: dict) -> tuple[str, ...]:
    """Every delta id this utterance or persona has pre-declared.

    `personas.json` carries `expected_deltas` at the persona level and
    `expected_delta`, `expected_delta_if_empty`, `expected_delta_if_english`,
    ... at the utterance level. Those are the fixtures' own statement of which
    structural gaps they expect to trip, which is what makes the matching
    deterministic instead of interpretive.
    """
    out: list[str] = list(persona.get("expected_deltas") or [])
    for key, value in asserts.items():
        if key == "expected_delta" or key.startswith("expected_delta_if"):
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, list):
                out.extend(str(v) for v in value)
    seen: set[str] = set()
    return tuple(d for d in out if not (d in seen or seen.add(d)))
