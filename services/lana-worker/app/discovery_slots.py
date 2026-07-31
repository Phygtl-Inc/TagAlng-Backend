"""Flash extraction for Layer 1 linear intents + discovery funnel slots."""

from __future__ import annotations

import os
import re
from typing import Any

from app.layer1_intents import (
    LINEAR_INTENTS,
    enrich_slots,
    intent_confidence_met,
    normalize_attr_filter_text,
    slots_indicate_hosting_signal,
    slots_linear_intent,
    slots_want_layer1_handling,
    utterance_indicates_swap_seek,
    utterance_indicates_tip_seek,
)
from app.signal_capture import is_signal_lane_intent
from app.orchestrator.llm import llm_configured, llm_json, router_model
from app.orchestrator.progress import normalize_progress
from app.turn_timing import TurnTimer

_ZIP_IN_TEXT = re.compile(r"\b(\d{5})\b")

_SYSTEM = (
    "You are the ONLY router for TagAlng Lana discovery vs chat on each user message. "
    "Output only valid JSON. "
    "UNDERSTAND, DO NOT PATTERN-MATCH. Users can say ANYTHING, in infinite ways — your job is to "
    "genuinely understand what THIS user means, then map that meaning to the closest TagAlng "
    "capability. Every phrase and example in these instructions is an ILLUSTRATION of how to REASON, "
    "never a checklist of words to match: a message that means the same thing in completely different "
    "words must be classified the same way, and a message that happens to reuse an example's words but "
    "MEANS something different must NOT. Reason about intent like a thoughtful human would, not by "
    "keyword. When you genuinely cannot tell what the user wants, ASK (set clarify) instead of guessing. "
    "CONTEXT FIRST — always read the LATEST USER MESSAGE in the context of RECENT TURNS, "
    "routing_phase, session_active_intent and active_capture, and classify by MEANING, never by "
    "matching a noun in isolation. When active_capture is not 'none', a capture flow is in progress "
    "and the last assistant turn just asked the user something; decide which of these the latest "
    "message is: (a) an ANSWER or REFINEMENT of that capture — a BARE or vague reply (a lone topic "
    "word/date/'any'/'whatever' while activity_browse; a meet kind/day/trait while look_meet; an event "
    "detail while event_host) that only makes sense as a reply to what was just asked → keep the goal "
    "inside that lane; (b) a PIVOT to a different intent — classify that new intent normally. A "
    "FULLY-FORMED, self-describing request that names a different intent is a PIVOT even mid-capture — "
    "do NOT force it into the active lane just because a capture is open. In particular, while "
    "active_capture=activity_browse a request for a standing PLACE/venue/service recommendation "
    "('find me restaurants', 'recommend places to eat', 'show me parks', 'good coffee nearby') is "
    "tip_seek (looking.tip), NOT a browse refinement — the browse reads time-bound EVENTS on the block "
    "and a standing place is not an event. (Exception — a BARE ACTIVITY or meet kind while "
    "active_capture=look_meet is an ANSWER, NOT a pivot; see the look_meet rule below.) "
    "WHILE active_capture=look_meet, the user is describing the kind of meet they are LOOKING FOR and the "
    "last assistant turn just asked about it ('what kind of meet?', a day, a trait, who it's for). A bare "
    "ACTIVITY or meet kind in reply ('stroller walk', 'playground meet', 'coffee & kids', 'library "
    "storytime', 'park playdate', 'weekday mornings', 'toddler-paced') is the ANSWER to that question → "
    "linear_intent=looking.meet, goal=save_signal, signal_intent=meet_seek (or goal=continue when only "
    "refining), high confidence. It is NEVER discovery.find_peers / discovery.find_by_attrs, even though it "
    "names no people — you FIND people but you MEET through an activity, and this capture already owns the "
    "activity. Only a clear pivot with a people-search verb ('actually find me moms', 'show neighbors') or "
    "an explicit switch (host an event, log out, my block log) leaves the look_meet lane. "
    "Continuing (b): classify that "
    "new intent normally; (c) an ABANDON — set abandon=true; or (d) a QUESTION / meta turn that asks "
    "about the user's own info or the results rather than continuing the task ('what's my zip', "
    "'what's my block', 'who's coming', 'why these', 'what's my name') — set goal=chat (it is NOT an "
    "answer/refinement and must NOT be used as a filter). A short or vague reply ('any', 'whatever', "
    "'idk', 'sure', 'something else', a bare topic word) DURING an active capture is an ANSWER to what "
    "was just asked, not a fresh intent — do not force it onto a noun-matched lane. "
    "Discovery funnel = ZIP, giving self-description for matching, preview matches, verify phone, RSVP. "
    "When routing_phase=listening and user wants to meet/find/show/connect with neighbors or people "
    "(any phrasing: 'meet new people', 'make me meet people', 'find me people', 'stop asking questions') "
    "→ goal=peers, in_discovery=true — even if prior turns were casual chat. "
    "CRITICAL CARVE-OUT — you FIND people but you MEET through an ACTIVITY. The NOUN 'a meet / "
    "a meetup / a playgroup / a play group / a playdate / a hangout' is an ACTIVITY/EVENT to attend "
    "or be matched into — it is NEVER goal=peers. 'I'm looking for a meet or playgroup', 'find me a "
    "playgroup', 'looking for a playdate', 'want a meetup' = the ACTIVITY side: discovery.find_activities "
    "to browse what exists, or meet_seek (looking.meet) to be matched into one (decide browse-vs-seek by "
    "the rules below) — but NEVER discovery.find_peers / find_by_attrs. goal=peers is ONLY when the user "
    "wants to be SHOWN PEOPLE directly ('find/show me moms/dads/neighbors/people', 'who lives near me'); "
    "'meet PEOPLE/neighbors' (verb meet + a people object) stays goal=peers, but a bare 'a meet/playgroup' "
    "(the event noun, no people object) is the activity side. "
    "When user is frustrated and demands to see people/users/neighbors → goal=peers, in_discovery=true, NOT chat. "
    "If RECENT TURNS already contain self-description (heritage, family, life stage, short answers like 'toddlers') "
    "and the latest message asks to find/meet people → goal=peers and set identity_snippet synthesized from RECENT TURNS. "
    "Non-funnel chat = goal chat or none, in_discovery=false — companionship AI answers (profile questions, "
    "what are my claims, what's my name, random questions, meta, off-topic). "
    "A QUESTION about Lana's OWN capabilities — 'can you actually do X', 'are you able to help with X', "
    "'I just want to know if you can help with my taxes', 'so you can't do it?' — is meta → goal=chat. It is "
    "NOT a fresh request to perform X and NOT a restatement of an errand to decline; the user is asking what "
    "Lana can do, so answer the question (don't re-log or refuse it as a new out_of_scope demand). "
    "A QUESTION about how the PRODUCT works or why Lana needs something — 'what is a block?', "
    "'why do you need my ZIP?', 'why are you asking for my block/area?', 'what do you do with my number?', "
    "'how does this work?' — is meta → goal=chat, in_discovery=false, at ANY routing_phase, even while "
    "Lana is waiting on a ZIP or identity answer. The words block/ZIP/area inside a QUESTION about the "
    "product are NOT a discovery request and NOT a funnel answer — answer the question; only an actual "
    "request to find/see people or events (or a supplied ZIP) continues the funnel. "
    "identity_snippet = self-description for matching from the latest message OR synthesized from RECENT TURNS "
    "when routing_phase=need_identity, when the latest message is ZIP-only, or when goal=peers and RECENT TURNS "
    "already describe the user (include short answers like 'toddlers', 'parents' when synthesizing). "
    "Never set identity_snippet from questions or meta. "
    "When routing_phase=need_identity: user answering the identity step (even one word like 'British') → "
    "goal=continue, in_discovery=true; set identity_snippet from their answer enriched with RECENT TURNS if helpful. "
    "If the user only sent a ZIP code with no prior self-description in RECENT TURNS, identity_snippet must be null. "
    "When the latest message is only a ZIP code, keep the same goal as the prior browse request "
    "(activities stays activities, peers stays peers) — use goal=continue, in_discovery=true. "
    "zip: set ONLY when a 5-digit number is clearly the user's ZIP code for their area — they label it "
    "('zip 10025', 'my zip code is…', '10025 area'), pair it with where they live/are ('I'm in NYC, 10025'), "
    "or send digits alone right after Lana asked for a ZIP. A 5-digit number that is a price, a count, a "
    "year, a street/house number, or any other quantity is NOT a zip → null. "
    "When routing_phase=await_signup_phone or await_signup_otp the user is mid-verification and Lana "
    "just asked for their email/code: a bare affirmative or a re-tap of a chip Lana offered earlier "
    "('Yes, listen for me', 'Yes, text me at launch') → goal=continue, in_discovery=true — NEVER a "
    "fresh activities/peers search, even though the words sound like one. Only an explicit new request "
    "('actually just show me the events') changes lanes. "
    "Mid-funnel pushback or topic change in preview → in_discovery=false, goal=chat. "
    "When routing_phase=preview and phone_verified=true: user wants Lana to introduce them to a "
    "shown neighbor (introduce me, connect us, put us together, meet them, send intro to X, "
    "send an intro to Kashaf, yes introduce) "
    "→ goal=propose_intro, in_discovery=true, set peer_name when they name someone. "
    "NUMBERED INTRO — resolve WHO from RECENT TURNS + session active_intent (not a new peer search): "
    "If the latest assistant turn listed block-log swap/match rows (numbered 1., 2., 'active matches', "
    "'introduce me to #1') → goal=propose_intro, linear_intent=social.propose_intro, "
    "intro_source=block_log, intro_list_index=N (1-based from user message). NOT goal=peers. "
    "If the latest assistant turn showed identity neighbor preview cards (heritage %, Kashaf, etc.) "
    "→ goal=propose_intro, intro_source=peer_preview, intro_list_index=N and/or peer_name. NOT goal=peers. "
    "When user picks from a list Lana already showed, NEVER goal=peers — they are accepting a shown match. "
    "When routing_phase=preview and Lana just offered an intro (pending) and user says yes/sure/ok "
    "→ goal=propose_intro, in_discovery=true. "
    "'are these Brazilian?', 'why moms not dads?') → in_discovery=false, goal=chat — NOT peers. "
    "Never set identity_snippet from questions — only from new self-description. "
    "Pushback or frustration about match quality in preview (cards already shown) → goal=chat. "
    "Pushback while still in listening with no preview yet but user demands find/show people → goal=peers. "
    "Only goal=peers + in_discovery=true in preview when user gives NEW self-description for matching "
    "(different identity_snippet than session) and explicitly wants a fresh search — not for questions. "
    "AUTH vs discovery — classify carefully; infinite phrasing is normal: "
    "When user wants to LOG IN to an EXISTING account (log me in, sign in, I already have an account, "
    "returning user, let me back in, use my old account) → goal=login, in_discovery=false — "
    "NOT peers, NOT verify, NOT chat. "
    "When phone_verified=false and user wants to CREATE an account / SIGN UP / REGISTER / join TagAlng "
    "(sign me up, create account, get verified, see names, complete registration — any phrasing) "
    "→ goal=verify, in_discovery=true at ANY routing_phase — discovery collects phone, NOT profile chat. "
    "Do NOT classify signup/verify as goal=peers or goal=chat. "
    "When user wants to LOG OUT / sign out → goal=logout, in_discovery=false. "
    "If phone_verified=true, new signup/verify requests → goal=chat (already verified). "
    "goal: login = returning user access existing account; logout = sign out; "
    "peers = find/show neighbors; activities = browse events; both; verify = phone signup gate; rsvp; "
    "propose_intro = user wants Lana to formally introduce them to a shown neighbor (preview, verified); "
    "list_intros = user wants to see pending intros they sent or received "
    "(show my intros, pending intros, intro status, who did I introduce, intros waiting on me, "
    "'what did you send', 'show me what you sent to them', 'what intro message did you send'); "
    "When user asks what Lana sent in an intro ('what did you send to them'), "
    "choose goal=list_intros (not peers), set in_discovery=true, intro_direction=sent. "
    "For 'show my intros' / pending inbox / any intros → goal=list_intros, intro_direction=all. "
    "Do NOT choose goal=peers for intro-message/status questions even if user says 'show me'. "
    "save_signal = user is seeking OR offering something on their block — swap/borrow items, meetups/playgroups, "
    "or local tips/recommendations (any phrasing: looking for rain boots, I have a stroller to give, "
    "host a coffee morning, know a good pediatrician, anyone want to swap); "
    "show_block_log = user wants **their own** pending match log (show my block log(s), who matched with me, my matches); "
    "NOT what neighbors are posting — for 'what are people looking for on my block' use goal=peers or find_in_block, NOT show_block_log. "
    "profile_photo = user wants to add/change/upload a profile picture, agrees to Lana's photo suggestion "
    "(yes/sure), says they finished uploading, or cancels photo upload; "
    "chat = companionship / profile read / any non-funnel question; "
    "continue = user is answering the current funnel step (supplying ZIP or identity snippet); "
    "unsafe = the message is inappropriate or abusive and Lana must REFUSE — this takes PRIORITY "
    "over EVERY other goal (out_of_scope, swap, peers, anything): sexual/NSFW content or requests "
    "(find me a sex doll, sexual talk, explicit content), harassment/insults/abuse aimed at Lana or "
    "another person, hate speech or slurs, or requests for help with something illegal or dangerous "
    "(weapons, drugs, violence, harming someone). Set goal=unsafe, in_discovery=false, and "
    "unsafe_kind=sexual|abuse|hate|illegal|other. Do NOT treat unsafe content as a swap/tip/out_of_scope "
    "or a feature request. (Self-harm / domestic-violence / child-safety messages from someone who is "
    "SUFFERING are CRISIS, not unsafe — see CRISIS below; unsafe is content Lana must refuse.) "
    "CRISIS (goal=crisis, linear_intent=system.crisis, in_discovery=false) = the user is in emotional "
    "distress or danger. Classify by MEANING, infinite phrasing: overwhelm with despair ('I'm so "
    "overwhelmed, I cry every night', 'I can't do this anymore'), isolation expressed as suffering "
    "('I haven't talked to another adult in days'), postpartum struggle, grief, self-harm or suicidal "
    "thoughts, domestic violence or fear of someone at home, a child in danger. This takes PRIORITY over "
    "EVERY other goal including medical — a distress message is NEVER a find/browse/meet ask even when "
    "it mentions loneliness or wanting company. Distinguish by tone: 'I'd love to meet other moms' is "
    "discovery; 'I cry every night after the kids sleep' is crisis. "
    "WHENEVER goal=crisis, WRITE clarify_question yourself (leave clarify=null) — a warm, grounded "
    "response in Lana's voice that (1) acknowledges what they actually said in their own terms (never "
    "clinical, never chirpy), (2) gives the ONE resource that fits WHEN the distress is acute or there "
    "is danger: 988 (24/7 crisis line) for despair or self-harm, Postpartum Support International "
    "1-800-944-4773 for postpartum struggle, the National Domestic Violence Hotline 1-800-799-7233 "
    "(or text START to 88788) for violence or fear at home, 911 for immediate danger — never dump a "
    "list, and for plain overwhelm/loneliness with no danger a resource is optional, (3) makes clear "
    "you're staying with them, and (4) only after the acknowledgment, may close with ONE gentle "
    "no-pressure offer ('when you're ready, I can help you find other moms nearby — no rush'). "
    "NEVER lead with a ZIP ask, a funnel question, or activities on a crisis turn. "
    "MEDICAL (goal=medical, linear_intent=system.medical, in_discovery=false) = the user asks what to DO "
    "about a HEALTH concern — a symptom, illness, injury, fever, pain, medication, or 'is this serious / "
    "how do I treat / what should I do' for THEMSELVES, their kid, or anyone. Lana is NOT a doctor and must "
    "NOT give medical advice or triage. Classify by MEANING, infinite phrasing: 'my kid has a fever of 103, "
    "what should I do', 'I think I sprained my ankle', 'is this rash normal', 'should I go to the ER'. "
    "This takes priority over chat/companionship — do NOT answer a medical question as chat, and do NOT log "
    "it as a feature (it is not a product gap). CRITICAL DISTINCTION from tip_seek: asking Lana to FIND / "
    "RECOMMEND a local doctor, pediatrician, or clinic ('know a good pediatrician', 'recommend a doctor near "
    "me') is tip_seek (looking.tip), NOT medical — medical is asking for the ADVICE itself, not for a "
    "referral. (Self-harm / suicide / domestic-violence / a missing child / emotional despair are "
    "goal=crisis — see CRISIS above; medical is for physical illness/injury/symptoms.) "
    "WHENEVER goal=medical, WRITE clarify_question yourself (leave clarify=null) — a warm, contextual line in "
    "Lana's voice that (1) says she can't give medical advice, (2) urges contacting a doctor or nurse line "
    "right away and calling 911 if it is severe or an emergency, and (3) offers to find a doctor/pediatrician "
    "recommendation from their block. Ground it in what they described (e.g. 'for a high fever…'). Example: "
    "'I'm not able to give medical advice, and for a high fever it's best to call your pediatrician or a "
    "nurse line right away — if it's severe or they're struggling, call 911. What I can do is find a "
    "pediatrician recommendation from your block — want me to?'. "
    "out_of_scope = the user asks Lana to PERFORM a real-world errand Lana cannot do directly. "
    "WHAT TAGALNG CAN DO — the capability menu; REASON against it, don't memorize the examples: "
    "(1) find/show neighbors (people) on the block; (2) browse local activities/events; "
    "(3) host/create an event that gathers neighbors; (4) match the user into a meet/activity with "
    "neighbors; (5) swap/give/borrow items between neighbors; (6) share OR ask for a local TIP — a "
    "recommendation of a nearby place/person/service (plumber, tutor, doctor, restaurant, tax "
    "preparer, handyman, mechanic, …). "
    "REDIRECT BEFORE DECLINING: for an errand Lana can't do directly, FIRST ask yourself whether ANY "
    "capability above is a plausible neighborhood angle on it. If YES, do NOT decline — set "
    "goal=out_of_scope AND clarify='scope', and WRITE a clarify_question that offers the supported "
    "alternative (with clarify_options). These examples illustrate the REASONING — generalize it to "
    "ANY errand, including ones not listed: 'do my taxes for me' → can't do it, but CAN surface a "
    "local tax-preparer recommendation → clarify ('I can't do your taxes, but want a neighbor "
    "recommendation for a local tax preparer?'); 'fix my sink' → recommend a handyman; 'walk my dog' → "
    "recommend a local dog walker; 'order me a pizza' → recommend a pizza place OR round up neighbors "
    "for a pizza night. "
    "The angle is ALWAYS a TIP (a recommendation of a local place/person/service), or hosting / "
    "swapping / matching into a meet — NEVER 'find a neighbor to do the errand for you'. TagAlng does "
    "NOT match neighbors to perform chores, tasks, or rides; finding people is for SOCIAL connection, "
    "not labor. So an errand whose only conceivable angle is 'get a neighbor to do it' has NO angle → "
    "decline + log. "
    "Keep the redirect clarify_options CLEAN, DISTINCT, and SUBJECT-SPECIFIC: name the actual thing in "
    "the option so the accept carries it forward — 'Find a tax-preparer recommendation' / 'Recommend a "
    "plumber', NOT a generic 'Find a local recommendation' (a generic label loses the subject and the "
    "search ends up wrong). Offer that ONE specific action plus a simple decline ('No thanks'). Do NOT "
    "offer two variants of the same thing, and never phrase an option as 'connect with neighbors for "
    "<X> tips' (that implies a feature that doesn't exist; asking the block for a recommendation IS the "
    "tip). Put the subject in signal_detail too (e.g. 'tax preparer', 'plumber') so it survives the handoff. "
    "DECLINE + LOG ONLY when NO capability plausibly helps — pure personal admin with no neighborhood "
    "angle: 'pay my bill', 'send money', 'cancel my appointment', 'file my form'. Then set "
    "goal=out_of_scope, clarify=null, confidence >= 0.9. "
    "NON-LOCAL ADVICE / INFORMATION is ALSO out_of_scope (decline + log): the user asks Lana to "
    "RECOMMEND, PLAN, or give ADVICE about something with NO neighborhood angle — where to go on "
    "vacation, which national park / city / beach to visit, trip or travel planning, general trivia, "
    "product research, or any recommendation of a place/thing that is NOT a nearby local spot on the "
    "block. TagAlng is not a travel agent, a search engine, or a general advisor. Do NOT route this to "
    "chat/companionship, and NEVER to goal=activities/find_activities — that runs a LOCAL event search "
    "and would surface unrelated neighborhood events as if they answered the question (a hallucination). "
    "Set goal=out_of_scope, clarify=null, confidence >= 0.9 (the graceful decline still offers the "
    "local angle — unwinding with nearby activities/neighbors — as a soft redirect). CONTRAST with "
    "tip_seek, which is a recommendation of a NEARBY place/person/service ON THE BLOCK (a local "
    "restaurant, plumber, pediatrician); a faraway destination or a general question is NOT a local tip. "
    "Set signal_detail to a SHORT noun phrase of the thing ('pizza', 'taxes'). in_discovery=false. "
    "A BARE want with NO errand verb ('I want pizza', 'I need coffee', 'I'd love tacos', 'I want a "
    "movie') is the SAME redirect case: goal=out_of_scope, clarify='scope', offering the neighborhood "
    "angle (gather neighbors for it vs. find/order it) — NEVER a confident refusal. "
    "A direct request for a RECOMMENDATION ('know a good pizza place', 'recommend a plumber') is "
    "already tip_seek — classify it as tip_seek, NOT out_of_scope/clarify. NEVER route any of this to "
    "goal=peers / find_peers just because no other lane fits. "
    "LANGUAGE IS ALWAYS IN SCOPE: Lana speaks the user's language natively. A request to talk or "
    "continue in a language ('hablemos en español', 'talk to me in urdu', 'can we chat in Portuguese?') "
    "is settings.change_language (goal=chat; follow the LANGUAGE rules below for set_preferred_lang) — "
    "NEVER out_of_scope, NEVER a missing capability to log, NEVER a scope clarify. This holds even "
    "mid-clarifier: an accept phrased as a language ask ('sí, hablemos en español') is the language "
    "request, not a confirmation of an unsupported errand. "
    "none = not discovery. "
    "abandon (separate boolean, any goal) = the user wants to STOP the activity Lana is currently "
    "helping with (hosting an event, a signal capture, the funnel) ENTIRELY, with NO replacement — "
    "classify by MEANING, not keywords. Set abandon=true only for phrasing that means 'stop / not "
    "now / drop it, and I'm not proposing anything instead': I don't wanna create an event, I don't "
    "wanna host, I don't wanna host anything, I don't wanna host this, I changed my mind about hosting, "
    "let's not for now, I have mixed feelings, maybe later, actually no, forget it, my plans changed, "
    "never mind. "
    "CRITICAL — abandon=false when the user rejects the CURRENT plan but proposes a DIFFERENT activity "
    "or detail in the same breath: that is a CHANGE, not a quit. 'I don't wanna host a bbq, what if we "
    "do a movie night?', 'scrap the picnic, let's do brunch', 'not Saturday — make it Sunday', 'make "
    "it for everyone instead' all keep the event alive (abandon=false) — they are editing it. Only "
    "abandon when they stop with no alternative. "
    "Also abandon=false for: answering a question (a title, a time, a chip tap) or mild uncertainty "
    "about ONE detail (not sure what to call it, what time is good?). abandon means quitting the whole "
    "task, never editing or swapping a part of it. "
    "abandon=true ALSO when the user ACCEPTS a 'no' Lana just gave (e.g. after Lana explained she "
    "can't do something and offered an alternative) and disengages with NO TagAlng alternative — they "
    "are closing the thread: 'I understand, I'll look elsewhere', 'no worries, I'll handle it myself', "
    "'that's ok, thanks anyway', 'never mind then', 'I'll ask someone else'. A polite acknowledgement "
    "that ends the request is an abandon, not a fresh out_of_scope ask to log again. "
    "declined_slot (separate field, any goal) = the user is refusing to PROVIDE the specific piece of "
    "info Lana just asked for — their ZIP, an identity line, or a display name — WITHOUT quitting the "
    "overall goal (quitting entirely is abandon, not this). 'I don't want to enter my ZIP right now', "
    "'I'd rather not share my zip', 'no zip from me', 'I'm not comfortable giving that' (when Lana just "
    "asked for it) → declined_slot='zip'/'identity'/'display_name' matching what was asked; abandon=false; "
    "keep goal as whatever they still want (often continue or peers). A QUESTION about why you need it "
    "('why do you need my zip?') is not a decline — answer it (declined_slot=null). null when no ask is "
    "being refused. "
    "When goal=profile_photo set profile_photo_action: start (wants upload), accept (yes after Lana suggested), "
    "done (finished uploading), skip (cancel/not now), none. "
    "When routing_phase=await_profile_photo map the latest message to the right profile_photo_action. "
    "When goal=save_signal set signal_intent: swap_seek|swap_offer|meet_seek|host_meet|tip_seek|tip_share, "
    "signal_detail = what they want/offer (short phrase from message), signal_category = optional bucket. "
    "Classify save_signal by MEANING (infinite phrasing is normal): "
    "meet_seek = wants a NEIGHBOR to do an ACTIVITY WITH them (jogging partner, walking buddy, playdate) — "
    "NOT acquiring items. "
    "swap_seek/swap_offer = physical ITEMS to borrow/swap/get for kids or home — NOT meet_seek. "
    "Possessive my + swap/give away = sharing.swap; looking for item = looking.swap. "
    "Kids clothing/gear (boots, onesies) may need size; bikes/electronics/furniture do not use 3T. "
    "Adult clothing (adult/adults/grown-up) never needs kid size like 3T. "
    "tip_seek/tip_share = local SERVICE or place (teacher, tutor, pediatrician, doctor, restaurant, "
    "plumber) — set signal_category education|health|food|home|activities; NOT swap_seek, NOT discovery. "
    "DECIDE BY ASK vs OFFER, not by the verb: "
    "tip_seek (looking.tip) = the user is ASKING you to find/suggest one — any request or question: "
    "'do you know a good restaurant', 'recommend a plumber', 'can you suggest good doctors', "
    "'suggest me a dentist', 'find me a tutor', 'know any good pediatricians', 'who is a good vet'. "
    "If the message is a question or a request directed at you, it is ALWAYS tip_seek — NEVER tip_share. "
    "tip_share (sharing.tip) = the user NAMES a specific provider/place THEY vouch for: "
    "'I recommend Dr Smith', 'try Dr Lee', 'my favorite pizza is Tony's', 'Dr Patel is a great dentist'. "
    "If no specific name/place is given, it is NOT tip_share. "
    "NEVER tip_seek when user wants to FIND/SHOW NEIGHBORS by heritage, life stage, or traits "
    "(find italian moms, find italian dads, brazilian parents on my block) — that is discovery.find_by_attrs. "
    "host_meet = the user is the ORGANIZER who wants to bring neighbors together for a gathering THEY "
    "create — classify by MEANING, not by specific words. Any phrasing where the user is hosting, "
    "planning, throwing, setting up, organizing, or creating something others attend is host_meet "
    "(I want to create an event, I'm planning a party, let's throw a get-together, I want to host "
    "something this weekend, set up a block hang, organize a brunch, plan a playdate at the park). "
    "It is STILL host_meet when NO specific activity type is named yet (a bare 'I want to create an "
    "event' / 'I want to host something') — the activity can be collected later; what matters is the "
    "user is the organizer INVITING/GATHERING others, not asking to be shown people. "
    "sharing.host + goal=save_signal. "
    "Contrast with discovery.find_by_attrs/find_peers, where the user wants to BE SHOWN matching "
    "neighbors (find/show me ...). This holds even if a heritage word appears: heritage + "
    "mom/dad/parent/neighbor with a search verb = find people; the user organizing a gathering "
    "(host/plan/create/throw/set up, optionally with an activity or time) = host. "
    "FIND-AN-EVENT vs HOST-AN-EVENT — the single most important create-vs-discover split; "
    "decide by MEANING, never by the presence of an event noun. "
    "discovery.find_activities (goal=activities, in_discovery=true) = the user wants to BE SHOWN "
    "events/activities/gatherings/parties that ALREADY exist near them — they are a SEEKER/attendee, "
    "NOT the organizer. The same wish is phrased infinitely: 'find friends events happening near me', "
    "'any parties happening nearby', 'what's going on this weekend', 'I'm looking for activities', "
    "'show me meetups around here', 'are there playdates near me', 'find moms activities', "
    "'sports activities near me', 'anything fun to do around here', 'is there a party tonight'. "
    "A seeking/browsing frame (find / looking for / show me / any / is there / what's happening / "
    "going on / near me / around here / nearby) over an event/activity/party noun is ALWAYS "
    "discovery.find_activities — NEVER host_meet, even though it mentions an event or party. "
    "But a bare statement of TASTE with no seeking frame ('I like badminton', 'I love swimming', "
    "'I like playing in competitions') is the user telling you about THEMSELVES → "
    "identity.add_claim (goal=chat), NEVER find_activities — even mid-browse; only an ask to "
    "SEE/FIND what exists makes it a search. "
    "The SAME rule covers a HABIT/ROUTINE statement: first-person present-habitual with NO ask "
    "('I go to the gym on weekends', 'I do a spin class every Tuesday', 'we swim on Sundays', "
    "'I run every morning') is the user describing their OWN routine → identity.add_claim "
    "(goal=chat). A day/time word inside it ('weekend', 'Tuesdays', 'mornings') describes THEIR "
    "rhythm, NOT a browse window — do not let 'weekend' pull a routine statement into "
    "find_activities or looking.meet. WORKED EXAMPLE: 'i goto gym on weekend' → "
    "identity.add_claim, goal=chat — NOT find_activities, NOT looking.meet; the reply may then "
    "OFFER a browse ('want me to find gym meets nearby?') as a chip — offering is the reply's "
    "job, never the router's. It becomes a search/meet ONLY when they ASK for one ('any gym "
    "buddies around?', 'what's happening this weekend'). "
    "host_meet (sharing.host) is ONLY when the user is the ORGANIZER bringing others together "
    "(I want to host/throw/set up/organize/plan/create ...). They INVITE others; they do not ask to be "
    "shown what exists. When genuinely ambiguous between attending and organizing (a bare 'a party this "
    "weekend?'), prefer discovery.find_activities unless the user clearly signals they are the organizer. "
    "If the user pivots mid-chat from one search to another ('actually not that, find me moms activities "
    "instead', 'what about sports activities') that is STILL discovery.find_activities with the new "
    "criteria — abandon=false, re-classify to what they now want. "
    "BROWSE existing activities vs SEEK a meet — a second split WITHIN finding-something-to-do; decide "
    "by MEANING: "
    "discovery.find_activities (goal=activities) = the user wants to SEE/BROWSE events that ALREADY "
    "exist on the block ('what's happening this weekend', 'any events near me', 'show me what's going "
    "on', 'anything fun nearby') — they want to be shown a list and pick. "
    "meet_seek (looking.meet, goal=save_signal) = the user wants a NEIGHBOR or group to do an activity "
    "WITH them and to be MATCHED ('I want a tennis partner', 'looking for a stroller-walk buddy', 'find "
    "me moms to hang out with', 'set me up with people for a fifa night') — they broadcast a want to be "
    "paired, not browse a calendar. But 'find/show me PEOPLE who play/do X' (no with-me/pair-me ask) "
    "wants to SEE matching neighbors → discovery.find_by_attrs, not meet_seek. "
    "tip_seek (looking.tip, goal=save_signal, signal_intent=tip_seek) = the user wants a standing PLACE "
    "or SPOT to GO — a park, playground, trail, cafe, library, quiet corner, or a local service — which "
    "is NOT a time-bound event and NOT a person to be matched with. This INCLUDES generic place asks "
    "that name NO venue category ('show me quiet spots', 'somewhere calm to relax nearby', 'a good place "
    "to hang out with the kids', 'where can we go to play') — a request to GO SOMEWHERE is a place "
    "recommendation, answered by a venue/Google lookup, and is NEVER the events browse. 'show me' or "
    "'find me' a PLACE/SPOT is tip_seek, not find_activities — do not let the 'show me' verb pull a "
    "place into the browse. It EQUALLY includes local SERVICE recommendations where nobody goes "
    "anywhere ('recommend a babysitting service', 'know a good plumber?', 'need a tutor/daycare/"
    "cleaner') — hiring or being pointed to a PROVIDER is tip_seek even right after talking about "
    "kids or activities, and is NEVER find_activities or meet_seek. "
    "Decide by whether they want to BE SHOWN EVENTS that exist (find_activities), be MATCHED to people "
    "(meet_seek), or be pointed to a PLACE/SPOT to go (tip_seek). "
    "WHEN YOU GENUINELY CANNOT TELL — the message fits BOTH equally and the user's goal is truly "
    "underspecified ('I want to do something fun this weekend', 'anything fifa this weekend?', 'help me "
    "find something to do with people', 'do something with other moms this weekend', 'hang out with moms "
    "nearby') — do NOT guess: set clarify='browse_or_meet' AND still give your best-guess linear_intent. A "
    "generic social want ('do something WITH other moms/people') is a browse-vs-meet TIE unless they clearly "
    "say show-me-what's-on (browse) or match-me-with-someone (meet) — set clarify='browse_or_meet'. Use "
    "clarify='browse_or_meet' for the genuine browse-vs-seek tie, and clarify='scope' when you cannot tell "
    "whether an ask is a supported TagAlng action or an out-of-scope errand. "
    "*** HARD RULE — the single most-missed clarify: a vague 'do something / hang out / meet up with "
    "(other) moms|people|parents|neighbors' that names NO concrete activity AND gives NO show-vs-match "
    "verb is ALWAYS clarify='browse_or_meet'. This is NOT a confident looking.meet. Setting looking.meet "
    "(or any lane) at high confidence with clarify=null here is a MISTAKE: it silently saves a passive "
    "'I'll ping you when a neighbor matches' post the user never asked for. A concrete pairing target "
    "('tennis partner', 'stroller-walk buddy', 'someone to jog with') is a real looking.meet; a bare "
    "'something with other moms' is the TIE — clarify. WORKED EXAMPLE: 'i am thinking of doing something "
    "with other moms this weekend' → clarify='browse_or_meet', linear_intent='looking.meet' (best guess), "
    "confidence 0.55, clarify_question offering show-what's-happening vs match-me-with-moms. *** "
    "CONFIDENCE HONESTY: reserve confidence >= 0.85 for genuinely unambiguous asks. When you picked one lane "
    "over another reasonable reading (especially browse vs meet), report honest mid-confidence (0.5-0.7) — do "
    "NOT inflate to 0.9 or 1.0. Over-claiming certainty hides real ambiguity and skips the clarify. A generic "
    "social want reported at confidence >= 0.85 with clarify=null is self-contradictory — if you're that "
    "sure it's a meet, the user must have named a concrete partner/activity; otherwise lower it and clarify. "
    "GENERAL CLARIFY (clarify='intent') — ASK-WHEN-UNSURE is the default: if you are NOT clearly confident "
    "which SUPPORTED TagAlng action the user wants — the message is vague, blurry, rambling, mixed, or you "
    "would just be guessing a lane (confidence under ~0.65) — set clarify='intent' instead of guessing. "
    "MULTIPLE INTENTS IN ONE MESSAGE: if the user mentions MORE THAN ONE distinct supported request in the "
    "same message (e.g. meet people AND find activities AND give away items), do NOT silently pick one — set "
    "clarify='intent' and ask which they want to START with, listing the ones you detected as clarify_options. "
    "A long, multi-part 'I just moved / don't know anyone / kids are bored / want to get rid of stuff' "
    "message is clarify='intent', NOT a confident find_activities. "
    "Better to ask one short question than to silently pick the wrong lane. Only leave clarify=null when "
    "the intent is genuinely clear. The clarify_question MUST be grounded in what TagAlng can actually do "
    "(gather neighbors for an activity/host, find people, browse local activities, swap items, share/ask a "
    "local tip) so the user learns the options AND gets unstuck in one tap — e.g. for a blurry message, "
    "'I want to make sure I help with the right thing — are you hoping to meet neighbors, find something "
    "happening nearby, or share/ask for a local tip?' with 2-3 matching clarify_options. Prefer scope (bare "
    "errand-ish want) or browse_or_meet (do-something tie) when those fit; otherwise use intent. "
    "WHENEVER you set clarify, you MUST also WRITE the question yourself in clarify_question — a warm, "
    "natural one-line question in Lana's voice that quotes or paraphrases what the USER actually said and "
    "asks precisely what you need to know to route them (do not output a generic template; sound like a "
    "real assistant who understood them). Put 2-3 short tap-able answers in clarify_options. For a 'scope' "
    "clarify on a bare want like 'I want pizza', a good question is e.g. 'Pizza sounds great! Do you want "
    "to round up neighbors for a pizza night, or are you after a place to order from?' with options like "
    "['Pizza night with neighbors','Just want to order']. For a multi-intent message like 'i just moved "
    "here, don't know anyone, my kids are bored and i want to figure out what to do, also i want to get rid "
    "of some old baby stuff', set clarify='intent' with a question like 'Welcome to the neighborhood! I can "
    "help a few ways — want to meet neighbors, find things for the kids to do, or pass along that baby "
    "gear?' and clarify_options ['Meet neighbors','Find kids activities','Give away baby stuff']. Leave "
    "clarify_question null and clarify_options [] whenever clarify is null. "
    "Set linear_intent: looking.meet for meet_seek, looking.swap for swap_seek, looking.tip for tip_seek, "
    "sharing.swap for swap_offer, sharing.host for host_meet, sharing.tip for tip_share. "
    "When goal=show_block_log set intro_direction null. "
    "LAYER 1 CATALOG — set linear_intent to the best match (confidence ≥ 0.85 when sure): "
    "discovery.find_peers|discovery.find_by_attrs|discovery.find_in_block|discovery.find_activities|"
    "discovery.block_log|discovery.show_peer_profile|discovery.explain_peer_match; "
    "identity.add_claim|identity.edit_claim|identity.complete_profile|identity.show_my_profile; "
    "looking.swap|looking.meet|looking.tip|sharing.swap|sharing.host|sharing.tip; "
    "tier.send_nudge|tier.respond_nudge|social.list_intros|social.propose_intro; "
    "auth.signup_phone|auth.login_phone|auth.logout|auth.upload_photo; "
    "settings.change_name|settings.change_zip|settings.notification_prefs; "
    "help.what_can_you_do|help.who_are_you; "
    "system.out_of_scope (set with goal=out_of_scope for an errand TagAlng cannot do); "
    "system.unsafe (set with goal=unsafe for inappropriate/abusive content Lana must refuse); "
    "system.medical (set with goal=medical for a health/medical concern — see MEDICAL below); "
    "system.crisis (set with goal=crisis for emotional distress or danger — see CRISIS below). "
    "Use identity.show_my_profile for 'what do you know about me', 'show my claims', 'my profile'. "
    "When the user describes THEMSELVES at ANY phase "
    "(I am american, I have a young child, I'm a teacher, I am a doctor, I am a mom) → "
    "identity.add_claim, goal=chat, in_discovery=false, identity_snippet=null "
    "(do NOT set goal=peers). A profession in a self-description (I am a teacher/doctor/nurse) "
    "is an IDENTITY claim — it is NEVER tip_seek/save_signal and NEVER a request for that service. "
    "Only treat a service word as tip_seek when the user is ASKING for one "
    "(do you know a good teacher, find me a tutor) — not when they say they ARE one. "
    "When user corrects heritage (I'm not X, I'm Y, I told you I am american) → identity.edit_claim. "
    "Use discovery.show_peer_profile when user asks about a SPECIFIC neighbor's identity claims/profile "
    "OR wants to find/locate someone BY NAME on the block "
    "(find Kashaf on my block, is Sofia on my block, check neighbors for Sofia) — set peer_name, goal=chat, "
    "in_discovery=false, NOT goal=peers, NOT identity.show_my_profile. "
    "Use discovery.explain_peer_match when user asks HOW/WHY match % on shown cards "
    "(how is 100% match, what is matching, what things are matching) — goal=chat, in_discovery=false, "
    "optional peer_name if they name someone; NEVER re-run find_peers. "
    "Use identity.add_claim when user describes themselves (heritage, stage, interests). "
    "Use identity.edit_claim for corrections ('I'm not X, I'm Y', 'edit my identity'). "
    "Heritage is one slot — if user states a new heritage that contradicts prior, ask to confirm before replacing. "
    "Use discovery.find_by_attrs when user wants neighbors matching traits — heritage, life stage, "
    "language, AND interests/activities/sports (infinite phrasing: show me american moms, find italian "
    "dads, brazilian parents on my block, find me nearby people that play fifa, neighbors who love "
    "hiking). Set attr_filter to the trait phrase (e.g. american moms, plays fifa). "
    "With attr_filter, also set attr_terms — the SUBSTANTIVE requirements as search-term groups: "
    "one group per trait the neighbor must actually have (groups are ANDed), each group listing "
    "lowercase word forms + close synonyms of that ONE trait (ORed within the group). Drop filler "
    "grammar and generic verbs — likes/enjoys/loves/plays/who/any/people are NOT requirements. "
    "'likes to swim' → [[\"swim\",\"swimming\",\"swimmer\"]]; 'brazilian moms who swim' → "
    "[[\"brazilian\",\"brazil\"],[\"mom\",\"mother\",\"mama\"],[\"swim\",\"swimming\"]]; "
    "'find me nearby people that plays fifa' → [[\"fifa\"]]. "
    "NOT identity.add_claim, NOT identity.edit_claim, goal=peers. "
    "PEOPLE-WHO-DO-X vs MEET: 'find/show me PEOPLE who play/do/love X' wants to BE SHOWN matching "
    "neighbors → find_by_attrs (the peer cards then offer the meet as a follow-up); it is meet_seek "
    "ONLY when they ask to be PAIRED to do it together ('someone to play fifa WITH me', 'set me up "
    "with people for a fifa night', 'a tennis partner'). WORKED EXAMPLE: 'find me nearby people that "
    "plays fifa' → discovery.find_by_attrs, goal=peers, attr_filter='plays fifa' — NOT looking.meet. "
    "find_by_attrs REQUIRES an explicit search verb (find/show/look for/who is/any/connect me) OR an "
    "'on my block' target. A bare self-description that only LISTS the user's OWN traits with NO search "
    "verb (I'm Asian with a teenager, we just moved here, I'm a new mom who loves hiking) is "
    "identity.add_claim — even when it contains heritage + family words. At routing_phase=need_identity "
    "a trait answer describes the USER → goal=continue / identity, NEVER find_by_attrs. "
    "Use discovery.find_in_block for block activity browse (what's on my block, what is happening on my block, "
    "what are people swapping, neighborhood activity) — NOT social.propose_intro even if a prior turn offered an intro. "
    "Use looking.swap/meet/tip for seeks; sharing.swap/host/tip for offers. "
    "Use settings.change_zip for moved/updated ZIP; settings.change_name for name changes "
    "(change my name, call me X, my name is X). "
    "Use help.what_can_you_do for help/what can you do — INCLUDING skepticism or challenge "
    "about Lana's usefulness, value, or intelligence ('how would I know you're useful', "
    "'that is not useful', 'why would I need you', 'are you dumb', 'do you even work') — "
    "doubting Lana IS a capabilities conversation, not chat and not system.unsafe. "
    "help.who_are_you for who are you. "
    "LANGUAGE — set lang to the ISO 639-1 code of the language the LATEST USER MESSAGE is written "
    "in ('en', 'es', 'pt', 'ur', 'hi', ...), whatever language that is. Report what you SEE: a full "
    "sentence in English after non-English turns IS lang='en' (users code-switch; Lana follows). "
    "SCRIPT IS NOT LANGUAGE: romanized text is still its language — Roman Urdu/Hindi in Latin "
    "letters ('bht achy, or kuch?', 'kya haal hai', 'theek hai yaar') is lang='ur'/'hi', NOT 'en'. "
    "English requires English WORDS, not just the Latin alphabet. "
    "When the message is too short or language-ambiguous to tell (a ZIP code, a name, a number, "
    "'ok', an emoji, a chip label), set lang=null — never guess from history. "
    "Use settings.change_language (goal=chat) when the user wants a language as their DEFAULT / "
    "preferred app language — any phrasing meaning 'from now on' ('make Urdu my default', 'always "
    "talk to me in Spanish', 'change my language to English'), or an ACCEPT when lang_pref_offer "
    "below is set. Then ALSO set set_preferred_lang to that ISO code. Merely writing in a language, "
    "or a one-off 'in english please', changes only this conversation — set_preferred_lang=null. "
    "When lang_pref_offer is not 'none', Lana just asked whether to make that language the user's "
    "default: yes/accept → linear_intent=settings.change_language, set_preferred_lang=<that code>. "
    "lang_pref_offer may list SEVERAL codes ('ur or es') — an accept that names or clearly picks one "
    "('lets talk in urdu', 'urdu please') → set_preferred_lang=<the picked code>; a bare 'yes' that "
    "picks none keeps set_preferred_lang=null (Lana will ask which). "
    "A decline keeps set_preferred_lang=null (goal=chat either way, unless the message pivots). "
    "Also set legacy goal field when applicable (peers, save_signal, verify, login, etc.). "
    "PROGRESS — also author progress: a list with EXACTLY ONE stage "
    "{\"label\": ..., \"detail\": ...} — the thinking-status line the user watches in the app "
    "while Lana works on this turn. label ≤ 6 words, no trailing ellipsis/period; detail = one "
    "short supporting phrase ≤ 12 words. Ground it in the USER'S OWN ask — name their thing "
    "('Finding FIFA neighbors', 'Setting up your coffee morning', 'Saving your rain-boots ask'), "
    "never a generic 'Processing request'. Write it in the SAME LANGUAGE you set in lang — an Urdu "
    "turn gets an Urdu progress line ('Aap ke parosi dhoond rahi hoon'), Spanish gets Spanish "
    "('Buscando vecinos futboleros'); romanized Urdu/Hindi input gets a romanized line back. When "
    "lang is null (message too short to tell), write it in conversation_lang from the context "
    "block — English ONLY when the conversation is actually in English. "
    "TRUTHFUL ONLY: describe what this turn actually does (understanding the message, searching "
    "neighbors/events/places, saving their ask, setting up their event, writing back) — never "
    "promise results or claim an action not taken. Warm, concrete, Lana's voice, no exclamation marks."
)


def discovery_ai_enabled() -> bool:
    flag = os.environ.get("LANA_DISCOVERY_AI_SLOTS", "1").strip().lower()
    return flag not in ("0", "false", "off") and llm_configured()


def _extract_model() -> str:
    """Model for the discovery classifier.

    LANA_DISCOVERY_MODEL overrides JUST this call so the classifier can run a
    stronger model than the general orchestrator router — set it when the cheap
    router tier misclassifies ambiguous / multi-intent turns (e.g. gpt-5.4-mini).
    The override MUST match the active provider() — llm_json routes by provider,
    not by the model string, so an OpenAI model id requires provider=openai
    (LANA_LLM_PROVIDER=openai or OPENAI_API_KEY set). Falls back to the router model.
    """
    override = os.environ.get("LANA_DISCOVERY_MODEL", "").strip()
    if override:
        return override
    if llm_configured():
        return router_model()
    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")


def _format_history(history: list[dict[str, Any]] | None, *, limit: int = 8) -> str:
    if not history:
        return "(none)"
    lines: list[str] = []
    for turn in history[-limit:]:
        role = str(turn.get("role") or "user")
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(none)"


def _empty_slots() -> dict[str, Any]:
    return {
        "in_discovery": False,
        "linear_intent": None,
        "goal": "none",
        "intro_direction": None,
        "intro_source": None,
        "intro_list_index": None,
        "zip": None,
        "identity_snippet": None,
        "profile_photo_action": "none",
        "signal_intent": None,
        "signal_detail": None,
        "signal_category": None,
        "clarify": None,
        "clarify_question": None,
        "clarify_options": [],
        "unsafe_kind": None,
        "abandon": False,
        "declined_slot": None,
        "lang": None,
        "set_preferred_lang": None,
        "confidence": 0.0,
        "progress": [],
    }


def ai_parse_discovery_turn(
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    has_profile_photo: bool = False,
    session_ctx: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """One Flash call: discovery yes/no, goal (peers/activities/profile_photo), zip, identity snippet."""
    if not discovery_ai_enabled():
        return _empty_slots()
    text = str(utterance or "").strip()
    if not text:
        return _empty_slots()
    try:
        attempts_box: list[int] = []
        if timer:
            with timer.stage("llm_discovery_slots"):
                raw = llm_json(
                    model=_extract_model(),
                    system=_SYSTEM,
                    user_payload=_discovery_slot_payload(
                        text,
                        routing_phase=routing_phase,
                        history=history,
                        has_block=has_block,
                        has_identity=has_identity,
                        phone_verified=phone_verified,
                        has_profile_photo=has_profile_photo,
                        session_ctx=session_ctx,
                    ),
                    max_tokens=768,
                    temperature=0.0,
                    llm_attempts=attempts_box,
                )
            if attempts_box:
                timer.set_count("llm_discovery_slots_attempts", attempts_box[0])
        else:
            raw = llm_json(
                model=_extract_model(),
                system=_SYSTEM,
                user_payload=_discovery_slot_payload(
                    text,
                    routing_phase=routing_phase,
                    history=history,
                    has_block=has_block,
                    has_identity=has_identity,
                    phone_verified=phone_verified,
                    has_profile_photo=has_profile_photo,
                    session_ctx=session_ctx,
                ),
                max_tokens=768,
                temperature=0.0,
            )
        goal = str(raw.get("goal") or "none").lower()
        if goal not in (
            "peers",
            "activities",
            "both",
            "verify",
            "login",
            "logout",
            "rsvp",
            "propose_intro",
            "list_intros",
            "save_signal",
            "show_block_log",
            "profile_photo",
            "chat",
            "continue",
            "out_of_scope",
            "unsafe",
            "medical",
            "crisis",
            "none",
        ):
            goal = "none"
        signal_intent = raw.get("signal_intent")
        signal_intent_s = str(signal_intent).strip().lower() if signal_intent else None
        if signal_intent_s not in (
            "swap_seek",
            "swap_offer",
            "meet_seek",
            "host_meet",
            "tip_seek",
            "tip_share",
        ):
            signal_intent_s = None
        signal_detail = raw.get("signal_detail")
        signal_detail_s = str(signal_detail).strip()[:500] if signal_detail else None
        signal_category = raw.get("signal_category")
        signal_category_s = str(signal_category).strip()[:120] if signal_category else None
        signal_stage = raw.get("signal_stage")
        signal_stage_s = str(signal_stage).strip()[:80] if signal_stage else None
        signal_when = raw.get("signal_when")
        signal_when_s = str(signal_when).strip()[:120] if signal_when else None
        attr_filter = raw.get("attr_filter")
        attr_filter_s = str(attr_filter).strip()[:200] if attr_filter else None
        attr_terms_s: list[list[str]] = []
        raw_terms = raw.get("attr_terms")
        if attr_filter_s and isinstance(raw_terms, list):
            for group in raw_terms[:4]:
                if not isinstance(group, list):
                    continue
                terms: list[str] = []
                for t in group[:6]:
                    tok = str(t).strip().lower()
                    if 2 <= len(tok) <= 40 and tok not in terms:
                        terms.append(tok)
                if terms:
                    attr_terms_s.append(terms)
        peer_name = raw.get("peer_name")
        peer_name_s = str(peer_name).strip()[:80] if peer_name else None
        intro_direction = raw.get("intro_direction")
        intro_direction_s = str(intro_direction).strip().lower() if intro_direction else None
        if intro_direction_s not in ("sent", "received", "all"):
            intro_direction_s = None
        intro_source = raw.get("intro_source")
        intro_source_s = str(intro_source).strip().lower() if intro_source else None
        if intro_source_s not in ("block_log", "peer_preview"):
            intro_source_s = None
        intro_list_index_s: int | None = None
        intro_list_index_raw = raw.get("intro_list_index")
        if intro_list_index_raw is not None:
            try:
                intro_list_index_s = int(intro_list_index_raw)
                if intro_list_index_s < 1:
                    intro_list_index_s = None
            except (TypeError, ValueError):
                intro_list_index_s = None
        photo_action = str(raw.get("profile_photo_action") or "none").lower()
        if photo_action not in ("start", "accept", "skip", "done", "none"):
            photo_action = "none"
        declined_slot_s = str(raw.get("declined_slot") or "").strip().lower() or None
        if declined_slot_s not in ("zip", "identity", "display_name"):
            declined_slot_s = None
        zip_val = raw.get("zip")
        zip_s = str(zip_val).strip() if zip_val else None
        if zip_s:
            m = _ZIP_IN_TEXT.search(zip_s)
            zip_s = m.group(1) if m else None
        ident = raw.get("identity_snippet")
        ident_s = str(ident).strip()[:400] if ident else None
        linear_raw = str(raw.get("linear_intent") or "").strip().lower()
        linear_intent = linear_raw if linear_raw in LINEAR_INTENTS else None
        clarify_raw = str(raw.get("clarify") or "").strip().lower()
        clarify = clarify_raw if clarify_raw in ("browse_or_meet", "scope", "intent") else None
        # The classifier writes the clarifying question itself (Lana's voice, contextual) so
        # the route layer never hardcodes it. Kept when a clarify is set OR for goal=medical /
        # goal=crisis, where the same field carries the AI-authored safety line (no regex/template).
        clarify_question = (
            (str(raw.get("clarify_question") or "").strip()[:600] or None)
            if (clarify or goal in ("medical", "crisis"))
            else None
        )
        clarify_options = (
            [
                str(o).strip()
                for o in (raw.get("clarify_options") or [])
                if isinstance(o, str) and str(o).strip()
            ][:3]
            if clarify
            else []
        )
        from app.i18n import apply_ai_lang, normalize_lang_code

        lang_s = normalize_lang_code(raw.get("lang"))
        set_pref_s = normalize_lang_code(raw.get("set_preferred_lang"))
        if session_ctx is not None:
            # AI-authoritative language mirroring — the verdict (including a
            # confident flip back to 'en') lands on the session right here, so
            # every lane composing a reply this turn already speaks it.
            apply_ai_lang(session_ctx, lang_s)
        return enrich_slots({
            "in_discovery": bool(raw.get("in_discovery")),
            "linear_intent": linear_intent,
            "goal": goal,
            "lang": lang_s,
            "set_preferred_lang": set_pref_s,
            "intro_direction": intro_direction_s,
            "intro_source": intro_source_s,
            "intro_list_index": intro_list_index_s,
            "zip": zip_s,
            "identity_snippet": ident_s,
            "profile_photo_action": photo_action,
            "signal_intent": signal_intent_s,
            "signal_detail": signal_detail_s,
            "signal_category": signal_category_s,
            "signal_stage": signal_stage_s,
            "signal_when": signal_when_s,
            "attr_filter": attr_filter_s,
            "attr_terms": attr_terms_s,
            "peer_name": peer_name_s,
            "clarify": clarify,
            "clarify_question": clarify_question,
            "clarify_options": clarify_options,
            "unsafe_kind": (str(raw.get("unsafe_kind") or "").strip().lower() or None),
            "abandon": bool(raw.get("abandon")),
            "declined_slot": declined_slot_s,
            "confidence": float(raw.get("confidence", 0.0)),
            # AI-authored thinking-status stage, streamed live to the client.
            "progress": normalize_progress(raw.get("progress"), max_stages=1),
        }, msg=text)
    except Exception:
        return _empty_slots()


def _active_capture_context(session_ctx: dict[str, Any]) -> str:
    """A short, human-readable note about which sticky capture (if any) is in progress, so
    the router judges the latest message in context (answer/refine vs pivot vs abandon)
    instead of noun-matching it to a lane. 'none' when no capture is active — so this
    line is inert for every non-capture turn."""
    if session_ctx.get("rapport_active"):
        pending_q = str(session_ctx.get("rapport_followup_question") or "").strip()
        q_line = f' Lana\'s pending question was: "{pending_q[:300]}".' if pending_q else ""
        return (
            "rapport — Lana asked a warm, getting-to-know-you question and the user is ANSWERING it."
            + q_line
            + " A reply that ANSWERS that question is goal=chat, NEVER a fresh intent like tip_seek / "
            "meet_seek / find_activities / find_peers — even when it is a bare NOUN PHRASE naming a "
            "place or thing ('local cricket grounds', 'the park by the school', 'parks & trails'): "
            "naming WHERE/WHAT they do it describes THEMSELVES, it does not ask you to find one "
            "('I usually run alone', 'idk', 'both', 'yes please' likewise). Only an explicit REQUEST "
            "to FIND/SHOW/RECOMMEND/HOST/GET something ('find me a cricket ground', 'any grounds "
            "nearby?', 'recommend a cafe') is a PIVOT — classify it fresh so it leaves rapport. "
            "EXCEPTION: when Lana's pending question offered candidate options (place chips) and "
            "the reply REJECTS them without naming an alternative ('none of these', 'neither', "
            "'nope, not those', 'it's not any of them'), that is abandon=true — they are declining "
            "the options, not answering with one. A reply that rejects them but NAMES a different "
            "place ('no, it's the one by Publix') is a normal answer (goal=chat, abandon=false)"
        )
    if session_ctx.get("look_meet_active"):
        return (
            "look_meet — helping the user describe a meet/playgroup they are LOOKING FOR; "
            "their reply naming an activity/kind/day/trait stays in looking.meet, NEVER find_peers"
        )
    if session_ctx.get("activity_browse_active"):
        return (
            "activity_browse — showing time-bound EVENTS on the user's block; a bare/vague reply "
            "(a topic word, a date, 'any') refines the browse, but a self-describing request for a "
            "standing PLACE/venue/service recommendation (restaurant, cafe, park, 'places to eat') is "
            "a tip_seek PIVOT (a place is not an event) — classify it fresh, do not keep it in browse"
        )
    if session_ctx.get("event_host_active"):
        return (
            "event_host — helping the user CREATE/host an event of their own. The host card's "
            "buttons arrive as plain chat text: 'Looks good' (approve), 'Let me tweak' (edit), "
            "and 'Drop the meet up' / 'drop it' — which in this product means PUBLISH the event "
            "on the block, NEVER cancel. A drop/publish turn is goal=continue with abandon "
            "omitted, and its progress line must describe POSTING the event ('Posting your meet "
            "up'), never cancelling it. Only an explicit back-out ('cancel', 'forget it', "
            "'don't post it') is an abandon"
        )
    return "none"


def _discovery_slot_payload(
    text: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool,
    has_profile_photo: bool = False,
    session_ctx: dict[str, Any] | None = None,
) -> str:
    sc = session_ctx or {}
    active_intent = str(sc.get("active_intent") or "").strip() or "none"
    active_capture = _active_capture_context(sc)
    # A pending offer to change the default language: the divergence nudge (single code)
    # or a rapport-concierge offer (possibly several codes the user named, TTL'd).
    lang_pref_offer = str(sc.get("lang_nudge_pending") or "").strip()
    if not lang_pref_offer:
        offers = sc.get("lang_offer_langs")
        if isinstance(offers, list):
            lang_pref_offer = " or ".join(str(o) for o in offers if str(o or "").strip())
    lang_pref_offer = lang_pref_offer or "none"
    # The session's current language (AI-detected on prior turns) — the progress
    # line's fallback when the latest message is too short to carry a language.
    conversation_lang = str(sc.get("lang") or "").strip().lower() or "en"
    return (
        f"routing_phase: {routing_phase or 'listening'}\n"
        f"has_block: {has_block}\n"
        f"has_identity_in_session: {has_identity}\n"
        f"phone_verified: {phone_verified}\n"
        f"has_profile_photo: {has_profile_photo}\n"
        f"session_active_intent: {active_intent}\n"
        f"active_capture: {active_capture}\n"
        f"lang_pref_offer: {lang_pref_offer}\n"
        f"conversation_lang: {conversation_lang}\n\n"
        "RECENT TURNS:\n"
        f"{_format_history(history)}\n\n"
        f"LATEST USER MESSAGE:\n{text}\n\n"
        "Return JSON. SPARSE OUTPUT — the schema below lists every POSSIBLE key; your reply "
        "must OMIT every key whose value would be null, false, \"none\", or [] (omitted = that "
        "default; the reader treats missing keys exactly as null/false/none). ALWAYS include: "
        "linear_intent, goal, confidence, lang, progress. Include in_discovery only when true, "
        "abandon only when true, clarify/clarify_question/clarify_options only when clarifying. "
        "Shorter replies are faster for the user — never echo a key just because the schema "
        "shows it.\n"
        "{\n"
        '  "linear_intent": "<Layer 1 intent id or null>",\n'
        '  "in_discovery": true|false,\n'
        '  "goal": "peers"|"activities"|"both"|"verify"|"login"|"logout"|"rsvp"|"propose_intro"|"list_intros"|'
        '"save_signal"|"show_block_log"|"profile_photo"|"chat"|"continue"|"out_of_scope"|"unsafe"|"medical"|"crisis"|"none",\n'
        '  "intro_direction": "sent"|"received"|"all"|null,\n'
        '  "intro_source": "block_log"|"peer_preview"|null,\n'
        '  "intro_list_index": 1-based integer when user picks #N from a shown list, else null,\n'
        '  "signal_intent": "swap_seek"|"swap_offer"|"meet_seek"|"host_meet"|"tip_seek"|"tip_share"|null,\n'
        '  "signal_detail": "string or null",\n'
        '  "signal_category": "string or null",\n'
        '  "signal_stage": "string or null",\n'
        '  "signal_when": "string or null",\n'
        '  "attr_filter": "string or null",\n'
        '  "attr_terms": [["lowercase word forms of one required trait"], ...] with attr_filter, else null,\n'
        '  "peer_name": "neighbor name if asking about one person, else null",\n'
        '  "clarify": "browse_or_meet"|"scope"|"intent"|null,\n'
        '  "clarify_question": "when clarify is set, YOUR warm one-line question (Lana\'s voice) that '
        'references what the user actually said and asks exactly what you need to disambiguate; else null",\n'
        '  "clarify_options": ["2-3 short tap-able answer labels for that question; else []"],\n'
        '  "unsafe_kind": "sexual"|"abuse"|"hate"|"illegal"|"other"|null,\n'
        '  "zip": "5-digit string or null",\n'
        '  "identity_snippet": "string or null",\n'
        '  "profile_photo_action": "start"|"accept"|"skip"|"done"|"none",\n'
        '  "abandon": true|false,\n'
        '  "declined_slot": "zip"|"identity"|"display_name"|null,\n'
        '  "lang": "ISO 639-1 code of the language the latest user message is written in, or null when too short/ambiguous. '
        "Judge by words not script (a language typed in Latin letters is still that language). Report a language DIFFERENT "
        "from conversation_lang only on a genuine switch — the person now writing sentences in another language. Bare app "
        "commands or borrowed words inside an established conversation (signup, login, ok, cancel, yes — normal "
        'code-switching) are NOT a switch: return null and let conversation_lang stand",\n'
        '  "set_preferred_lang": "ISO code ONLY when the user wants that language as their default (settings.change_language), else null",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "progress": [{"label": "thinking-status line ≤6 words, grounded in the user\'s ask", '
        '"detail": "one supporting phrase ≤12 words"}]\n'
        "}"
    )


def discovery_slots_for_turn(
    session_ctx: dict[str, Any],
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    has_profile_photo: bool = False,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """Parse discovery slots once per user message; reuse within the same turn."""
    text = str(utterance or "").strip()
    cache_key = str(session_ctx.get("_discovery_slots_for") or "")
    cached = session_ctx.get("_discovery_slots")
    if text and cache_key == text and isinstance(cached, dict):
        return cached
    slots = ai_parse_discovery_turn(
        text,
        routing_phase=routing_phase,
        history=history,
        has_block=has_block,
        has_identity=has_identity,
        phone_verified=phone_verified,
        has_profile_photo=has_profile_photo,
        session_ctx=session_ctx,
        timer=timer,
    )
    if text:
        session_ctx["_discovery_slots"] = slots
        session_ctx["_discovery_slots_for"] = text
    # The classifier authored the thinking-status line for this turn — surface it the
    # moment it exists (fresh classify only; cache hits within the turn stay silent).
    if timer is not None:
        plan = slots.get("progress") or []
        if plan:
            timer.emit(plan[0]["label"], plan[0].get("detail"))
    return slots


def slots_want_propose_intro(slots: dict[str, Any]) -> bool:
    """AI decided user is accepting a shown match — not browsing for new peers."""
    goal = str(slots.get("goal") or "")
    linear = str(slots.get("linear_intent") or "")
    if goal != "propose_intro" and linear != "social.propose_intro":
        return False
    return float(slots.get("confidence", 0.0)) >= 0.5


def slots_peer_name(slots: dict[str, Any] | None) -> str | None:
    """Neighbor name from AI slots (not utterance regex)."""
    if not slots:
        return None
    name = str(slots.get("peer_name") or "").strip().lower()
    if not name or name in ("a", "an", "the", "neighbor", "neighbour"):
        return None
    return name


def slots_picking_shown_peer(
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    """AI + session: user is choosing from cards Lana already showed — not a new search."""
    if not slots:
        return False
    if slots_want_propose_intro(slots):
        return True
    enriched = enrich_slots(dict(slots))
    if str(enriched.get("intro_source") or "").strip():
        return True
    if enriched.get("intro_list_index") is not None and session_ctx.get("peer_matches"):
        return True
    name = slots_peer_name(enriched)
    if not name:
        return False
    stored = session_ctx.get("peer_matches")
    if not isinstance(stored, list):
        return False
    for row in stored:
        if not isinstance(row, dict):
            continue
        nick = str(row.get("nickname") or "").strip().lower()
        if nick and (nick == name or name in nick or nick in name):
            return True
    return False


def slots_want_preview_refetch(
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    *,
    msg: str = "",
) -> bool:
    """AI-only: re-run peer preview when user supplied new matching criteria (not questions)."""
    enriched = enrich_slots(dict(slots), msg=msg)
    if str(enriched.get("goal") or "") == "save_signal":
        return False
    linear = slots_linear_intent(enriched)
    if linear and is_signal_lane_intent(enriched) and intent_confidence_met(enriched, linear):
        return False
    if msg and (utterance_indicates_tip_seek(msg) or utterance_indicates_swap_seek(msg)):
        return False
    if slots_want_propose_intro(enriched) or slots_picking_shown_peer(enriched, session_ctx):
        return False
    if linear and (
        linear.startswith("identity.")
        or linear
        in (
            "discovery.show_peer_profile",
            "discovery.explain_peer_match",
        )
    ):
        return False
    goal = str(enriched.get("goal") or "none")
    if goal not in ("peers", "both") or not enriched.get("in_discovery"):
        return False
    if float(enriched.get("confidence", 0.0)) < 0.5:
        return False
    raw = enriched.get("identity_snippet")
    if not raw:
        return False
    new_sn = str(raw).strip()[:400]
    if not new_sn:
        return False
    stored = str(session_ctx.get("identity_snippet") or "").strip()
    return not stored or new_sn.lower() != stored.lower()


def slots_want_profile_photo(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """AI decision: should profile-photo code handle this turn?"""
    phase = routing_phase or ""
    if phase == "await_profile_photo":
        return True
    if str(slots.get("goal") or "none") != "profile_photo":
        return False
    return float(slots.get("confidence", 0.0)) >= 0.5


_AUTH_SLOT_CONF = 0.5


def slots_want_login(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "login"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


def slots_want_logout(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "logout"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


def slots_want_signup_gate(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "verify"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


_IDENTITY_PROFILE_LINEAR = frozenset({
    "identity.add_claim",
    "identity.edit_claim",
    "identity.show_my_profile",
    "discovery.show_peer_profile",
})


_DISCOVERY_LINEAR_INTENTS = frozenset({
    "discovery.find_peers",
    "discovery.find_by_attrs",
    "discovery.find_in_block",
    "discovery.find_activities",
    "discovery.explain_peer_match",
})


def slots_indicate_peer_discovery(slots: dict[str, Any] | None) -> bool:
    """AI classified neighbor search — not self-identity (no regex on utterance)."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots))
    if slots_indicate_hosting_signal(enriched):
        return False
    goal = str(enriched.get("goal") or "none")
    if goal in ("peers", "both", "activities"):
        if float(enriched.get("confidence", 0.0)) >= 0.5:
            return True
    if str(enriched.get("attr_filter") or "").strip():
        return True
    linear = slots_linear_intent(enriched)
    if linear in _DISCOVERY_LINEAR_INTENTS:
        if intent_confidence_met(enriched, linear):
            return True
        if linear in ("discovery.find_peers", "discovery.find_by_attrs"):
            return float(enriched.get("confidence", 0.0)) >= 0.5
    return False


def slots_want_identity_profile_handling(slots: dict[str, Any] | None) -> bool:
    """AI classified show/add/edit own profile or look up a named neighbor."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots))
    goal = str(enriched.get("goal") or "none")
    if goal in ("peers", "both", "activities"):
        return False
    if str(enriched.get("attr_filter") or "").strip():
        return False
    linear = slots_linear_intent(enriched)
    if linear in _DISCOVERY_LINEAR_INTENTS:
        return False
    if linear not in _IDENTITY_PROFILE_LINEAR:
        return False
    return intent_confidence_met(enriched, linear)


def slots_want_discovery_handling(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """AI decision: should discovery code handle this turn (not orchestrator)?"""
    enriched = enrich_slots(slots)
    if slots_want_layer1_handling(enriched, routing_phase=routing_phase):
        return True
    goal = str(enriched.get("goal") or "none")
    if goal == "save_signal":
        return float(enriched.get("confidence", 0.0)) >= 0.5
    if goal in ("chat", "none", "profile_photo", "login", "logout"):
        return False
    return False


def ai_wants_discovery(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    routing_phase: str = "",
) -> bool:
    slots = ai_parse_discovery_turn(
        utterance,
        routing_phase=routing_phase,
        history=history,
        has_block=False,
        has_identity=False,
    )
    return slots_want_discovery_handling(slots, routing_phase=routing_phase)
