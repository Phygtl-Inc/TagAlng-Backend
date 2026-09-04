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
    # ── The authored reason (app/peer_rec_line.py) ───────────────────────────────────
    # One AI-written line in Lana's voice saying what these two actually share, from the
    # same proven overlap `trait_tags` lists. The fellows card renders this instead of the
    # chips; the tags stay on the wire because the chat card still renders them. `rec_id`
    # is the stored line's id — the target a 👍/👎 posts to /lana/feedback. Absent on every
    # row we could not author (no overlap, no LLM, a failed compose), which is why the tags
    # remain the fallback rather than a canned sentence.
    rec_line: str | None = None
    # The same reason as 2-3 short facets ("Runs at dawn", "Author talks"), authored in the
    # same call from the same overlap. The card leads with these as chips under "WHY LANA
    # SEES A FIT" and keeps `rec_line` beneath them. Empty whenever no facet could be named
    # honestly (and on every row with no authored line at all) — the chips are a view of
    # the proven overlap, never a grade and never a claim the line doesn't already make.
    rec_chips: list[str] = Field(default_factory=list)
    rec_id: str | None = None
    # "intro_sent" (an intro is on its way) or "connected" (they already know each other,
    # per user_relationships.tier). Either way the card shows a status, not a Nudge button:
    # Lana saying "I just sent your intro" must not sit above a control inviting the same
    # action, and nudging an existing connection can only hit the 7-day pair cooldown.
    connection: str | None = None
    # "member" | "curious" on a community-roster row (app/community_discovery.py). The
    # community screen tags a curious joiner and chat could not: the field is on the
    # roster row already, but had nowhere to land here, so pydantic dropped it and a
    # watcher rendered identically to someone who actually goes there. None on every
    # other kind of row — nothing outside a roster has a membership to state.
    membership: str | None = None
    actions: list["UiActionRow"] = Field(default_factory=list)
    # ── The recommendation cascade (§12a/b) ──────────────────────────────────────────
    # What this neighbor actually recommended, in their own words, and the tip_share row
    # it came from (so a tap can open or attribute it). Present only on a looking.tip turn
    # where this neighbor posted a matching tip — absent everywhere else, including on
    # claim-affinity matches, which have no rec behind them.
    tip_text: str | None = None
    tip_signal_id: str | None = None
    # Honest distance phrase ("a few minutes away", "1.4 mi away") straight from
    # humanize_distance_text. None whenever either side's coarse point is unknown —
    # never a guess, and never confused with matching_peer_label (a shared thread).
    distance_text: str | None = None
    # ── Circle provenance on a rec (C-FIND-V2) ───────────────────────────────────────
    # The places BOTH the viewer and this recommender belong to. The results screen groups
    # by these ("ST MARY'S CHURCH" over the rec, "YOUR BLOCK" over one with no shared
    # place) because the shared circle is WHY the rec is worth trusting — a stranger's
    # recommendation and one from someone you sit next to are not the same claim.
    # Shared only: never this person's other memberships, which are theirs to disclose.
    shared_circles: list["SharedCircleRow"] = Field(default_factory=list)
    same_block: bool = False
    # Which heading the row sits under. `group_key` is a place id for a circle, else the
    # literal "block" / "nearby"; `group_label` is the place's own name, and null for the
    # other two so the surface writes and translates that heading itself.
    group_key: str | None = None
    group_label: str | None = None
    group_kind: str | None = None


class SharedCircleRow(BaseModel):
    """One place the viewer and a recommender both belong to."""

    place_id: str
    name: str
    circle_type: str | None = None


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
    # The community this meet was created for, when it has one — same shape as everywhere
    # else, so the browse row names it beside the venue.
    community: dict[str, Any] | None = None
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


class AskDraftChip(BaseModel):
    """An entity chip on the ask-draft card. `field` is the part of the ask it stands for
    (category | locality | qualifier), so a tap can re-ask just that one."""

    label: str
    tone: str = "coral"  # coral | sky | green | amber | violet
    field: str | None = None


class AskDraftPayload(BaseModel):
    """What Lana understood a recommendation SEEK to be, read back before anything is
    broadcast (frame C-4-look-tip-P2). The share-side twin is TipDraft; this is the seek
    side, and it is a receipt of understanding — never a promise that anything was posted."""

    title: str | None = None
    detail: str | None = None
    category: str | None = None
    locality: str | None = None
    chips: list[AskDraftChip] = Field(default_factory=list)
    ready: bool = False


class GroundingCardOption(BaseModel):
    """One real place on a grounding card. `suggested` marks a nearby place of the right
    KIND rather than one bearing the name the user gave — copy must ask, never assert."""

    label: str
    address: str | None = None
    google_place_id: str | None = None
    send: str
    suggested: bool = False


class GroundingCardPayload(BaseModel):
    """The "which spot is it?" card, on a CHAT turn (C-CIRCLE-GROUND).

    Deliberately the same shape the home tile's ask already has, so the FE renders the
    one component both places: pick-one grid, Google search box, non-punishing skip.
    Chat used to get bare chips, so a user whose place wasn't in the list had no way to
    search for it and the two surfaces disagreed about the same question (2026-08-18).

    The tile-only fields are stubs here: this ask is a live turn, not a queued gap, so
    there is no gap row to answer or skip — the card posts a normal message back."""

    kind: str = "place_grounding"
    gap_row_id: str = ""
    gap_id: str = "chat_grounding"
    parent_bucket: str = "vicinity"
    why_frame: str = ""
    sensitivity_tier: str = "LOW"
    chip_color_token: str = "sky"
    question: str
    affiliation_id: str
    options: list[GroundingCardOption] = Field(default_factory=list)
    circle_type: str | None = None
    relation_noun: str | None = None
    emoji: str | None = None
    place_name: str | None = None
    detail: str | None = None
    # The name we searched for and could not find — the card leads with it instead of
    # showing an empty grid.
    unmatched_name: str | None = None


class CommunityEventRow(BaseModel):
    event_id: str
    title: str
    starts_at: str | None = None
    # False = the meet has no real clock time; render the date alone (#56).
    has_time: bool = True
    venue_name: str | None = None
    # What the meet is, in the host's words — the public place page shows it under the title.
    description: str | None = None
    # The real going roster — the only thing "popular" is ordered on.
    going_count: int = 0
    # The meet's AI-picked cover glyph, so this row wears the same face as the meet's
    # own card. None falls back to the FE's calendar.
    cover_emoji: str | None = None


class MeetGoingPreviewRow(BaseModel):
    """One face on a meet's avatar stack. Stranger-tier, the same two fields the member
    cards show — and absent entirely for an unverified caller (§F)."""

    user_id: str
    nickname: str | None = None
    profile_photo_url: str | None = None


class CommunityMeetRow(CommunityEventRow):
    """A meet on the all-meets screen: the card row plus the two things that screen
    renders and no other surface needs — the host's own copy, and who is going."""

    # None = the host never wrote one; never synthesised ([[event-description-gap]]).
    description: str | None = None
    going_preview: list[MeetGoingPreviewRow] = Field(default_factory=list)


class CommunityMeetGroup(BaseModel):
    """One community's collapsible group on C-CIRCLE-COMMS-ALL."""

    affiliation_id: str
    place_id: str | None = None
    place_name: str | None = None
    circle_type: str | None = None
    emoji: str | None = None
    upcoming_count: int = 0
    meets: list[CommunityMeetRow] = Field(default_factory=list)


class CommunityMeetsResponse(BaseModel):
    """Every meet across the caller's communities, soonest first inside each group and
    between them. Groups with nothing upcoming are omitted; `total` is how many
    communities she holds, so "3 of your 7 have something on" is sayable."""

    communities: list[CommunityMeetGroup] = Field(default_factory=list)
    total: int = 0


class CommunityCardRow(BaseModel):
    """One of the caller's communities on the look screen (C-CIRCLE-LOOK-COMMS).

    `place_id` is what the profile / people endpoints are keyed on. Counts are real
    reads, and `status_line` is the same two facts already in the numbers — render
    either, never a claim beyond them.
    """

    affiliation_id: str
    place_id: str | None = None
    place_name: str | None = None
    place_address: str | None = None
    circle_type: str | None = None
    # Caller-relative noun for the place ("gym", "school") — never the word "circle".
    relation: str | None = None
    # Card art for the community's TYPE (🏋️ / ⛪ / 🎨), the same job events.cover_emoji
    # does for a meet. Deterministic per type, so it never varies between surfaces;
    # advisory — render your own icon set instead if you prefer.
    emoji: str | None = None
    member_count: int = 0
    meets_this_week: int = 0
    # What's actually on at this place, soonest first — each row's event_id opens that
    # meet. Empty when nothing is coming up; the row is still listed.
    meets: list[CommunityEventRow] = Field(default_factory=list)
    # Everything upcoming there, so a badge over `meets` (a slice of two) is truthful.
    upcoming_count: int = 0
    active: bool = False
    status_line: str | None = None


class CommunitiesCardPayload(BaseModel):
    """The "YOUR COMMUNITIES" card — the caller's top three plus how many more there
    are ("View N more" → the Radar Communities tab). Absent on every turn except the
    looking-open one, and absent (not empty) when the user has no community yet."""

    items: list[CommunityCardRow] = Field(default_factory=list)
    total: int = 0
    more_count: int = 0


class CommunityDiscoveryRow(BaseModel):
    """A community near the caller that they could join (C-CIRCLE-COMM-DISCOVER).

    Carries NO member identities — who is there is members-only, so joining is what
    earns the names. `member_count` includes the caller when `is_member` is true,
    which is why `status_line` phrases those two cases differently.
    """

    place_id: str
    place_name: str | None = None
    place_address: str | None = None
    place_type: str | None = None
    relation: str | None = None
    emoji: str | None = None
    zip: str | None = None
    member_count: int = 0
    # Coarse (block/ZIP centroid) distance phrase, or null when either point is
    # unknown — never a guess.
    distance_text: str | None = None
    is_member: bool = False
    status_line: str | None = None


class FellowsResponse(BaseModel):
    """The caller's matched fellows, ranked and badged exactly as Lana's chat cards.

    Same rows, same shaper (peers_to_match_rows), so the fellows screen and the
    conversation can never disagree about who matches or how strongly. Replaces the
    direct find_my_fellows RPC, which read the vector arm alone: it saw neither the
    onion (shared-place / exact-concept) matches nor the public+mutual disclosure split,
    so a faith or sobriety overlap counted in chat and was invisible on the screen.
    """

    fellows: list[PeerMatchRow] = Field(default_factory=list)
    # Everyone arrives with a session — the PWA signs guests in anonymously on first
    # visit — so "signed out" is not the state that gates this surface; VERIFICATION is.
    # The shaper nulls nickname/peer_user_id for an unverified caller, so the rows are
    # real matches with their identities withheld. Named the same as the members
    # endpoint's flag so both gated surfaces read alike.
    requires_phone_verification: bool = False


class CommunityDiscoveryResponse(BaseModel):
    communities: list[CommunityDiscoveryRow] = Field(default_factory=list)
    # The radius actually searched, in metres — so an empty list can be explained
    # ("nothing within ~5 miles") rather than looking like a bug.
    radius_meters: int = 0


class CommunityJoinResponse(BaseModel):
    """The result of joining. `source` is where the community first came from and is
    NOT overwritten by joining: a place the user mentioned in chat and later joined
    from the panel reads source='chat_extraction', confirmed_via='community_join'."""

    affiliation_id: str
    place_id: str
    place_name: str | None = None
    status: str = "confirmed"
    # What the joiner said she is to the place: 'member' (counted, named, matched) or
    # 'curious' (hers to watch, excluded from counts, rosters and matching).
    membership: str = "member"
    already_member: bool = False
    source: str | None = None
    confirmed_via: str | None = None
    joined_via_label: str | None = None
    # True when the join confirmed an existing candidate of theirs rather than
    # creating a new row (they had mentioned the place before).
    promoted_from_candidate: bool = False


class CommunityFeatureRow(BaseModel):
    """A feature members actually volunteered about the place ("Pool", "Childcare").
    Never inferred from the place type — if nobody said it, it isn't here."""

    key: str
    label: str
    sub_group: str | None = None
    # Picked when the feature was written; null on rows learned before 20261010.
    emoji: str | None = None
    # The caller contributed this one, so /features/remove will take it back — the
    # only rows the card should render an × on (issues #77).
    mine: bool = False


class CommunityActivityRow(BaseModel):
    """Something people DO at this place ("Aerobics"). `mine` is the caller's own —
    the same list is the "your activities" chips and the "add more" menu, so what
    the other members do is the only suggestion offered."""

    concept: str
    label: str
    member_count: int = 0
    mine: bool = False


class CommunityMemberPreviewRow(BaseModel):
    peer_user_id: str
    nickname: str | None = None
    avatar_url: str | None = None
    # The caller's own face. member_count has always counted her, so she travels in the
    # list too and the flag keeps neighbour-only affordances off her row (§17).
    me: bool = False


class CommunityProfileResponse(BaseModel):
    """One community, for the people who go there (C-CIRCLE-COMM-PROFILE)."""

    place_id: str
    affiliation_id: str | None = None
    place_name: str | None = None
    place_address: str | None = None
    circle_type: str | None = None
    # Invite label for POST /lana/invites/mint — the "Invite people" CTA is native FE
    # (mint + share sheet), so its input rides here instead of in `actions`.
    circle_key: str | None = None
    # Provenance: where the community came from (`source`) vs the action that made it
    # real (`confirmed_via`); `joined_via_label` is the one-phrase render of the pair.
    source: str | None = None
    confirmed_via: str | None = None
    joined_via_label: str | None = None
    relation: str | None = None
    emoji: str | None = None
    detail: str | None = None
    # The caller's own relationship to the place: 'member'; 'curious' — she joined to watch
    # it (§19); or 'visitor' — she has no row here and opened it from a peer's profile or
    # discovery. Only 'member' gets a roster and the host/invite actions.
    membership: str = "member"
    member_count: int = 0
    active: bool = False
    status_line: str | None = None
    # AI-authored from the real facts below (features / area / member count), never a
    # judgement of the place. Null when there is nothing true to say about it yet.
    description: str | None = None
    features: list[CommunityFeatureRow] = Field(default_factory=list)
    activities: list[CommunityActivityRow] = Field(default_factory=list)
    member_preview: list[CommunityMemberPreviewRow] = Field(default_factory=list)
    upcoming_events: list[CommunityEventRow] = Field(default_factory=list)
    # "Create an event" input: POST verbatim to /lana/sessions/{id}/event-venue, then open
    # the setup screen with the venue pinned — no chat turn, no classifier. Null when the
    # place has no google id on file (FE asks for the venue as usual).
    create_event_venue: "EventVenueRequest | None" = None
    actions: list["UiActionRow"] = Field(default_factory=list)


class CommunityMemberRow(BaseModel):
    """A neighbour at the place. Deliberately carries NO stars, band, badge or
    similarity: nothing here compared two people. `attributes` states what is proven
    about THEM — their own public threads, the ones the caller holds too first. Empty
    when nothing true is on file: the old `shared_line` fallback ("You both go to this
    gym") was true of every row and so said nothing."""

    peer_user_id: str
    nickname: str | None = None
    avatar_url: str | None = None
    trait_tags: list[str] = Field(default_factory=list)
    # What this person is to the place: "member" (they go here) or "curious" (§19 —
    # joined to watch it, not counted in member_count). The roster groups on it.
    membership: str = "member"
    # What they do HERE, member-curated (place_activities): the row's second chip kind.
    activities: list[str] = Field(default_factory=list)
    # Their own public threads ("Colombian roots", "Loves to cook"), strongest
    # first. Self-subject only — a child's thread would read as theirs.
    attributes: list[str] = Field(default_factory=list)
    # The caller's own row: no attributes (she is not described back to herself) and
    # no Nudge. Rows rendered now equal member_count (§17).
    me: bool = False
    # "intro_sent" (an intro is already on its way) or "connected" (they already know
    # each other). Either way the row shows a status, never a Nudge — the same rule the
    # peer cards follow, since a second nudge can only hit the 7-day pair cooldown.
    connection: str | None = None
    actions: list["UiActionRow"] = Field(default_factory=list)


class CommunityMembersResponse(BaseModel):
    place_id: str
    place_name: str | None = None
    member_count: int = 0
    # Curious joiners are listed too but never counted as members — the header split
    # ("34 people · 28 go here in real life") is member_count + curious_count.
    curious_count: int = 0
    members: list[CommunityMemberRow] = Field(default_factory=list)
    has_more: bool = False
    # True for an unverified caller: the count is real, the names are withheld.
    requires_phone_verification: bool = False


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
    # Whose thread this is. Owner-facing card only — get_peer_profile never
    # projects the name, so these can't reach another user.
    subject_kind: str | None = None
    subject_name: str | None = None
    subject_age: int | None = None


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
    # Recurring meets: one meet whose starts_at rolls forward, with a standing roster.
    # 'weekly' | 'biweekly' | 'monthly'; None = a one-off. recurrence_until is a plain
    # date ("2026-08-31") — None means the DB's 180-day default from creation.
    recurrence: str | None = None
    recurrence_until: str | None = None
    max_attendees: int | None = None
    # Join settings captured in the host flow.
    auto_approve: bool | None = None  # True = anyone joins; False = host approves each
    allow_attendee_share: bool | None = None
    # Items attendees should bring (the 4/4 quick-setup card) → the meet's pinned list.
    bring_items: list[str] = Field(default_factory=list)
    # AI-picked emoji cover (☕🎨⚽…) — the card's visual when there's no cover image.
    cover_emoji: str | None = None
    # The community this meet is for (setup card 2/5) — the canonical place id of one of
    # the host's own communities. None = a plain neighborhood meet, which is the default.
    circle_place_id: str | None = None
    # Display form of that community — {place_ref, name, emoji, circle_type, detail}, the
    # same shape every event-reading RPC returns. Stamped for the turn (see
    # main._draft_from_dict); the id above stays the stored value.
    community: dict[str, Any] | None = None
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


class RecoStep(BaseModel):
    """One step of the recommendation's question set — a card in the C-4-EVENT-P2B swipe
    carousel, or one turn of the side-Lana chat fork. Same list drives both.

    The set is written per recommendation (Lana generates it; app.reco_question_sets owns the
    floor, the guards and the two closing steps), so `field`/`label`/`question` are data on
    the wire, never an enum the client can switch on. `kind` is the only thing the FE
    branches on:
      text   → free-type input, `placeholder` is the example hint under it
      choice → `options` as tappable chips, still free-typeable
      place  → the Google Places picker (POST /lana/places/search), NOT a text box: a
               park's "where is it?" typed by hand is a string nobody can navigate to
      toggle → the consent step (mock 9/10)
      agree  → "others also said", `options` are "<attr> ×<n>", multi-select
    """

    field: str
    label: str
    question: str
    kind: str = "text"
    placeholder: str | None = None
    options: list[str] = Field(default_factory=list)
    required: bool = False
    answer: str | None = None


class TipDraft(BaseModel):
    """In-chat "share a tip / recommendation" draft (the tip_share flow)."""

    # Stable identity for ONE recommendation, for the whole draft's life. The FE keys its
    # cards-or-chat mode on "is this still the same recommendation?", and it used `name` —
    # which is null until the subject step is answered, so two nameless recs in a row were
    # indistinguishable and the mode leaked from the previous one (dev QA 2026-09-04).
    draft_id: str | None = None
    name: str | None = None
    category: str | None = None
    trait: str | None = None
    locality: str | None = None
    reco_type: str | None = None
    steps: list[RecoStep] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    chips: list[ItemChip] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    ready: bool = False
    listed: bool = False
    signal_id: str | None = None
    missing: list[str] = Field(default_factory=list)
    # Which step of `steps` the chat fork is asking right now — the FE renders that step's
    # `kind` (a `place` step gets the picker). None on the ready card: nothing is open.
    pending_field: str | None = None


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


class ReverseGeocodeRequest(BaseModel):
    """A bare device pin ("Use my current location") to resolve into a name/address
    (issue #42). The coordinates themselves stay authoritative for the event pin."""

    lat: float
    lng: float


class EventVenueRequest(BaseModel):
    """The exact place the host picked from search — stamped onto the session's event
    draft so publish stores the precise pin (not a re-geocoded name)."""

    name: str
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    place_id: str | None = None
    # Set when the host started from a community's screen ("Create an event" there): the
    # meet is FOR that community, not merely held at its address. Pre-selects the setup
    # card's community picker, which the host can still change or clear.
    circle_place_id: str | None = None


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
    # Community card: the place id of the community picked in the dropdown, or None for
    # "None" (just the host's own meet). Members are emailed at publish.
    circle_place_id: str | None = None


class TipSetupRequest(BaseModel):
    """The carousel fork of the recommendation capture (C-4-EVENT-P1B-FORK, "flip through
    cards"): every answer at once instead of one per turn.

    Keys are the `field`s of the steps Lana generated for THIS recommendation, so this
    request cannot be validated against a fixed enum — the endpoint intersects it with the
    session's own step set instead, which is also what stops a client writing arbitrary
    keys into the draft."""

    answers: dict[str, str] = Field(default_factory=dict)


class NudgeHookRequest(BaseModel):
    """FE calls this right after send_nudge / accept_nudge / accept_intro. Only the id
    travels — the worker reads the row to decide who to tell, so a client cannot aim a
    notification.

    Exactly one id is meaningful per call. Both halves live on one endpoint because
    propose_intro writes a nudges row AND an intros row at the same instant: the Chats
    drawer accepts whichever kind the item is, and either has to reach the person waiting
    to hear back.
    """

    nudge_id: str | None = None
    intro_id: str | None = None


class EventJoinHookRequest(BaseModel):
    """FE calls this right after request_to_join_event so the host + joiner get notified."""

    event_id: str


class EventDecisionHookRequest(BaseModel):
    """FE calls this right after decide_event_request so the requester gets the outcome."""

    request_id: str


class EventCancelHookRequest(BaseModel):
    """FE calls this right after cancel_event so the going roster gets push + email."""

    event_id: str


class EventSkipRequest(BaseModel):
    """Host calls off ONE occurrence of a recurring meet ("skip this Friday"). Unlike
    cancel, the worker calls the RPC itself and then notifies — one round trip for the FE."""

    event_id: str


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
    # The community picked in the top filter (a places.id). Everything this session
    # searches, shows and creates is scoped to it; "" or absent = the ZIP default.
    # See app/community_scope.py — membership is re-checked server-side.
    community_id: str | None = None


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
    # The session's saved/effective default language (users.locale, or the
    # device-locale seed when nothing is saved) — the FE mirrors its UI locale
    # to it when the code is one it supports (en/es/pt). None = no signal yet.
    preferred_language: str | None = None
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
    # Same look-screen card as on SendMessageResponse — absent unless the opening
    # turn itself was the looking-open one.
    communities: CommunitiesCardPayload | None = None
    community_discovery: CommunityDiscoveryResponse | None = None
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
    # The threads the user weighted in "What matters most" (intent_hint="look_tip_rerank").
    # Labels as shown — they are matched against each row's own tags and rec text, so they
    # only ever re-ORDER the ranking; an unmatched weight changes nothing.
    weights: list[str] = Field(default_factory=list, max_length=8)
    # A "By the way…" tile answer (intent_hint="rapport_answer"): the gap being answered and
    # the tile's question, so the worker closes the gap and gives the profile engine context.
    rapport_gap_row_id: str | None = None
    rapport_question: str | None = None
    # WHO a tapped Nudge means, when the card already knows. The button posts a normal
    # message ("introduce me to Rust") so the turn lives in the transcript, but the name
    # alone only resolves against the last find-peers run — a community roster or any
    # other surface answered "I don't see Rust in your neighbor matches" and sent nothing
    # (2026-08-18). The id is authoritative; the text still carries the conversation.
    peer_user_id: str | None = None
    # The community picked in the top filter (a places.id) — see CreateSessionRequest.
    # None means "the client said nothing", which keeps whatever the session already
    # has; "" is the explicit "no community" pick and restores the ZIP default.
    community_id: str | None = None
    # WHICH place a grounding card's pick is, when the user chose it from the card's
    # own Google search. Those places are in no cached candidate list, so matching the
    # posted text ("It's Fitness CF St. Cloud") would only re-search for them.
    ground_place_id: str | None = None


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
    # The look screen's "YOUR COMMUNITIES" card. Present only on the looking-open turn
    # (and only when the user has at least one community) — absent everywhere else.
    communities: CommunitiesCardPayload | None = None
    # Nearby communities the user could join, on a turn where they asked about
    # communities in chat ("show me communities around me"). Same rows as
    # /lana/circles/discover, already filtered to ones they're not in.
    community_discovery: CommunityDiscoveryResponse | None = None
    # Set once an event publishes — the FE builds a shareable /meet/{id} link from it.
    event_id: str | None = None
    item_draft: ItemDraft | None = None
    tip_draft: TipDraft | None = None
    look_draft: LookDraft | None = None
    # Seek-side ask card on a looking.tip turn (§12d). Absent on every other turn, so the
    # FE renders nothing until it arrives.
    ask_draft: AskDraftPayload | None = None
    # The "which spot is it?" card, when this turn asked a place-grounding question.
    # Absent on every other turn.
    grounding: GroundingCardPayload | None = None
    routing: TurnRouting | None = None
    # See CreateSessionResponse.preferred_language — echoed every turn so the FE
    # can follow a mid-chat language switch (auto-persisted after 2 diverging turns).
    preferred_language: str | None = None
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
    # Short user-visible sub-facts accumulated across turns for the same thread
    # ("Swims every weekend"). Merged append-dedup on upsert, capped at 5.
    details: list[str] = Field(default_factory=list)
    # Who the claim is about. "self" for the speaker; "child" when they said it
    # about their kid ("my 7-year-old does karate"). The name is OWNER-ONLY —
    # it is never written into label/source_quote/synonyms (those stay redacted)
    # and no peer-facing surface reads it.
    subject_kind: str = "self"
    subject_name: str | None = None
    # Stored as a birth year, not an age: an age written today is wrong in a year.
    subject_birth_year: int | None = None
    # True when they said they do this AT the community they are looking at ("I like
    # the pool HERE"). Not stored on the claim — a claim holds one place_ref and the
    # same activity happens at two communities — it writes the place↔activity edge
    # (place_activities, 20261010120000). Only ever set on a turn that HAS a here-place.
    at_here: bool = False


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
