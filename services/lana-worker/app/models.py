from typing import Any, Literal

from pydantic import BaseModel, Field


class HighlightSpan(BaseModel):
    text: str
    bucket: str = "general"


class LanaTurnUi(BaseModel):
    bucket: str | None = None
    focus_phrase: str | None = None
    highlights: list[HighlightSpan] = Field(default_factory=list)


class JointMomentCandidate(BaseModel):
    user_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None


class PeerMatchRow(BaseModel):
    peer_user_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    similarity_score: float | None = None
    matching_peer_label: str | None = None
    matching_my_label: str | None = None
    matching_peer_concept: str | None = None
    has_exact_concept_match: bool = False
    preview: bool = False
    match_stars: int | None = None
    match_band: str | None = None
    match_badge: str | None = None
    trait_tags: list[str] = Field(default_factory=list)
    actions: list["UiActionRow"] = Field(default_factory=list)


class DiscoveryWeakPeerRow(BaseModel):
    peer_user_id: str | None = None
    nickname: str | None = None
    match_stars: int | None = None
    match_badge: str | None = None


class DiscoverySurfacePayload(BaseModel):
    strong_count: int = 0
    partial_count: int = 0
    weak_count: int = 0
    status_label: str | None = None
    weak_peer: DiscoveryWeakPeerRow | None = None
    ranked_summary: str | None = None


class ActivityPreviewRow(BaseModel):
    activity_id: str | None = None
    title: str
    starts_at: str | None = None
    # False = the event has no real clock time; render the date without "12 AM" (#56).
    has_time: bool | None = None
    starts_label: str | None = None
    venue_name: str | None = None
    preview: bool = True


class AuthActionPayload(BaseModel):
    type: str
    phone: str | None = None
    email: str | None = None
    token: str | None = None
    verify_type: str | None = None


class JointMomentPayload(BaseModel):
    joint_moment_id: str | None = None
    status: str | None = None
    candidate: JointMomentCandidate | None = None
    lana_copy: str | None = None
    match_reason: str | None = None
    is_demo: bool = False


class IntroProposalPayload(BaseModel):
    intro_id: str | None = None
    nudge_id: str | None = None
    candidate_user_id: str | None = None
    candidate_nickname: str | None = None
    matching_peer_label: str | None = None
    match_reason: str | None = None
    shared_dimensions: list[str] = Field(default_factory=list)
    status: str | None = None


class PendingIntroRow(BaseModel):
    intro_id: str | None = None
    other_user_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    status: str | None = None
    match_reason: str | None = None
    shared_dimensions: list[str] = Field(default_factory=list)
    direction: str | None = None
    actions: list["UiActionRow"] = Field(default_factory=list)


class UiActionRow(BaseModel):
    """Tap → POST `message` to Lana (same contract as typing in chat)."""
    id: str
    label: str
    message: str
    style: Literal["primary", "secondary", "ghost"] = "primary"
    intro_id: str | None = None
    peer_user_id: str | None = None


class BlockLogEntryRow(BaseModel):
    entry_id: str | None = None
    match_type: str | None = None
    peer_user_id: str | None = None
    peer_preview_label: str | None = None
    match_strength: float | None = None
    match_reasons: list[str] = Field(default_factory=list)
    match_summary: str | None = None
    peer_signal_detail: str | None = None
    peer_signal_intent: str | None = None
    my_signal_detail: str | None = None
    my_signal_intent: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    notification_sent_to_peer: bool = False
    block_id: str | None = None
    block_name: str | None = None


class HostingDraftPayload(BaseModel):
    title: str | None = None
    headline: str | None = None
    when_label: str | None = None
    where_label: str | None = None
    who_label: str | None = None
    trait_tags: list[str] = Field(default_factory=list)
    status_label: str | None = None
    outreach_copy: str | None = None


class TipDraftPayload(BaseModel):
    title: str | None = None
    headline: str | None = None
    where_label: str | None = None
    trait_tags: list[str] = Field(default_factory=list)
    status_label: str | None = None
    outreach_copy: str | None = None


class SignalSavedPayload(BaseModel):
    signal_id: str | None = None
    intent: str | None = None
    category: str | None = None
    detail_text: str | None = None
    block_id: str | None = None
    matches_created: int | None = None
    hosting: "HostingDraftPayload | None" = None
    tip: TipDraftPayload | None = None


class IdentityClaimRow(BaseModel):
    id: str | None = None
    concept: str | None = None
    label: str | None = None
    tone: str | None = None
    confidence: float | None = None
    disclosure: str | None = None
    bucket: str | None = None
    source_quote: str | None = None


class IdentityProfilePayload(BaseModel):
    mapped_summary: str | None = None
    nickname: str | None = None
    block_display_name: str | None = None
    claims: list[IdentityClaimRow] = Field(default_factory=list)


class MappedSpan(BaseModel):
    text: str
    bucket: str = "general"
    claim_concept: str | None = None


class EventDraft(BaseModel):
    title: str | None = None
    description: str | None = None
    venue_name: str | None = None
    # Exact picked place (Google Places) — so neighbors navigate to the right pin.
    venue_address: str | None = None
    place_id: str | None = None
    venue_lat: float | None = None
    venue_lng: float | None = None
    starts_at: str | None = None
    # Whether the host actually gave a clock time. False = starts_at's time component
    # is a midnight placeholder (date-only ask) — cards must render the date alone (#56).
    has_time: bool | None = None
    ends_at: str | None = None
    duration_minutes: int | None = None
    max_attendees: int | None = None
    # Join settings captured in the host flow.
    auto_approve: bool | None = None  # True = anyone joins; False = host approves each
    allow_attendee_share: bool | None = None
    # Items attendees should bring (the 4/4 quick-setup card) → the meet's pinned list.
    bring_items: list[str] = Field(default_factory=list)
    # AI-picked emoji cover (☕🎨⚽…) — the card's visual when there's no cover image.
    cover_emoji: str | None = None
    # AI-tailored quick-setup card config (capacity/sharing/approval/bring labels + bring
    # suggestions), so the FE renders one scrollable carousel of questions fit to THIS event.
    event_setup: dict[str, Any] | None = None
    cohort_tags: list[str] = Field(default_factory=list)
    # Display-only labels for cohort_tags (same order; cohorts.label like "Lifestyle +
    # social"). Chips render these; the ids above stay canonical for publish + matching.
    cohort_tag_labels: list[str] = Field(default_factory=list)
    affinity_prompt: str | None = None
    affinity_options: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ItemChip(BaseModel):
    """A colored entity chip in the 'Heard you' item card. `field` is the draft key
    it represents, so a tap can send `fix:<field>` to re-ask just that entity."""

    label: str
    tone: str = "coral"  # coral | sky | green | amber | violet
    field: str | None = None


class ItemDraft(BaseModel):
    """In-chat 'pass along an item' draft (the swap_offer flow)."""

    title: str | None = None
    category: str | None = None
    condition: str | None = None
    stage: str | None = None
    intent_type: str | None = None  # "free" | "swap"
    photo_url: str | None = None
    chips: list[ItemChip] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    listed: bool = False
    signal_id: str | None = None
    missing: list[str] = Field(default_factory=list)


class TipDraft(BaseModel):
    """In-chat "share a tip / recommendation" draft (the tip_share flow)."""

    name: str | None = None
    category: str | None = None
    trait: str | None = None
    locality: str | None = None
    chips: list[ItemChip] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    ready: bool = False
    listed: bool = False
    signal_id: str | None = None
    missing: list[str] = Field(default_factory=list)


class LookEvent(BaseModel):
    """An existing block meet the seeker could join, surfaced on the ready card."""

    event_id: str
    title: str
    starts_at: str | None = None
    venue_name: str | None = None


class LookDraft(BaseModel):
    """In-chat "looking for a meet / playgroup" draft (the meet_seek flow)."""

    kind: str | None = None
    day: str | None = None
    place: str | None = None
    trait: str | None = None
    affinity: str | None = None
    chips: list[ItemChip] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    # Existing block events matching the seek (the source of truth for real meets).
    events: list[LookEvent] = Field(default_factory=list)
    ready: bool = False
    saved: bool = False
    signal_id: str | None = None
    missing: list[str] = Field(default_factory=list)


class ProfilePhotoUploadResponse(BaseModel):
    profile_photo_url: str


class SignalPhotoUploadResponse(BaseModel):
    photo_url: str


class PlaceSearchRequest(BaseModel):
    q: str


class EventVenueRequest(BaseModel):
    """The exact place the host picked from search — stamped onto the session's event
    draft so publish stores the precise pin (not a re-geocoded name)."""

    name: str
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None


class EventSetupRequest(BaseModel):
    """The host's quick-setup submission from the carousel, stamped onto the session draft
    in one shot so the next turn advances to the final review. Carries the settings
    (capacity / sharing / approval / bring) AND any blockers the opening message didn't
    provide (title / when / where) — so nothing is asked one field per turn."""

    # Blockers the carousel collects when the opening message was sparse.
    title: str | None = None
    starts_at: str | None = None  # naive local ISO (e.g. 2026-07-11T09:00:00); TZ-anchored at publish
    venue_name: str | None = None
    venue_address: str | None = None
    venue_lat: float | None = None
    venue_lng: float | None = None
    place_id: str | None = None
    # Settings.
    max_attendees: int | None = None  # None = no limit
    auto_approve: bool | None = None  # True = anyone joins; False = host approves each
    allow_attendee_share: bool | None = None
    bring_items: list[str] = Field(default_factory=list)


class EventJoinHookRequest(BaseModel):
    """FE calls this right after request_to_join_event so the host + joiner get notified."""

    event_id: str


class EventDecisionHookRequest(BaseModel):
    """FE calls this right after decide_event_request so the requester gets the outcome."""

    request_id: str


class PlaceResult(BaseModel):
    name: str
    address: str = ""
    place_id: str | None = None
    lat: float | None = None
    lng: float | None = None


class PlaceSuggestionRow(BaseModel):
    """A Google Places fallback shown when no neighbor has recommended one yet — rendered as
    tappable cards (maps_url opens the spot in Google Maps). Clearly NOT a neighbor vouch."""
    name: str
    address: str = ""
    place_id: str | None = None
    maps_url: str | None = None


class PlaceSearchResponse(BaseModel):
    results: list[PlaceResult] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    purpose: Literal["lana", "profile_intake", "event_draft"] = "lana"
    force_new: bool = False


class CreateSessionResponse(BaseModel):
    session_id: str
    purpose: str
    status: str
    assistant_message: str
    # lana_messages row id of the opening, so it's 👍/👎-rateable like any other reply.
    assistant_message_id: str | None = None
    ready_to_complete: bool = False
    ui: LanaTurnUi = Field(default_factory=LanaTurnUi)
    event_draft: EventDraft | None = None
    orchestrator: bool = False
    timing_ms: dict[str, int] | None = None
    is_anonymous: bool = False
    phone_verified: bool = False
    home_block_assigned: bool = False
    onboarding_step: str | None = None
    requires_phone_verification: bool = False
    joint_moment: JointMomentPayload | None = None
    intro_proposal: IntroProposalPayload | None = None
    pending_intros: list[PendingIntroRow] = Field(default_factory=list)
    block_log_entries: list[BlockLogEntryRow] = Field(default_factory=list)
    signal_saved: SignalSavedPayload | None = None
    identity_profile: IdentityProfilePayload | None = None
    peer_matches: list[PeerMatchRow] = Field(default_factory=list)
    discovery_surface: DiscoverySurfacePayload | None = None
    activity_previews: list[ActivityPreviewRow] = Field(default_factory=list)
    place_suggestions: list[PlaceSuggestionRow] = Field(default_factory=list)
    auth_intent: str | None = None
    login_phone: str | None = None
    requires_login_otp: bool = False
    login_otp_token: str | None = None
    auth_action: AuthActionPayload | None = None
    active_intent: str | None = None
    routing_phase: str | None = None
    ui_intent: str | None = None
    ui_actions: list[UiActionRow] = Field(default_factory=list)


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    # Deterministic intent from a tapped CTA (e.g. "host_event" from "A meet to host").
    # Lets the server enter a flow without depending on fuzzy classification.
    intent_hint: str | None = None
    # A "By the way…" tile answer (intent_hint="rapport_answer"): the gap being answered and
    # the tile's question, so the worker closes the gap and gives the profile engine context.
    rapport_gap_row_id: str | None = None
    rapport_question: str | None = None


class TurnRouting(BaseModel):
    outcome: str | None = None
    intent_class: str | None = None
    confidence: float | None = None
    tool_called: str | None = None
    capture_fired: bool = False


class TurnDebug(BaseModel):
    """Why this turn routed the way it did — for inbox/debug tooling, not end users."""

    intent: str | None = None
    goal: str | None = None
    confidence: float | None = None
    signal_intent: str | None = None
    active_intent: str | None = None
    routing_phase: str | None = None
    ui_intent: str | None = None
    handler: str | None = None
    orchestrator: bool = False
    event_host_active: bool = False
    slots: dict[str, Any] | None = None


class SendMessageResponse(BaseModel):
    session_id: str
    status: str
    assistant_message: str
    # lana_messages row id of this reply — the FE's 👍/👎 posts it to /lana/feedback.
    # History scroll-back already carries ids; this covers the just-streamed turn.
    assistant_message_id: str | None = None
    ready_to_complete: bool = False
    ui: LanaTurnUi = Field(default_factory=LanaTurnUi)
    event_draft: EventDraft | None = None
    # Set once an event publishes — the FE builds a shareable /meet/{id} link from it.
    event_id: str | None = None
    item_draft: ItemDraft | None = None
    tip_draft: TipDraft | None = None
    look_draft: LookDraft | None = None
    routing: TurnRouting | None = None
    orchestrator: bool = False
    requires_phone_verification: bool = False
    joint_moment: JointMomentPayload | None = None
    intro_proposal: IntroProposalPayload | None = None
    pending_intros: list[PendingIntroRow] = Field(default_factory=list)
    block_log_entries: list[BlockLogEntryRow] = Field(default_factory=list)
    signal_saved: SignalSavedPayload | None = None
    identity_profile: IdentityProfilePayload | None = None
    phone_verified: bool = False
    home_block_assigned: bool = False
    peer_matches: list[PeerMatchRow] = Field(default_factory=list)
    discovery_surface: DiscoverySurfacePayload | None = None
    activity_previews: list[ActivityPreviewRow] = Field(default_factory=list)
    place_suggestions: list[PlaceSuggestionRow] = Field(default_factory=list)
    auth_intent: str | None = None
    login_phone: str | None = None
    requires_login_otp: bool = False
    login_otp_token: str | None = None
    auth_action: AuthActionPayload | None = None
    active_intent: str | None = None
    routing_phase: str | None = None
    ui_intent: str | None = None
    ui_actions: list[UiActionRow] = Field(default_factory=list)


class BlockLogActionRequest(BaseModel):
    action: Literal["nudged", "dismissed", "saved", "ignored"]


class BlockLogListResponse(BaseModel):
    block_id: str | None = None
    block_name: str | None = None
    entries: list[BlockLogEntryRow] = Field(default_factory=list)


class CompleteSessionRequest(BaseModel):
    force: bool = False
    publish: bool = True


class ExtractedClaim(BaseModel):
    concept: str
    label: str
    tone: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    disclosure: str = "public"
    synonyms: list[str] = Field(default_factory=list)
    source_quote: str | None = None
    bucket: str | None = None
    # True for temporary states that are NOT durable identity (a sprained ankle, an
    # illness, an upcoming trip, a passing mood). Persisted for context but kept off
    # the identity wall — see get_my_identity_claims() and dedupe/clean in claims_persist.
    transient: bool = False
    # True when the claim is coarse and a follow-up would sharpen it ("tech worker",
    # "speaks 5 languages" without naming them). Drives a curiosity follow-up.
    vague: bool = False


class CompleteSessionResponse(BaseModel):
    session_id: str
    status: str
    assistant_message: str
    claims: list[ExtractedClaim] = Field(default_factory=list)
    threads_found: int = 0
    mapped_summary: str | None = None
    spans: list[MappedSpan] = Field(default_factory=list)
    event_id: str | None = None
    event_draft: EventDraft | None = None
    published: bool = False


class SessionDetailResponse(BaseModel):
    session_id: str
    purpose: str
    status: str
    context: dict = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)
    mapped_summary: str | None = None
    spans: list[MappedSpan] = Field(default_factory=list)
    event_draft: EventDraft | None = None
