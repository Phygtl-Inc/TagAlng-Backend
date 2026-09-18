"""
population.py — synthetic elicitation problems, seeded.

The two axes that decide whether two questions can ever be enough:

  SPARSITY    the share of (candidate, predicate) cells that are MISSING (0.5). This is not a
              knob invented for the sweep — contract v2's display floor is 3 distinct attesters
              per claim, so most cells in a real matrix ARE empty, and a missing cell carries no
              information in either direction. High sparsity means the selector is choosing
              between candidates it knows almost nothing about.

  SEPARATION  the utility gap between the asker's true best candidate and the runner-up. If
              every candidate is worth the same to them, no number of questions separates the
              set — and that is a property of the candidates, not a selector failure.

SEPARATION IS WHY THIS MATTERS BEYOND C4. Pouya's point about the recommendation path is that
subjects bunch up: "a coffee shop" matches Foxtail, Bluebird and Corner Cafe identically, so the
topic score cannot rank them and the tie-break does all the work. That is the low-separation
regime. If elicitation also fails there, then nothing in the current design separates equally
good candidates and the ranking is effectively arbitrary — which is a finding about the product,
not about the selector.

Determinism: every problem is seeded from its own id. No wall clock, no global RNG.
"""

from __future__ import annotations

import random

from ports import MISSING, SubjectMatrix

# Predicate vocabulary, deliberately plausible rather than abstract — a reader should be able to
# tell whether a generated question ("do you care more about parking or the wait?") is sane.
_PREDICATES = [
    "quiet_enough_to_work", "parking", "wait_at_peak", "good_with_kids", "price_band",
    "outdoor_seating", "staff_patience", "late_opening", "wheelchair_access", "dog_friendly",
    "vegetarian_range", "wifi_quality",
]

_SUBJECTS = [
    "Foxtail", "Bluebird", "Corner Cafe", "Lineage", "Vespr", "Harper's", "Stardust", "Neu",
]


def make_problem(
    idx: int,
    *,
    n_candidates: int = 5,
    n_predicates: int = 6,
    sparsity: float = 0.4,
    separation: str = "mixed",
) -> SubjectMatrix:
    """One synthetic problem.

    `separation` shapes how distinguishable the candidates are to the asker:
      "clear"   one candidate is genuinely better on the predicates this asker cares about
      "bunched" every candidate is close — Pouya's coffee-shop case
      "mixed"   drawn at random, the realistic middle
    """
    rng = random.Random(f"subject_eval|{idx}|{n_candidates}|{n_predicates}|{sparsity}|{separation}")
    cands = _SUBJECTS[:n_candidates]
    preds = _PREDICATES[:n_predicates]

    # The asker's true weights. Concentrated rather than flat: people care a lot about two or
    # three things, not equally about twelve.
    raw = [rng.gammavariate(0.6, 1.0) for _ in range(n_predicates)]
    total = sum(raw) or 1.0
    true_w = [x / total for x in raw]

    if separation == "bunched":
        # Every candidate near the same value on every predicate: the tie-break regime.
        base = [rng.uniform(0.45, 0.60) for _ in range(n_predicates)]
        scores = [[min(1.0, max(0.0, b + rng.gauss(0, 0.03))) for b in base]
                  for _ in range(n_candidates)]
    elif separation == "clear":
        scores = [[rng.uniform(0.2, 0.6) for _ in range(n_predicates)]
                  for _ in range(n_candidates)]
        # Plant one candidate that is strong exactly where this asker's weight is.
        hero = rng.randrange(n_candidates)
        top = sorted(range(n_predicates), key=lambda p: true_w[p], reverse=True)[:2]
        for p in top:
            scores[hero][p] = rng.uniform(0.85, 1.0)
    else:
        scores = [[rng.uniform(0.1, 0.95) for _ in range(n_predicates)]
                  for _ in range(n_candidates)]

    # Blank cells. Applied AFTER the scores so `separation` describes the underlying truth and
    # sparsity describes how much of it the selector can actually see.
    for c in range(n_candidates):
        for p in range(n_predicates):
            if rng.random() < sparsity:
                scores[c][p] = MISSING

    return SubjectMatrix(
        id=f"p{idx:03d}_{separation}_c{n_candidates}_k{n_predicates}_s{int(sparsity * 100)}",
        candidates=list(cands),
        predicates=list(preds),
        scores=scores,
        true_weights=true_w,
        probes=f"{separation} separation, {int(sparsity * 100)}% cells missing",
    )


def sweep(
    n_per_cell: int = 25,
    *,
    candidates: tuple[int, ...] = (3, 5, 8),
    sparsities: tuple[float, ...] = (0.0, 0.3, 0.6),
    separations: tuple[str, ...] = ("clear", "mixed", "bunched"),
    n_predicates: int = 6,
) -> list[SubjectMatrix]:
    """The full grid. Default 3 x 3 x 3 x 25 = 675 problems, all deterministic."""
    out: list[SubjectMatrix] = []
    i = 0
    for nc in candidates:
        for sp in sparsities:
            for sep in separations:
                for _ in range(n_per_cell):
                    out.append(make_problem(i, n_candidates=nc, n_predicates=n_predicates,
                                            sparsity=sp, separation=sep))
                    i += 1
    return out
