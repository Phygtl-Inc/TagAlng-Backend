"""The P1 language classifier, gender agreement, and banned literals.

Implements `SPEC_P1_LANGUAGE.md` §CLASSIFIER and §GENDER exactly. Two rules from
that spec are load-bearing and easy to lose in an implementation:

  * **No LLM-as-judge anywhere in P1.** The point of the section is determinism.
    A model asked "is this Spanish?" will disagree with itself across runs and
    the section will never converge.
  * **`unclassifiable_short / total <= 0.34`, else the verdict is `error`.** If
    more than a third of Lana's output is too short to classify, the check is
    void — reporting `pass` there is reporting a measurement that did not happen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# ---------------------------------------------------------------- banned literals

# SPEC_P1_LANGUAGE.md §CLASSIFIER — the verbatim strings Cata saw in a Spanish
# session. Case-insensitive substring match. Any hit is a hard FAIL with no
# classifier needed; tier is [WORKER] from assistant_message, [FRONTEND] from a
# ui_actions label rendered out of a message catalog.
BANNED_LITERALS = (
    "I found your account",
    "Love it.",
    "What kind of thing are you up for?",
    "Welcome back",
    "Got it",
    "Tell me more",
    "Sounds good",
    "Let's do this",
)

# §GENDER. Accent-preserving (NFC), word-boundary, case-insensitive.
ES_FEMININE = (
    "bienvenida", "encantada", "lista", "nueva", "sola", "tranquila",
    "contenta", "ocupada", "preparada", "cansada", "sorprendida", "segura",
)
ES_MASCULINE = (
    "bienvenido", "encantado", "listo", "nuevo", "tranquilo", "contento",
    "ocupado", "preparado", "cansado", "sorprendido", "seguro",
)
PT_FEMININE = ("bem-vinda", "pronta", "nova", "sozinha", "ocupada", "cansada", "animada", "tranquila", "certa")
PT_MASCULINE = ("bem-vindo", "novo", "sozinho", "ocupado", "cansado", "animado", "tranquilo", "certo")

# §GENDER exclusions — these collide with non-gendered uses.
#   ES `solo` is the adverb "only"; only `sola` is scored (it is simply absent
#       from ES_MASCULINE above).
#   ES `seguro` also means "insurance", and Cata's central use case is health
#       insurance. Excluded when preceded by a determiner or followed by
#       médico / de salud.
#   PT `pronto` is a sentence-initial interjection; excluded in that position.
_ES_SEGURO_INSURANCE = re.compile(
    r"\b(?:el|un|mi|su|tu)\s+seguro\b|\bseguro\s+(?:médico|de\s+salud)\b", re.IGNORECASE
)
_PT_PRONTO_INTERJECTION = re.compile(r"^\s*pronto\s*[,.!?]", re.IGNORECASE)

# EN control (§GENDER "English arm"): importing gendered Latin-language address
# into English is the symmetric bug.
EN_GENDERED_ADDRESS = ("mama", "mamá", "mommy", "mum", "señora", "mamãe")

_SENTENCE_SPLIT = re.compile(r"[.!?¿¡\n]+")
_MARKDOWN = re.compile(r"[*_`#>\[\]()~]|!\[[^\]]*\]")
_EMOJI = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f1e6-\U0001f1ff" "️" "]+"
)


@lru_cache(maxsize=1)
def _detector():
    """lingua, built over exactly EN/ES/PT with preloaded models.

    Restricting the language set is not an optimisation — it is what makes PT vs
    ES separable at sentence length. A full-set detector spreads probability
    mass across Galician, Catalan and Italian and the 0.90 threshold stops
    firing.
    """
    from lingua import Language, LanguageDetectorBuilder

    return (
        LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.SPANISH, Language.PORTUGUESE)
        .with_preloaded_language_models()
        .build()
    )


def detector_version() -> str:
    """Pinned into the run record — a classifier verdict is only reproducible
    against the detector build that produced it (§CLASSIFIER).
    """
    try:
        from importlib.metadata import version

        return f"lingua-language-detector=={version('lingua-language-detector')}"
    except Exception:
        return "lingua-language-detector==unknown"


@dataclass
class LanguageReport:
    total_sentences: int = 0
    unclassifiable_short: int = 0
    classified: dict[str, int] = field(default_factory=dict)
    unclassified: int = 0
    sentences: list[tuple[str, str]] = field(default_factory=list)

    @property
    def classified_count(self) -> int:
        return sum(self.classified.values())

    def count(self, lang: str) -> int:
        return self.classified.get(lang, 0)

    @property
    def short_ratio(self) -> float:
        if self.total_sentences == 0:
            return 0.0
        return self.unclassifiable_short / self.total_sentences

    @property
    def void(self) -> bool:
        """§CLASSIFIER: >34% unclassifiably-short means the check did not happen."""
        return self.short_ratio > 0.34

    def target_ratio(self, target: str) -> float | None:
        if self.classified_count == 0:
            return None
        return self.count(target) / self.classified_count


def _strip(text: str, allowlist: tuple[str, ...]) -> str:
    """§CLASSIFIER steps 1-2: strip markdown, emoji, and legitimately-foreign
    proper nouns (place names, Google Places results, peer nicknames, and
    anything the persona said first).
    """
    out = _EMOJI.sub(" ", _MARKDOWN.sub(" ", text))
    for token in sorted(allowlist, key=len, reverse=True):
        if token and len(token) > 2:
            out = re.sub(re.escape(token), " ", out, flags=re.IGNORECASE)
    return out


def classify(text: str, *, proper_noun_allowlist: tuple[str, ...] = ()) -> LanguageReport:
    """§CLASSIFIER steps 1-5."""
    report = LanguageReport()
    if not text or not text.strip():
        return report

    cleaned = _strip(text, proper_noun_allowlist)
    det = _detector()

    for raw in _SENTENCE_SPLIT.split(cleaned):
        sentence = raw.strip()
        if not sentence:
            continue
        report.total_sentences += 1
        # Step 4: below 4 word tokens, detection is unreliable. Counted, not judged.
        if len(sentence.split()) < 4:
            report.unclassifiable_short += 1
            continue
        # Step 5: top language, but only at confidence >= 0.90.
        values = det.compute_language_confidence_values(sentence)
        if values and values[0].value >= 0.90:
            code = values[0].language.iso_code_639_1.name.lower()
            report.classified[code] = report.classified.get(code, 0) + 1
            report.sentences.append((sentence, code))
        else:
            report.unclassified += 1
            report.sentences.append((sentence, "unclassified"))
    return report


def english_sentence_count(text: str, *, proper_noun_allowlist: tuple[str, ...] = ()) -> int:
    """`EN_SENTENCE_COUNT(msg)` — the primary P1 assertion."""
    return classify(text, proper_noun_allowlist=proper_noun_allowlist).count("en")


def banned_literal_hits(*surfaces: str | None) -> list[str]:
    """§CLASSIFIER banned-literal list. Applied to assistant_message,
    ui_actions[].label and ui.focus_phrase when persona.locale != 'en'.
    """
    hits: list[str] = []
    for surface in surfaces:
        if not surface:
            continue
        low = surface.lower()
        hits.extend(lit for lit in BANNED_LITERALS if lit.lower() in low)
    return sorted(set(hits))


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _tokens_present(text: str, tokens: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    hay = _nfc(text)
    for tok in tokens:
        # Hyphenated tokens (bem-vinda) need the hyphen inside the boundary.
        pattern = r"(?<![\w-])" + re.escape(_nfc(tok)) + r"(?![\w-])"
        if re.search(pattern, hay, flags=re.IGNORECASE):
            found.append(tok)
    return found


def gender_tokens(text: str, locale: str) -> dict[str, list[str]]:
    """§GENDER — feminine and masculine agreement tokens present, exclusions applied."""
    if not text:
        return {"feminine": [], "masculine": []}

    hay = _nfc(text)
    if locale == "es":
        masc = _tokens_present(hay, ES_MASCULINE)
        # `seguro` as "insurance" is not a masculine-agreement signal.
        if "seguro" in masc and _ES_SEGURO_INSURANCE.search(hay):
            others = _tokens_present(hay, ("seguro",))
            # Only drop it when every occurrence is an insurance sense.
            total = len(re.findall(r"(?<![\w-])seguro(?![\w-])", hay, flags=re.IGNORECASE))
            insurance = len(_ES_SEGURO_INSURANCE.findall(hay))
            if others and total <= insurance:
                masc = [m for m in masc if m != "seguro"]
        return {"feminine": _tokens_present(hay, ES_FEMININE), "masculine": masc}

    if locale == "pt":
        masc = _tokens_present(hay, PT_MASCULINE)
        # Sentence-initial `Pronto,` is an interjection. `pronto` is not in
        # PT_MASCULINE, but check the sentence form for completeness in case the
        # token list grows.
        if _PT_PRONTO_INTERJECTION.match(hay):
            masc = [m for m in masc if m != "pronto"]
        return {"feminine": _tokens_present(hay, PT_FEMININE), "masculine": masc}

    return {"feminine": [], "masculine": []}


def en_gendered_address_hits(text: str, *, persona_said_first: tuple[str, ...] = ()) -> list[str]:
    """§GENDER English arm (G06): the negative assertion."""
    said = {s.lower() for s in persona_said_first}
    return [t for t in _tokens_present(_nfc(text), EN_GENDERED_ADDRESS) if t.lower() not in said]


def normalized_questions(text: str) -> list[str]:
    """Interrogative sentences, normalized for P0 B10 (Asjid's repeat-question
    defect): lowercase, strip punctuation, collapse whitespace.
    """
    out: list[str] = []
    for chunk in re.split(r"(?<=[?？])\s*", text or ""):
        if "?" not in chunk:
            continue
        norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", chunk.lower())).strip()
        if norm:
            out.append(norm)
    return out
