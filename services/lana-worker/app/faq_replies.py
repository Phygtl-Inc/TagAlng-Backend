"""Static template answers for the four product-FAQ intents.

QA (2026-07-08) found four direct questions — safety, who-is-this-for, childcare, ZIP
privacy — that each got a canned funnel line or an event dump instead of an answer. These
replies are deterministic (no LLM), factually grounded in the product, and each ends with a
gentle re-offer of the ongoing goal so the flow the question interrupted can resume.

Copy lives here (mirrors layer1_handlers.HELP_*) so the pipeline's early gate and the
Layer 1 handler share one source. Detection lives in layer1_intents.faq_linear_intent.
"""

from __future__ import annotations

FAQ_SAFETY = (
    "Totally fair question — you should ask it. Every neighbor here is email-verified and "
    "matched to their real block before I introduce anyone, and nobody ever sees your address "
    "or exact location. Connections only happen when you say yes, and you can block or report "
    "anyone in one tap. Whenever you're ready, we can pick up right where we left off."
)

FAQ_WHO_FOR = (
    "You're absolutely welcome here. TagAlng is moms-first, but it's for anyone caring for "
    "little ones on the block — stay-at-home dads, grandparents, and caregivers included. And "
    "if you're expecting, it's not too early at all: plenty of moms join before their first "
    "arrives, so neighbors are already lined up when baby comes. Want to keep going from "
    "where we were?"
)

FAQ_CHILDCARE = (
    "I wish I could help there, but childcare and babysitting are outside what I can do — I "
    "can't arrange or provide a sitter myself. What I can do is ask your block: neighbors are "
    "the best source of sitter recommendations they actually trust. Want me to put that "
    "question out, or shall we pick up where we left off?"
)

FAQ_ZIP_PRIVACY = (
    "That's completely okay to ask. I only use your ZIP to find your block so I can match you "
    "with true neighbors — it's never shown to neighbors, and your address is never shared "
    "either. Whenever you're comfortable, share it and we'll pick up right where we left off."
)

FAQ_REPLY_BY_INTENT: dict[str, str] = {
    "help.faq_safety": FAQ_SAFETY,
    "help.faq_who_for": FAQ_WHO_FOR,
    "help.faq_childcare": FAQ_CHILDCARE,
    "help.faq_zip_privacy": FAQ_ZIP_PRIVACY,
}

# Short topic names for analytics (faq_answered events).
FAQ_TOPIC_BY_INTENT: dict[str, str] = {
    "help.faq_safety": "safety",
    "help.faq_who_for": "who_for",
    "help.faq_childcare": "childcare",
    "help.faq_zip_privacy": "zip_privacy",
}


def faq_reply(linear_intent: str) -> str | None:
    return FAQ_REPLY_BY_INTENT.get(str(linear_intent or ""))


def faq_topic(linear_intent: str) -> str:
    return FAQ_TOPIC_BY_INTENT.get(str(linear_intent or ""), "unknown")
