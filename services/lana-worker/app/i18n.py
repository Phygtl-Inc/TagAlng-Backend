"""Session-sticky language detection + localized canned strings (en/es/pt).

QA (2026-07-08, production): Spanish and Brazilian-Portuguese moms got English
category prompts, event lists, and funnel strings — Lake Nona's community is
heavily Brazilian, so mirroring the user's language is table stakes.

Three pieces, all pure python (no new dependencies):

1. ``detect_language(msg)`` — a lightweight heuristic detector for es/pt vs en.
   Scores distinctive common words + diacritics per language; only returns a
   language when several distinctive signals agree (the ">0.8 confidence"
   equivalent), otherwise ``None``. It is deliberately conservative: a bare ZIP,
   a name, or a short ambiguous line detects nothing and the session keeps
   whatever language it already had.

2. ``resolve_session_lang(session_ctx, message)`` — session stickiness. A
   confidently detected non-EN message persists ``session_ctx["lang"]``; an
   explicit "in english please" (or "en español" / "em português") flips it.
   Ambiguous turns never flip the language back.

3. ``t(key, lang, **fmt)`` — the canned-string table for the deterministic
   funnel lines the LLM never writes (category ask, ZIP ask, empty states,
   verify gates). EN strings are the exact literals the code used before;
   es/pt strings are written in Lana's warm register (tú for es, você for
   pt-BR). NOTE: the es/pt copy is agent-written — native-speaker review is
   expected before merge.

The synthesizer (LLM) side of mirroring lives in ``synth_language_directive``:
one extra system-payload block telling Lana to reply in the session language
while keeping event titles / venue names as authored. Extraction and
classification prompts stay English-internal — the user text is quoted inside
them, never translated.
"""

from __future__ import annotations

import re
from typing import Any

SUPPORTED_LANGS = ("en", "es", "pt")

# ── Detection ────────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-záéíóúüñãõâêôçà¿¡']+", re.IGNORECASE)

# Distinctive tokens only — words shared across en/es/pt ("no", "me", "para",
# "a", "de") are useless as signals and are deliberately absent.
_ES_WORDS = frozenset({
    "hola", "busco", "buscando", "quiero", "quisiera", "necesito", "dónde",
    "cómo", "aquí", "cerca", "niño", "niños", "niña", "niñas", "pequeños",
    "pequeñas", "mamá", "mamás", "madre", "madres", "señora", "año", "años",
    "también", "gracias", "soy", "estoy", "tengo", "vivo", "somos", "mucho",
    "muchas", "muchos", "otra", "otras", "otros", "nueva", "nuevo", "ciudad",
    "conocer", "hijos", "hija", "hijo", "ayuda", "ayúdame", "qué", "usted",
    "ustedes", "mudé", "vecinas", "vecinos", "inglés", "español", "hablar",
    "hablo", "habla", "pequeña", "pequeño", "encantada", "gustaría",
})
_PT_WORDS = frozenset({
    "oi", "olá", "sou", "estou", "você", "vocês", "não", "sim", "mãe", "mães",
    "conhecer", "quero", "queria", "gostaria", "acabei", "cheguei", "mudei",
    "aqui", "perto", "filhos", "filha", "filho", "criança", "crianças",
    "pequenos", "pequenas", "obrigada", "obrigado", "também", "tenho", "muito",
    "muitas", "muitos", "outra", "outras", "outros", "nova", "novo", "moro",
    "bairro", "vizinha", "vizinhas", "vizinhos", "procuro", "procurando",
    "brasileira", "brasileiro", "inglês", "português", "falo", "fala",
    "encontrar", "mamãe", "mamães", "ajuda", "quarteirão",
})
_EN_WORDS = frozenset({
    "the", "and", "is", "are", "i'm", "im", "you", "my", "for", "with",
    "please", "hi", "hello", "hey", "thanks", "thank", "want", "need",
    "looking", "find", "meet", "mom", "moms", "kids", "near", "nearby", "new",
    "here", "just", "what", "where", "when", "who", "how", "would", "like",
    "help", "can", "could", "yes", "there", "this", "that", "have",
})

# Diacritics/punctuation that pin a language hard.
_ES_ONLY_CHARS = "ñ¿¡"
_PT_ONLY_CHARS = "ãõç"
# Shared accents — weak evidence of "not English", split between es and pt.
_SHARED_ACCENTS = "áéíóúü"

# A signal is a distinctive word (1 point) or a distinctive character (2 points).
# "Several distinctive tokens/diacritics" ≈ score >= 2 with a clear margin.
_MIN_SCORE = 2.0
_MIN_MARGIN = 1.0


def detect_language(msg: Any) -> str | None:
    """Best-effort 'en' | 'es' | 'pt' for a chat message; None when unsure.

    Conservative by design — a ZIP code, an email, or a two-word reply returns
    None so the session-sticky language is never flipped by ambiguity.
    """
    text = str(msg or "").strip().lower()
    if not text:
        return None
    words = _WORD_RE.findall(text)

    es = sum(1.0 for w in words if w in _ES_WORDS)
    pt = sum(1.0 for w in words if w in _PT_WORDS)
    en = sum(1.0 for w in words if w in _EN_WORDS)
    es += sum(2.0 for ch in text if ch in _ES_ONLY_CHARS)
    pt += sum(2.0 for ch in text if ch in _PT_ONLY_CHARS)
    shared = sum(0.5 for ch in text if ch in _SHARED_ACCENTS)
    es += shared
    pt += shared

    best_lang, best = max((("es", es), ("pt", pt), ("en", en)), key=lambda kv: kv[1])
    others = max(v for k, v in (("es", es), ("pt", pt), ("en", en)) if k != best_lang)
    if best < _MIN_SCORE:
        return None
    if best - others < _MIN_MARGIN:
        # es/pt near-tie: strong single-language diacritics break it.
        if best_lang in ("es", "pt"):
            has_pt = any(ch in text for ch in _PT_ONLY_CHARS)
            has_es = any(ch in text for ch in _ES_ONLY_CHARS)
            if has_pt and not has_es:
                return "pt"
            if has_es and not has_pt:
                return "es"
        return None
    return best_lang


# "In english please" style overrides — including the es/pt phrasings a user
# would actually type mid-conversation.
_ENGLISH_OVERRIDE_RE = re.compile(
    r"\b(?:in english|english,? please|speak english|switch to english|"
    r"talk (?:to me )?in english|reply in english|respond in english|"
    r"en ingl[eé]s|em ingl[eê]s|ingl[eé]s,? por favor|ingl[eê]s,? por favor)\b"
    r"|^\s*english\s*[.!]*\s*$",
    re.IGNORECASE,
)
_SPANISH_OVERRIDE_RE = re.compile(
    r"\b(?:in spanish|en espa[nñ]ol|espa[nñ]ol,? por favor|habla espa[nñ]ol conmigo)\b",
    re.IGNORECASE,
)
_PORTUGUESE_OVERRIDE_RE = re.compile(
    r"\b(?:in portuguese|em portugu[eê]s|portugu[eê]s,? por favor|fala portugu[eê]s comigo)\b",
    re.IGNORECASE,
)


def resolve_session_lang(session_ctx: dict[str, Any], message: Any) -> str | None:
    """Update + return the session-sticky language for this turn.

    - Explicit override ("in english please" / "en español" / "em português")
      always wins and persists.
    - A confidently detected es/pt message persists ``session_ctx["lang"]``.
    - Anything ambiguous keeps whatever the session already had. Plain English
      is only persisted when nothing was set (so one borrowed English phrase
      never flips a Brazilian mom back to English mid-session).
    """
    text = str(message or "")
    if _ENGLISH_OVERRIDE_RE.search(text):
        session_ctx["lang"] = "en"
        return "en"
    if _SPANISH_OVERRIDE_RE.search(text):
        session_ctx["lang"] = "es"
        return "es"
    if _PORTUGUESE_OVERRIDE_RE.search(text):
        session_ctx["lang"] = "pt"
        return "pt"
    detected = detect_language(text)
    if detected in ("es", "pt"):
        session_ctx["lang"] = detected
        return detected
    current = str(session_ctx.get("lang") or "").strip().lower()
    if current in SUPPORTED_LANGS:
        return current
    return detected  # 'en' or None — not persisted; EN is the default anyway


def session_lang(session_ctx: dict[str, Any] | None) -> str | None:
    """The session's non-English language, or None (EN / unset)."""
    lang = str((session_ctx or {}).get("lang") or "").strip().lower()
    return lang if lang in ("es", "pt") else None


# ── Synthesizer directive ────────────────────────────────────────────────────

_LANG_LABEL = {"es": "Spanish", "pt": "Brazilian Portuguese"}
_REGISTER = {
    "es": 'use natural neutral "tú" — warm, neighborly, never stiff "usted" formality',
    "pt": 'use "você" (pt-BR) — warm, neighborly',
}


def synth_language_directive(lang: str) -> str | None:
    """One system-payload block that makes the synthesizer mirror the user."""
    if lang not in _LANG_LABEL:
        return None
    label = _LANG_LABEL[lang]
    return (
        f"LANGUAGE: The user speaks {label}. Write assistant_message ENTIRELY in "
        f"{label} — {_REGISTER[lang]}. Keep event titles, venue names, and neighbor "
        "nicknames exactly as authored (never translate them). JSON keys, status "
        "values, buckets, and ISO dates stay in English/ISO form — only the text "
        "the user reads is localized."
    )


# ── Canned strings ───────────────────────────────────────────────────────────
# EN values are the exact literals previously hardcoded (behavior-preserving for
# English sessions). es/pt written in Lana's voice — native review before merge.

_STRINGS: dict[str, dict[str, str]] = {
    # discovery funnel — ZIP asks
    "discovery.ask_zip_peers": {
        "en": "What ZIP code is your block? That helps me find neighbors near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu cuadra? Así puedo encontrar vecinas cerca de ti.",
        "pt": "Qual é o ZIP code do seu quarteirão? Assim consigo encontrar vizinhas perto de você.",
    },
    "discovery.ask_zip_activities": {
        "en": "What ZIP code is your block? That helps me find activities near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu cuadra? Así puedo encontrar actividades cerca de ti.",
        "pt": "Qual é o ZIP code do seu quarteirão? Assim consigo encontrar atividades perto de você.",
    },
    "discovery.ask_zip_both": {
        "en": "What ZIP code is your block? That helps me find neighbors and activities near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu cuadra? Así puedo encontrar vecinas y actividades cerca de ti.",
        "pt": "Qual é o ZIP code do seu quarteirão? Assim consigo encontrar vizinhas e atividades perto de você.",
    },
    "discovery.ask_zip_short": {
        "en": "What ZIP code is your block? (e.g. 32827)",
        "es": "¿Cuál es el código postal (ZIP) de tu cuadra? (p. ej. 32827)",
        "pt": "Qual é o ZIP code do seu quarteirão? (ex.: 32827)",
    },
    "discovery.ask_identity_short": {
        "en": "Tell me one thing about you — life stage, heritage, or what you're looking for.",
        "es": "Cuéntame una cosa sobre ti — tu etapa de vida, tus raíces o lo que estás buscando.",
        "pt": "Me conta uma coisa sobre você — fase da vida, suas raízes ou o que está procurando.",
    },
    # discovery — out-of-coverage / bad ZIP
    "discovery.zip_unplaceable": {
        "en": "Hmm, {zip} doesn't look like a ZIP I can place — mind double-checking the 5 digits?",
        "es": "Mmm, {zip} no parece un ZIP que pueda ubicar — ¿me confirmas los 5 dígitos?",
        "pt": "Hmm, {zip} não parece um ZIP que eu consiga localizar — pode conferir os 5 dígitos?",
    },
    # discovery — verify gates
    "discovery.verify_gate_neighbors": {
        "en": "I can see neighbors nearby — to show names and connect you, verify your email first. What's your email?",
        "es": "Veo vecinas cerca — para mostrarte nombres y conectarte, primero verifica tu correo. ¿Cuál es tu email?",
        "pt": "Estou vendo vizinhas por perto — para mostrar nomes e conectar você, primeiro verifique seu e-mail. Qual é o seu e-mail?",
    },
    "discovery.verify_gate_event": {
        "en": "To join {event}, verify your email first — I'll send you a code. What's your email?",
        "es": "Para unirte a {event}, primero verifica tu correo — te envío un código. ¿Cuál es tu email?",
        "pt": "Para participar de {event}, primeiro verifique seu e-mail — eu te envio um código. Qual é o seu e-mail?",
    },
    # discovery — activities preview (the event list QA hit in Portuguese)
    "discovery.activities_empty": {
        "en": "I don't see open activities on {where} in the next couple weeks yet. "
              "You can host something, or tell me what you're looking for.",
        "es": "Todavía no veo actividades abiertas en {where} para las próximas dos semanas. "
              "Puedes organizar algo tú, o cuéntame qué estás buscando.",
        "pt": "Ainda não vejo atividades abertas em {where} nas próximas duas semanas. "
              "Você pode organizar algo, ou me contar o que está procurando.",
    },
    "discovery.activities_header": {
        "en": "Here's what's coming up near {where}:",
        "es": "Esto es lo que viene cerca de {where}:",
        "pt": "Olha o que vem por aí perto de {where}:",
    },
    "discovery.activities_tail_verified": {
        "en": "Want to RSVP to one of these, or should I find neighbors like you?",
        "es": "¿Quieres apuntarte a alguna, o busco vecinas como tú?",
        "pt": "Quer confirmar presença em alguma, ou procuro vizinhas como você?",
    },
    "discovery.activities_tail_guest": {
        "en": "Verify your email to RSVP — or ask me to find neighbors like you.",
        "es": "Verifica tu correo para apuntarte — o pídeme que busque vecinas como tú.",
        "pt": "Verifique seu e-mail para confirmar presença — ou me peça para encontrar vizinhas como você.",
    },
    # discovery — peer preview
    "discovery.peers_empty": {
        "en": "I looked around {where} — no strong matches yet. "
              "Tell me a bit more about yourself, or try a nearby ZIP.",
        "es": "Busqué por {where} — todavía no hay coincidencias fuertes. "
              "Cuéntame un poco más de ti, o prueba un ZIP cercano.",
        "pt": "Procurei por {where} — ainda não achei combinações fortes. "
              "Me conta um pouco mais sobre você, ou tente um ZIP próximo.",
    },
    "discovery.peers_header_one": {
        "en": "I found 1 neighbor near {where}:",
        "es": "Encontré 1 vecina cerca de {where}:",
        "pt": "Encontrei 1 vizinha perto de {where}:",
    },
    "discovery.peers_header_many": {
        "en": "I found {n} neighbors near {where}:",
        "es": "Encontré {n} vecinas cerca de {where}:",
        "pt": "Encontrei {n} vizinhas perto de {where}:",
    },
    "discovery.peers_tail_verified": {
        "en": "Tell me more about you for sharper matches — or ask me to introduce you to someone.",
        "es": "Cuéntame más de ti para afinar las coincidencias — o pídeme que te presente a alguien.",
        "pt": "Me conta mais sobre você para eu afinar as combinações — ou peça para eu te apresentar a alguém.",
    },
    "discovery.peers_tail_guest": {
        "en": "Verify your email to see names and connect — or tell me more about you for sharper matches.",
        "es": "Verifica tu correo para ver nombres y conectar — o cuéntame más de ti para afinar las coincidencias.",
        "pt": "Verifique seu e-mail para ver nomes e se conectar — ou me conta mais sobre você para afinar as combinações.",
    },
    # activity browse (the category prompt QA hit in Spanish)
    "browse.ask_interest": {
        "en": "Love it — what kind of thing are you up for?",
        "es": "Me encanta — ¿qué tipo de plan te apetece?",
        "pt": "Adorei — que tipo de programa você está a fim?",
    },
    "browse.ask_zip": {
        "en": "What's your ZIP code? Once I know your block I can show what's happening nearby.",
        "es": "¿Cuál es tu código postal (ZIP)? En cuanto conozca tu cuadra te muestro qué está pasando cerca.",
        "pt": "Qual é o seu ZIP code? Assim que eu souber seu quarteirão, te mostro o que está rolando por perto.",
    },
    "browse.ask_zip_retry": {
        "en": "What's your ZIP so I can see what's on your block?",
        "es": "¿Cuál es tu ZIP? Así veo qué hay en tu cuadra.",
        "pt": "Qual é o seu ZIP? Assim vejo o que tem no seu quarteirão.",
    },
    "browse.zip_no_block": {
        "en": "I couldn't find a block for ZIP {zip}. Try another (e.g. 32827 for Lake Nona).",
        "es": "No encontré una cuadra para el ZIP {zip}. Prueba con otro (p. ej. 32827 para Lake Nona).",
        "pt": "Não encontrei um quarteirão para o ZIP {zip}. Tente outro (ex.: 32827 para Lake Nona).",
    },
    "browse.empty_interest_offer": {
        "en": "Nothing like **{interest}** on your block in the next couple weeks. "
              "Want me to listen and text you the moment a neighbor wants the same — "
              "or widen the search?",
        "es": "No hay nada como **{interest}** en tu cuadra en las próximas dos semanas. "
              "¿Quieres que me quede atenta y te escriba en cuanto una vecina busque lo mismo — "
              "o amplío la búsqueda?",
        "pt": "Nada como **{interest}** no seu quarteirão nas próximas duas semanas. "
              "Quer que eu fique de olho e te avise assim que uma vizinha quiser o mesmo — "
              "ou amplio a busca?",
    },
    "browse.events_header": {
        "en": "Here's what's coming up:",
        "es": "Esto es lo que viene:",
        "pt": "Olha o que vem por aí:",
    },
    "browse.events_header_label": {
        "en": "Here's what's coming up for {label}:",
        "es": "Esto es lo que viene de {label}:",
        "pt": "Olha o que vem por aí de {label}:",
    },
    "browse.events_empty": {
        "en": "Nothing on your block in the next couple weeks. Want me to widen it, "
              "try another kind, or set up your own?",
        "es": "No hay nada en tu cuadra en las próximas dos semanas. ¿Amplío la búsqueda, "
              "probamos otro tipo de plan, o armas el tuyo?",
        "pt": "Nada no seu quarteirão nas próximas duas semanas. Quer que eu amplie a busca, "
              "tente outro tipo, ou você monta o seu?",
    },
    "browse.events_empty_label": {
        "en": "No {label} ones on your block in the next couple weeks. Want me to widen it, "
              "try another kind, or set up your own?",
        "es": "No veo planes de {label} en tu cuadra en las próximas dos semanas. ¿Amplío la "
              "búsqueda, probamos otro tipo, o armas el tuyo?",
        "pt": "Não vi nada de {label} no seu quarteirão nas próximas duas semanas. Amplio a "
              "busca, tentamos outro tipo, ou você monta o seu?",
    },
    "browse.events_tail_verified": {
        "en": "Tap one to RSVP, or tell me to narrow it (e.g. 'just cricket').",
        "es": "Toca uno para apuntarte, o dime cómo afinar la lista (p. ej. 'solo fútbol').",
        "pt": "Toque em um para confirmar presença, ou me diga como afinar a lista (ex.: 'só futebol').",
    },
    "browse.events_tail_guest": {
        "en": "Verify your email to RSVP, or tell me to narrow it (e.g. 'just cricket').",
        "es": "Verifica tu correo para apuntarte, o dime cómo afinar la lista (p. ej. 'solo fútbol').",
        "pt": "Verifique seu e-mail para confirmar presença, ou me diga como afinar a lista (ex.: 'só futebol').",
    },
    # look-meet capture (category ask + verify gate)
    "meet.ask_kind": {
        "en": "Love it — what kind of meet would help?",
        "es": "Me encanta — ¿qué tipo de encuentro te ayudaría?",
        "pt": "Adorei — que tipo de encontro ajudaria?",
    },
    "meet.verify_gate": {
        "en": "Love it — to start listening and text you when a mom wants the same, I just need "
              "to verify you. What's your email? (Already have an account? I'll log you right in.)",
        "es": "Me encanta — para quedarme atenta y escribirte cuando una mamá busque lo mismo, "
              "solo necesito verificarte. ¿Cuál es tu email? (¿Ya tienes cuenta? Te conecto enseguida.)",
        "pt": "Adorei — para eu ficar de olho e te avisar quando uma mãe quiser o mesmo, só "
              "preciso verificar você. Qual é o seu e-mail? (Já tem conta? Eu te conecto na hora.)",
    },
}


def t(key: str, lang: str | None = None, **fmt: Any) -> str:
    """Localized canned string. Unknown lang → EN; unknown key → the key itself
    (never raises mid-turn). Format placeholders are best-effort."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    lang_norm = str(lang or "en").strip().lower()
    text = entry.get(lang_norm) or entry["en"]
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError):
            return text
    return text
