"""
elicit.py — C4's question selector, implemented from the spec.

This is the REFERENCE implementation, written before the product's. It exists so D5 has
something to measure now, and so the spec's constants (500 particles, 2 questions, 0.85) can be
tested before anyone builds against them. When the real one lands it swaps in behind ElicitPort
and the same fixtures score it.

Deterministic: every run is seeded from the problem id, so the same fixture gives the same
answer on every machine. No wall clock, no unseeded randomness — the suite's determinism rule.

THE LOOP, verbatim from §C4
---------------------------
  particles      w ~ Dirichlet(alpha), 500 of them. Each is one hypothesis about what the
                 asker cares about.
  utility        u[particle][candidate] = sum_p w[p] * S[candidate][p]
  belief         P(candidate wins) = share of particles whose argmax is that candidate
  question       the predicate PAIR maximising EIG = H(argmax) - E[H(argmax | answer)]
  update         answer "a" reweights each particle by sigmoid(k * (w_a - w_b))
  stop           max P(winner) > 0.85, or after 2 questions

ONE DELIBERATE DEPARTURE, FLAGGED
---------------------------------
# GUESSED: the spec says "reweight particles by sigmoid on the answer" without giving the
# sigmoid's temperature. `_ANSWER_SHARPNESS` is the free parameter, and it decides how much a
# single answer moves the belief. Too low and two questions can never reach 0.85; too high and
# the filter collapses onto whichever candidate the first answer favoured, which is how a
# selector becomes confidently wrong. It is a function default so it can be swept, and
# run_eval sweeps it — if the reachable region is narrow, that is itself the finding.
"""

from __future__ import annotations

import math
import random

from ports import (
    DEFAULT_CONFIDENCE,
    DEFAULT_PARTICLES,
    DEFAULT_QUESTION_CAP,
    ElicitResult,
    Question,
    SubjectMatrix,
)

_ANSWER_SHARPNESS = 6.0  # GUESSED — see the module docstring.
_DIRICHLET_ALPHA = 1.0   # flat prior: no assumption about what the asker cares about.


def _dirichlet(rng: random.Random, k: int, alpha: float) -> list[float]:
    """One draw from Dirichlet(alpha, ..., alpha) via normalised Gammas."""
    g = [rng.gammavariate(alpha, 1.0) for _ in range(k)]
    total = sum(g) or 1.0
    return [x / total for x in g]


def _argmax_distribution(
    particles: list[list[float]], weights: list[float], scores: list[list[float]]
) -> list[float]:
    """P(each candidate is the asker's best), marginalised over the particle cloud."""
    n_cand = len(scores)
    tally = [0.0] * n_cand
    for particle, pw in zip(particles, weights):
        utils = [sum(w * s for w, s in zip(particle, scores[c])) for c in range(n_cand)]
        best_u = max(utils)
        # TIES SPLIT THE MASS. `if u > best_u` starting from -inf silently gives every tie to
        # the lowest-indexed candidate, and the selftest caught what that does: a matrix where
        # every cell is MISSING makes all candidates exactly equal, so the belief collapses onto
        # candidate 0 and the selector reports confidence 1.00 having learned nothing.
        #
        # That is not a corner case. §C4's own `missing cell = 0.5` guarantees exact ties
        # whenever the candidates' known cells agree, and the display floor of 3 attesters means
        # most cells ARE missing. It is also the bunched regime — the one where the topic score
        # cannot rank either. So the natural implementation is confidently arbitrary in exactly
        # the situation the feature exists for.
        #
        # # GUESSED: §C4 does not say what to do about ties. Splitting is the honest reading —
        # the belief should stay flat when nothing distinguishes the candidates — and it is what
        # makes `reached` mean something. Worth confirming when C4 is written.
        winners = [c for c in range(n_cand) if utils[c] >= best_u - 1e-12]
        share = pw / len(winners)
        for c in winners:
            tally[c] += share
    total = sum(tally) or 1.0
    return [t / total for t in tally]


def _entropy(dist: list[float]) -> float:
    return -sum(p * math.log(p) for p in dist if p > 0.0)


def _answer_likelihood(particle: list[float], a: int, b: int) -> float:
    """P(asker answers 'a') under this particle's weights. The sigmoid the spec calls for."""
    return 1.0 / (1.0 + math.exp(-_ANSWER_SHARPNESS * (particle[a] - particle[b])))


class ReferenceElicit:
    """C4 as specified. `particles`, `cap` and `confidence` are arguments, not literals."""

    name = "reference"

    def __init__(
        self,
        *,
        particles: int = DEFAULT_PARTICLES,
        cap: int = DEFAULT_QUESTION_CAP,
        confidence: float = DEFAULT_CONFIDENCE,
        sharpness: float = _ANSWER_SHARPNESS,
    ) -> None:
        self.particles = particles
        self.cap = cap
        self.confidence = confidence
        self.sharpness = sharpness

    def _likelihood(self, particle: list[float], a: int, b: int) -> float:
        return 1.0 / (1.0 + math.exp(-self.sharpness * (particle[a] - particle[b])))

    def run(self, matrix: SubjectMatrix) -> ElicitResult:
        n_pred = len(matrix.predicates)
        if n_pred < 2 or len(matrix.candidates) < 2:
            return ElicitResult(error="needs >=2 candidates and >=2 predicates")

        # Seeded from the problem id: same fixture, same answer, every machine, every run.
        rng = random.Random(f"{matrix.id}|{self.particles}|{self.sharpness}")
        cloud = [_dirichlet(rng, n_pred, _DIRICHLET_ALPHA) for _ in range(self.particles)]
        pw = [1.0 / self.particles] * self.particles

        asked: list[Question] = []
        dist = _argmax_distribution(cloud, pw, matrix.scores)

        for _ in range(self.cap):
            if max(dist) > self.confidence:
                break
            base_h = _entropy(dist)

            # EIG over every predicate pair. The candidate set is <=8 and predicates are few,
            # so this is exhaustive rather than sampled — no approximation to argue about.
            best_pair, best_eig = None, -1.0
            for a in range(n_pred):
                for b in range(a + 1, n_pred):
                    p_a = [self._likelihood(p, a, b) for p in cloud]
                    # Probability the asker answers "a" at all, under the current belief.
                    mass_a = sum(w * q for w, q in zip(pw, p_a))
                    if mass_a <= 0.0 or mass_a >= 1.0:
                        continue
                    w_if_a = [w * q for w, q in zip(pw, p_a)]
                    w_if_b = [w * (1.0 - q) for w, q in zip(pw, p_a)]
                    h_a = _entropy(_argmax_distribution(cloud, w_if_a, matrix.scores))
                    h_b = _entropy(_argmax_distribution(cloud, w_if_b, matrix.scores))
                    eig = base_h - (mass_a * h_a + (1.0 - mass_a) * h_b)
                    if eig > best_eig:
                        best_pair, best_eig = (a, b), eig

            if best_pair is None or best_eig <= 1e-9:
                # Nothing left to learn: every question is expected to teach nothing. Stopping
                # is correct, and it is NOT the same as reaching the bar — `reached` stays False.
                break

            a, b = best_pair
            asked.append(Question(predicate_a=a, predicate_b=b, eig=round(best_eig, 4)))

            # The simulated asker answers from their true weights. A real asker is noisier, so
            # every number this produces is an upper bound on what a person would give.
            says_a = matrix.true_weights[a] >= matrix.true_weights[b]
            p_a = [self._likelihood(p, a, b) for p in cloud]
            pw = [w * (q if says_a else (1.0 - q)) for w, q in zip(pw, p_a)]
            total = sum(pw) or 1.0
            pw = [w / total for w in pw]
            dist = _argmax_distribution(cloud, pw, matrix.scores)

        winner = max(range(len(dist)), key=lambda c: dist[c])
        conf = dist[winner]
        return ElicitResult(
            asked=asked,
            winner=winner,
            confidence=round(conf, 4),
            reached=conf > self.confidence,
        )


class NoElicit:
    """Ask nothing; pick the candidate with the best unweighted mean score.

    The baseline every result has to beat. If the selector's winner_correct is no better than
    this, two questions bought nothing and C4's whole cost is unjustified — the same role the
    naive-baseline table plays in reco_eval.
    """

    name = "no-questions"

    def run(self, matrix: SubjectMatrix) -> ElicitResult:
        means = [sum(row) / len(row) for row in matrix.scores]
        winner = max(range(len(means)), key=lambda c: means[c])
        return ElicitResult(asked=[], winner=winner, confidence=0.0, reached=False)
