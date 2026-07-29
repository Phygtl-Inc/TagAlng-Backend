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


_LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


def normalize_lang_code(value: Any) -> str | None:
    """Lowercased ISO-shaped language code, or None for anything else."""
    code = str(value or "").strip().lower()
    return code if _LANG_CODE_RE.match(code) else None


def apply_ai_lang(session_ctx: dict[str, Any], lang: Any) -> None:
    """Apply the classifier's per-turn language verdict (AI-authoritative).

    The classifier reports the language only when the message is clearly in it
    (null for ZIPs, names, 'ok'), so a verdict — including 'en' — flips the
    session: a mom who switches to English gets English back. Null keeps the
    sticky language; the word-list heuristic never overrides an AI verdict.

    Exception: a chip tap. The tapped message is app-authored (canonical
    English payload), not the user writing English — the pipeline marks those
    turns ``_lang_pinned_turn`` and the verdict is ignored so one tap never
    flips an Urdu session back to English."""
    if session_ctx.get("_lang_pinned_turn"):
        return
    code = normalize_lang_code(lang)
    if code:
        session_ctx["lang"] = code


def lang_display_name(code: str | None) -> str:
    """Human-readable language name for prompts and nudge copy."""
    norm = normalize_lang_code(code) or "en"
    if norm == "en":
        return "English"
    return _LANG_LABEL.get(norm, norm)


def localize_text(text: str, lang: str | None) -> str:
    """AI-render arbitrary assistant text (openings, one-off lines) in the
    session language. English/unset/failure → the text unchanged. Cached like
    t() so a repeated line costs one call per language per process."""
    code = normalize_lang_code(lang)
    if not text or not code or code == "en":
        return text
    cache_key = (text, code)
    cached = _AI_RENDER_CACHE.get(cache_key)
    if cached:
        return cached
    rendered = _ai_render(text, code)
    if rendered is None:
        return text  # LLM unconfigured — don't cache, stay deterministic
    _AI_RENDER_CACHE[cache_key] = rendered or text
    return rendered or text


def localize_labels(labels: list[str], lang: str | None) -> list[str]:
    """AI-render short UI chip labels in the session language — ONE batched LLM
    call for all cache misses of the turn (never one call per chip). English /
    unset / failure → the labels unchanged. Cached per (label, lang) like t(),
    so static chips ("Yes, listen for me") cost one render per language per
    process and dynamic chips one per unique label."""
    code = normalize_lang_code(lang)
    if not labels or not code or code == "en":
        return labels
    out = list(labels)
    misses = [
        (i, lbl) for i, lbl in enumerate(out)
        if lbl and (lbl, code) not in _AI_RENDER_CACHE
    ]
    for i, lbl in enumerate(out):
        cached = _AI_RENDER_CACHE.get((lbl, code))
        if cached:
            out[i] = cached
    if not misses:
        return out
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return out
        label_name = _LANG_LABEL.get(code) or f"the language with ISO code '{code}'"
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. Rewrite each short UI "
                f"button label ENTIRELY in {label_name}. Keep each label SHORT (a "
                "button, not a sentence — aim under 28 characters), same meaning, "
                "keep proper nouns and anything quoted as-is. Never mom/mamá/mamãe, "
                "never cuadra/quadra or círculo; gender-neutral when the English is "
                "neutral. Return JSON "
                '{"labels": ["..."]} with EXACTLY one entry per input, same order.'
            ),
            user_payload="\n".join(f"- {lbl}" for _, lbl in misses),
            max_tokens=40 * len(misses) + 60,
            temperature=0.2,
        )
        rendered = data.get("labels") if isinstance(data, dict) else None
        if isinstance(rendered, list) and len(rendered) == len(misses):
            for (i, lbl), new in zip(misses, rendered):
                new_s = str(new or "").strip()
                if new_s:
                    _AI_RENDER_CACHE[(lbl, code)] = new_s
                    out[i] = new_s
    except Exception:  # noqa: BLE001 — English chips beat a failed turn
        import logging

        logging.getLogger(__name__).exception("i18n_label_render_failed")
    return out


def session_lang(session_ctx: dict[str, Any] | None) -> str | None:
    """The session's non-English language code, or None (EN / unset / garbage).

    Any ISO-shaped code counts — the AI renders replies in whatever language
    the classifier detected, not just the hand-translated es/pt pair."""
    lang = str((session_ctx or {}).get("lang") or "").strip().lower()
    if lang in ("", "en") or not _LANG_CODE_RE.match(lang):
        return None
    return lang


# ── Synthesizer directive ────────────────────────────────────────────────────

_LANG_LABEL = {
    "es": "Spanish",
    "pt": "Brazilian Portuguese",
    "ur": "Urdu",
    "hi": "Hindi",
    "ht": "Haitian Creole",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "ar": "Arabic",
    "fr": "French",
    "tl": "Tagalog",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "uk": "Ukrainian",
    "pl": "Polish",
    "nl": "Dutch",
    "el": "Greek",
    "tr": "Turkish",
    "fa": "Farsi",
    "he": "Hebrew",
    "bn": "Bengali",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "id": "Indonesian",
    "sw": "Swahili",
    "am": "Amharic",
    "so": "Somali",
    "ro": "Romanian",
    "cs": "Czech",
    "sv": "Swedish",
}
_REGISTER = {
    "es": 'use natural neutral "tú" — warm, neighborly, never stiff "usted" formality',
    "pt": 'use "você" (pt-BR) — warm, neighborly',
}


def synth_language_directive(lang: str) -> str | None:
    """One system-payload block that makes the synthesizer mirror the user."""
    lang = str(lang or "").strip().lower()
    if lang in ("", "en") or not _LANG_CODE_RE.match(lang):
        return None
    label = _LANG_LABEL.get(lang) or f"the language with ISO code '{lang}'"
    register = _REGISTER.get(lang, "warm, neighborly — natural informal register")
    return (
        f"LANGUAGE: The user speaks {label}. Write assistant_message ENTIRELY in "
        f"{label} — {register}. Keep event titles, venue names, and neighbor "
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
        "en": "What's the ZIP code for your neighborhood? That helps me find neighbors near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu zona? Así puedo encontrar gente cerca de ti.",
        "pt": "Qual é o ZIP code do seu bairro? Assim consigo encontrar pessoas perto de você.",
    },
    "discovery.ask_zip_activities": {
        "en": "What's the ZIP code for your neighborhood? That helps me find activities near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu zona? Así puedo encontrar actividades cerca de ti.",
        "pt": "Qual é o ZIP code do seu bairro? Assim consigo encontrar atividades perto de você.",
    },
    "discovery.ask_zip_both": {
        "en": "What's the ZIP code for your neighborhood? That helps me find neighbors and activities near you.",
        "es": "¿Cuál es el código postal (ZIP) de tu zona? Así puedo encontrar gente y actividades cerca de ti.",
        "pt": "Qual é o ZIP code do seu bairro? Assim consigo encontrar pessoas e atividades perto de você.",
    },
    "discovery.ask_zip_short": {
        "en": "What's the ZIP code for your neighborhood? (e.g. 32827)",
        "es": "¿Cuál es el código postal (ZIP) de tu zona? (p. ej. 32827)",
        "pt": "Qual é o ZIP code do seu bairro? (ex.: 32827)",
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
        "es": "Veo gente cerca — para mostrarte nombres y conectarte, primero verifica tu correo. ¿Cuál es tu email?",
        "pt": "Estou vendo pessoas por perto — para mostrar nomes e conectar você, primeiro verifique seu e-mail. Qual é o seu e-mail?",
    },
    "discovery.verify_gate_event": {
        "en": "To join {event}, verify your email first — I'll send you a code. What's your email?",
        "es": "Para unirte a {event}, primero verifica tu correo — te envío un código. ¿Cuál es tu email?",
        "pt": "Para participar de {event}, primeiro verifique seu e-mail — eu te envio um código. Qual é o seu e-mail?",
    },
    # Direct account ask ("sign up") — neutral copy, no neighbors promise: the
    # post-verify turn ends at a welcome, not the peers preview.
    "discovery.verify_gate_direct": {
        "en": "Let's get you set up! What's your email? I'll send you a code to verify.",
        "es": "¡Vamos a crear tu cuenta! ¿Cuál es tu correo? Te envío un código para verificar.",
        "pt": "Vamos criar sua conta! Qual é o seu e-mail? Eu te envio um código para verificar.",
    },
    # discovery — activities preview (the event list QA hit in Portuguese)
    "discovery.activities_empty": {
        "en": "I don't see open activities around {where} in the next couple weeks yet. "
              "You can host something, or tell me what you're looking for.",
        "es": "Todavía no veo actividades abiertas en {where} para las próximas dos semanas. "
              "Puedes organizar algo tú, o cuéntame qué estás buscando.",
        "pt": "Ainda não vejo atividades abertas em {where} nas próximas duas semanas. "
              "Você pode organizar algo, ou me contar o que está procurando.",
    },
    "discovery.activities_header": {
        "en": "Here's what's coming up near {where}.",
        "es": "Esto es lo que viene cerca de {where}.",
        "pt": "Olha o que vem por aí perto de {where}.",
    },
    "discovery.activities_tail_verified": {
        "en": "Want to RSVP to one of these, or should I find neighbors like you?",
        "es": "¿Quieres apuntarte a alguna, o busco gente como tú?",
        "pt": "Quer confirmar presença em alguma, ou procuro pessoas como você?",
    },
    "discovery.activities_tail_guest": {
        "en": "Verify your email to RSVP — or ask me to find neighbors like you.",
        "es": "Verifica tu correo para apuntarte — o pídeme que busque gente como tú.",
        "pt": "Verifique seu e-mail para confirmar presença — ou me peça para encontrar pessoas como você.",
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
        "es": "Encontré a 1 persona cerca de {where}:",
        "pt": "Encontrei 1 pessoa perto de {where}:",
    },
    "discovery.peers_header_many": {
        "en": "I found {n} neighbors near {where}:",
        "es": "Encontré a {n} personas cerca de {where}:",
        "pt": "Encontrei {n} pessoas perto de {where}:",
    },
    # Lingo rule 4: people are never "matches" — offer intros instead.
    "discovery.peers_tail_verified": {
        "en": "Tell me more about you for sharper intros — or ask me to introduce you to someone.",
        "es": "Cuéntame más de ti para afinar las presentaciones — o pídeme que te presente a alguien.",
        "pt": "Me conta mais sobre você para eu afinar as apresentações — ou peça para eu te apresentar a alguém.",
    },
    "discovery.peers_tail_guest": {
        "en": "Verify your email to see names and connect — or tell me more about you for sharper intros.",
        "es": "Verifica tu correo para ver nombres y conectar — o cuéntame más de ti para afinar las presentaciones.",
        "pt": "Verifique seu e-mail para ver nomes e se conectar — ou me conta mais sobre você para afinar as apresentações.",
    },
    # activity browse (the category prompt QA hit in Spanish)
    "browse.ask_interest": {
        "en": "Love it — what kind of thing are you up for?",
        "es": "Me encanta — ¿qué tipo de plan te apetece?",
        "pt": "Adorei — que tipo de programa você está a fim?",
    },
    "browse.ask_zip": {
        "en": "What's your ZIP code? Activities are grouped by neighborhood — 5 digits is all I "
              "need to show what's happening around you.",
        "es": "¿Cuál es tu código postal (ZIP)? Las actividades se agrupan por zona — con "
              "5 dígitos te muestro qué está pasando a tu alrededor.",
        "pt": "Qual é o seu ZIP code? As atividades são agrupadas por bairro — com 5 "
              "dígitos eu te mostro o que está rolando ao seu redor.",
    },
    "browse.ask_zip_retry": {
        "en": "What's your ZIP so I can see what's happening near you?",
        "es": "¿Cuál es tu ZIP? Así veo qué hay cerca de ti.",
        "pt": "Qual é o seu ZIP? Assim vejo o que tem perto de você.",
    },
    "browse.zip_no_block": {
        "en": "I couldn't find a neighborhood for ZIP {zip}. Try another (e.g. 32827 for Lake Nona).",
        "es": "No encontré una zona para el ZIP {zip}. Prueba con otro (p. ej. 32827 para Lake Nona).",
        "pt": "Não encontrei um bairro para o ZIP {zip}. Tente outro (ex.: 32827 para Lake Nona).",
    },
    # out-of-coverage: the ZIP is real (or can't be disproven) — Lana just isn't there yet.
    # Never "try another ZIP": remember it, record the demand, offer the launch text.
    "zip.out_of_coverage": {
        "en": "I'm not live around {zip} just yet — you're one of the first from your "
              "area! I've saved your spot so I know where to open next. Want me to text "
              "you the moment I arrive?",
        "es": "Todavía no estoy activa en la zona de {zip} — ¡eres de las primeras personas "
              "de tu área! Guardé tu lugar para saber dónde abrir próximamente. ¿Quieres que "
              "te escriba en cuanto llegue?",
        "pt": "Ainda não estou ativa na região do {zip} — você é uma das primeiras pessoas "
              "da sua área! Guardei seu lugar para eu saber onde abrir em seguida. Quer que "
              "eu te avise assim que eu chegar?",
    },
    "zip.expansion_verify_gate": {
        "en": "Perfect — I just need a way to reach you. What's your email? (Already have "
              "an account? I'll log you right in.)",
        "es": "Perfecto — solo necesito cómo contactarte. ¿Cuál es tu email? (¿Ya tienes "
              "cuenta? Te conecto enseguida.)",
        "pt": "Perfeito — só preciso de um jeito de te encontrar. Qual é o seu e-mail? (Já "
              "tem conta? Eu te conecto na hora.)",
    },
    "zip.expansion_saved": {
        "en": "Done — you're on my launch list for {zip}. I'll text you the moment I'm "
              "live there! Anything else in the meantime?",
        "es": "Listo — estás en mi lista de lanzamiento para {zip}. ¡Te escribo en cuanto "
              "esté activa ahí! ¿Algo más mientras tanto?",
        "pt": "Pronto — você está na minha lista de lançamento para o {zip}. Te aviso "
              "assim que eu estiver ativa aí! Algo mais enquanto isso?",
    },
    "zip.expansion_close": {
        "en": "No problem — I'll be here when I reach your area. Anything else I can help "
              "with?",
        "es": "No hay problema — aquí estaré cuando llegue a tu zona. ¿Te ayudo con algo "
              "más?",
        "pt": "Sem problema — estarei aqui quando eu chegar na sua área. Posso ajudar com "
              "mais alguma coisa?",
    },
    "browse.empty_interest_offer": {
        "en": "No **{interest}** activities near you right now. Want me to keep an "
              "ear out and text you the moment one pops up — or widen the search?",
        "es": "No hay actividades de **{interest}** cerca de ti ahora mismo. ¿Quieres que "
              "me quede atenta y te escriba en cuanto aparezca una — o amplío la búsqueda?",
        "pt": "Não tem atividades de **{interest}** perto de você agora. Quer que eu "
              "fique de olho e te avise assim que aparecer uma — ou amplio a busca?",
    },
    "browse.empty_generic_offer": {
        "en": "No matching activities near you right now. Want me to keep an ear out "
              "and text you the moment one pops up — or widen the search?",
        "es": "No hay actividades que encajen cerca de ti ahora mismo. ¿Quieres que me "
              "quede atenta y te escriba en cuanto aparezca una — o amplío la búsqueda?",
        "pt": "Não tem atividades assim perto de você agora. Quer que eu fique de olho "
              "e te avise assim que aparecer uma — ou amplio a busca?",
    },
    "browse.events_header": {
        "en": "Here's what's coming up near you.",
        "es": "Esto es lo que viene cerca de ti.",
        "pt": "Olha o que vem por aí perto de você.",
    },
    "browse.events_header_label": {
        "en": "Here's what's coming up for {label} near you.",
        "es": "Esto es lo que viene de {label} cerca de ti.",
        "pt": "Olha o que vem por aí de {label} perto de você.",
    },
    "browse.events_empty": {
        "en": "Nothing near you in the next couple weeks. Want me to widen it, "
              "try another kind, or set up your own?",
        "es": "No hay nada cerca de ti en las próximas dos semanas. ¿Amplío la búsqueda, "
              "probamos otro tipo de plan, o armas el tuyo?",
        "pt": "Nada perto de você nas próximas duas semanas. Quer que eu amplie a busca, "
              "tente outro tipo, ou você monta o seu?",
    },
    "browse.events_empty_label": {
        "en": "No {label} ones near you in the next couple weeks. Want me to widen it, "
              "try another kind, or set up your own?",
        "es": "No veo planes de {label} cerca de ti en las próximas dos semanas. ¿Amplío la "
              "búsqueda, probamos otro tipo, o armas el tuyo?",
        "pt": "Não vi nada de {label} perto de você nas próximas duas semanas. Amplio a "
              "busca, tentamos outro tipo, ou você monta o seu?",
    },
    "browse.events_tail_verified": {
        "en": "Tap one to RSVP, or tell me to narrow it.",
        "es": "Toca uno para apuntarte, o dime cómo afinar la lista.",
        "pt": "Toque em um para confirmar presença, ou me diga como afinar a lista.",
    },
    "browse.events_tail_guest": {
        "en": "Verify your email to RSVP, or tell me to narrow it.",
        "es": "Verifica tu correo para apuntarte, o dime cómo afinar la lista.",
        "pt": "Verifique seu e-mail para confirmar presença, ou me diga como afinar a lista.",
    },
    # look-meet capture (category ask + verify gate)
    "meet.ask_kind": {
        "en": "Love it — what kind of meet would help?",
        "es": "Me encanta — ¿qué tipo de encuentro te ayudaría?",
        "pt": "Adorei — que tipo de encontro ajudaria?",
    },
    "meet.verify_gate": {
        "en": "Love it — to start listening and text you when a neighbor wants the same, I just need "
              "to verify you. What's your email? (Already have an account? I'll log you right in.)",
        "es": "Me encanta — para quedarme atenta y escribirte cuando alguien busque lo mismo, "
              "solo necesito verificarte. ¿Cuál es tu email? (¿Ya tienes cuenta? Te conecto enseguida.)",
        "pt": "Adorei — para eu ficar de olho e te avisar quando alguém quiser o mesmo, só "
              "preciso verificar você. Qual é o seu e-mail? (Já tem conta? Eu te conecto na hora.)",
    },
    # language preference nudge (asked at most once per session, cooldown across sessions)
    "lang.nudge_offer": {
        "en": "By the way — you've been chatting in {new_name}, but your language is set to "
              "{old_name}. Want me to make {new_name} your default?",
        "es": "Por cierto — has estado escribiendo en {new_name}, pero tu idioma está en "
              "{old_name}. ¿Quieres que ponga {new_name} como tu idioma predeterminado?",
        "pt": "Aliás — você tem escrito em {new_name}, mas seu idioma está em {old_name}. "
              "Quer que eu deixe {new_name} como seu padrão?",
    },
    "lang.pref_saved": {
        "en": "Done — {lang_name} is your language now. You can change it anytime in "
              "Settings, or just tell me.",
        "es": "Listo — {lang_name} es tu idioma ahora. Puedes cambiarlo cuando quieras en "
              "Ajustes, o simplemente dímelo.",
        "pt": "Pronto — {lang_name} é o seu idioma agora. Você pode mudar quando quiser nas "
              "Configurações, ou é só me dizer.",
    },
    # Guest (pre-signup) accept — no account yet, so no Settings mention. Each
    # localized template names its own language; the EN fallback stays generic
    # for languages without a hand template (the AI compose covers them).
    "lang.guest_confirm": {
        "en": "You got it — we'll keep chatting in your language!",
        "es": "¡Perfecto! Seguimos en español.",
        "pt": "Perfeito! Seguimos em português.",
    },
}


def _fmt(text: str, fmt: dict[str, Any]) -> str:
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        return text


# One AI render per (English line, lang) per process — a canned line costs one
# LLM call per language, not one per turn. Failures cache the fallback so a
# broken LLM never adds per-turn latency to deterministic paths.
_AI_RENDER_CACHE: dict[tuple[str, str], str] = {}


def _ai_render(en_text: str, lang: str) -> str | None:
    """Render an English canned line in the session language via the LLM.

    AI-authored, not table-driven, so any language works — the hand-written
    es/pt table below is only the offline fallback. Returns None when no LLM
    is configured (tests, local dev) so callers fall back deterministically."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        label = _LANG_LABEL.get(lang) or f"the language with ISO code '{lang}'"
        register = _REGISTER.get(lang, "warm, neighborly — natural informal register")
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. Rewrite the given chat "
                f"message ENTIRELY in {label} ({register}). Same meaning, same length, "
                "same markdown (keep **bold** spans). Keep proper nouns, numbers, ZIP "
                "codes, and anything a user typed exactly as-is. "
                "Lexicon: never address anyone as mom/mamá/mamãe; never cuadra/quadra "
                "for the area (say the equivalent of 'near you'/'your neighborhood'); "
                "never círculo for a community. When the English is gender-neutral, "
                "stay gender-neutral — rephrase rather than pick a gendered form. "
                'Return JSON {"message": "..."}.'
            ),
            user_payload=en_text,
            # Long deterministic replies (event lists, block summaries) must not
            # truncate mid-sentence — budget scales with the source text.
            max_tokens=max(220, min(1500, len(en_text) // 2)),
            temperature=0.2,
        )
        msg = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg or ""
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("i18n_ai_render_failed")
        return ""  # hard failure — caller caches the fallback, no per-turn retries


def t(key: str, lang: str | None = None, **fmt: Any) -> str:
    """Localized canned string — AI-rendered in the session language, with the
    hand-written es/pt table and then English as fallbacks. Unknown key → the
    key itself (never raises mid-turn). Format placeholders are best-effort."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    lang_norm = str(lang or "en").strip().lower()
    en_text = _fmt(entry["en"], fmt)
    if lang_norm in ("", "en"):
        return en_text
    cache_key = (en_text, lang_norm)
    cached = _AI_RENDER_CACHE.get(cache_key)
    if cached:
        return cached
    fallback = _fmt(entry[lang_norm], fmt) if entry.get(lang_norm) else en_text
    rendered = _ai_render(en_text, lang_norm)
    if rendered is None:
        return fallback  # LLM unconfigured (tests/dev) — don't cache, stay deterministic
    _AI_RENDER_CACHE[cache_key] = rendered or fallback
    return rendered or fallback
