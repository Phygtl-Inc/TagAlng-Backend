"""
ports.py — contract for the elicitation eval (contract v2 §C4, gate D5).

WHAT THIS MEASURES
------------------
C4 is the question selector: given several candidate subjects that all match the ask, it asks
the asker up to two comparison questions to work out which one they actually want.

    1. Candidate set = top 8 subjects from C2/C3.
    2. Matrix S (candidates x active predicates) from subject_context(); missing cell = 0.5.
    3. Prior over the asker's weight vector w: Dirichlet, 500 particles.
    4. For each predicate pair (a,b), expected information gain over WHICH CANDIDATE WINS:
       EIG = H(argmax) - E[H(argmax | answer)]
    5. Ask the highest-EIG pair. Reweight particles by a sigmoid on the answer.
    6. Stop when max P(winner) > 0.85 or after 2 questions. HARD CAP 2.

WHY THIS IS BUILDABLE BEFORE ANYONE WRITES C4
---------------------------------------------
There is no LLM in the loop, no database, and no dependency on the subject graph. The selector
is deterministic given a seed, so the whole thing can be implemented from the spec and evaluated
against synthetic matrices — and if the answer is "two questions reaches 0.85 on 40% of asks,
not 85%", that is far cheaper to learn now than after it is built.

D5, AND WHY THE v1 METRIC WAS VACUOUS
-------------------------------------
v1 measured "mean questions to P(winner) > 0.85", pass <= 2.0. A loop hard-capped at 2 cannot
average above 2.0, so pass was automatic and fail unreachable. Contract v2 replaced it with
"% of asks resolved within the cap", which this harness measures.

TWO METRICS, NOT ONE — the addition this harness makes:

    reach_rate     % of asks where max P(winner) > 0.85 within the cap
    winner_correct % of asks where the selector's winner IS the asker's true best candidate

**Confidence is not correctness.** A particle filter can converge hard on the wrong candidate,
and a gate that only measures reach_rate would score that as a success. Both are reported, and
`confidently_wrong` (reached the bar AND picked the wrong subject) is called out on its own —
it is the failure mode that would look like the feature working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SCHEMA_VERSION = "subject-eval/1"

# C4's own numbers, as defaults. Function arguments rather than inlined literals, per the
# contract's Rule 6 — tunable without a code deploy.
DEFAULT_PARTICLES = 500
DEFAULT_QUESTION_CAP = 2
DEFAULT_CONFIDENCE = 0.85
# Missing cell in the subject x predicate matrix. The spec's value: "no information", not "bad".
MISSING = 0.5


@dataclass
class SubjectMatrix:
    """One elicitation problem: candidates that all matched the ask, scored on each predicate.

    Values are in [0,1]; `MISSING` (0.5) means `subject_context()` returned nothing for that
    cell — which is the common case, since the display floor of 3 distinct attesters means most
    (subject, predicate) pairs are empty.
    """

    id: str
    candidates: list[str]
    predicates: list[str]
    # scores[c][p] for candidate index c, predicate index p.
    scores: list[list[float]]
    # The asker's TRUE preference weights over predicates, normalised. Ground truth, known by
    # construction — this is a synthetic problem, so the right answer is knowable.
    true_weights: list[float]
    # Free-text description of what this fixture is probing.
    probes: str = ""

    def true_best(self) -> int:
        """The candidate the asker would actually pick, given their real weights."""
        return max(
            range(len(self.candidates)),
            key=lambda c: sum(w * s for w, s in zip(self.true_weights, self.scores[c])),
        )

    def separation(self) -> float:
        """Utility gap between the true best candidate and the runner-up.

        The single number that decides whether elicitation can work at all. If every candidate
        is worth the same to the asker, no number of questions separates them — and that is not
        a selector failure, it is a property of the candidate set. Reported alongside every
        result so a low reach_rate can be attributed correctly.
        """
        utils = sorted(
            (sum(w * s for w, s in zip(self.true_weights, row)) for row in self.scores),
            reverse=True,
        )
        return (utils[0] - utils[1]) if len(utils) > 1 else 1.0


@dataclass
class Question:
    """One comparison put to the asker: 'which matters more, a or b?'"""

    predicate_a: int
    predicate_b: int
    eig: float


@dataclass
class ElicitResult:
    """What the selector did on one problem."""

    asked: list[Question] = field(default_factory=list)
    winner: int = -1
    confidence: float = 0.0
    reached: bool = False          # confidence > the threshold within the cap
    error: str | None = None

    @property
    def n_questions(self) -> int:
        return len(self.asked)


class ElicitPort(Protocol):
    """Run the selector against one problem. The asker's answers come from `matrix.true_weights`
    — a simulated asker who knows their own mind, which is the most favourable case. A real
    asker is noisier, so treat every number from this harness as an upper bound."""

    def run(self, matrix: SubjectMatrix) -> ElicitResult:
        ...
