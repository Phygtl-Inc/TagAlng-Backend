import type { LanaUi } from './buckets';

const BASE =
  process.env.NEXT_PUBLIC_LANA_WORKER_URL ||
  'https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app';

export type TurnRouting = {
  outcome?: string;
  intent_class?: string;
  confidence?: number;
  tool_called?: string | null;
  capture_fired?: boolean;
};

export type JointMomentPayload = {
  joint_moment_id?: string | null;
  status?: string | null;
  candidate?: {
    user_id?: string | null;
    nickname?: string | null;
    avatar_url?: string | null;
  } | null;
  lana_copy?: string | null;
  match_reason?: string | null;
  is_demo?: boolean;
};

export type IntroProposalPayload = {
  intro_id?: string | null;
  nudge_id?: string | null;
  candidate_user_id?: string | null;
  candidate_nickname?: string | null;
  matching_peer_label?: string | null;
  match_reason?: string | null;
  shared_dimensions?: string[];
  status?: string | null;
};

export type PendingIntroRow = {
  intro_id?: string | null;
  other_user_id?: string | null;
  nickname?: string | null;
  avatar_url?: string | null;
  created_at?: string | null;
  expires_at?: string | null;
  status?: string | null;
  match_reason?: string | null;
  shared_dimensions?: string[];
  direction?: 'sent' | 'received' | null;
};

export type AuthActionPayload = {
  type:
    | 'link_phone_signup'
    | 'verify_signup_otp'
    | 'send_login_otp'
    | 'verify_login_otp'
    | 'logout';
  phone?: string | null;
  token?: string | null;
  verify_type?: string | null;
};

export type PeerMatchRow = {
  peer_user_id?: string | null;
  nickname?: string | null;
  avatar_url?: string | null;
  similarity_score?: number | null;
  matching_peer_label?: string | null;
  matching_peer_concept?: string | null;
  has_exact_concept_match?: boolean;
  preview?: boolean;
};

export type ActivityPreviewRow = {
  title: string;
  starts_at?: string | null;
  starts_label?: string | null;
  venue_name?: string | null;
  preview?: boolean;
};

/** What input chrome to show — mirrors lana-worker `ui_intent`. */
export type LanaUiIntent =
  | 'chat'
  | 'collect_zip'
  | 'collect_identity'
  | 'collect_display_name'
  | 'collect_phone'
  | 'collect_otp'
  | 'show_peer_preview'
  | 'show_activity_preview'
  | 'confirm_profile'
  | 'upload_profile_photo'
  | 'sign_out'
  | 'offer_neighbor_intro'
  | 'propose_neighbor_intro'
  | 'show_pending_intros'
  | 'show_block_log'
  | 'signal_saved';

export type BlockLogEntryRow = {
  entry_id?: string | null;
  match_type?: string | null;
  peer_user_id?: string | null;
  peer_preview_label?: string | null;
  match_strength?: number | null;
  match_reasons?: string[];
  created_at?: string | null;
  expires_at?: string | null;
  notification_sent_to_peer?: boolean;
  block_id?: string | null;
  block_name?: string | null;
};

export type SignalSavedPayload = {
  signal_id?: string | null;
  intent?: string | null;
  category?: string | null;
  detail_text?: string | null;
  block_id?: string | null;
  matches_created?: number | null;
};

export type GuestOnboardingFields = {
  onboarding_step?: string | null;
  requires_phone_verification?: boolean;
  joint_moment?: JointMomentPayload | null;
  intro_proposal?: IntroProposalPayload | null;
  pending_intros?: PendingIntroRow[];
  block_log_entries?: BlockLogEntryRow[];
  signal_saved?: SignalSavedPayload | null;
  phone_verified?: boolean;
  home_block_assigned?: boolean;
  is_anonymous?: boolean;
  active_intent?: string | null;
  routing_phase?: string | null;
  ui_intent?: LanaUiIntent | null;
  peer_matches?: PeerMatchRow[];
  activity_previews?: ActivityPreviewRow[];
  auth_action?: AuthActionPayload | null;
  auth_intent?: string | null;
  login_phone?: string | null;
  requires_login_otp?: boolean;
  login_otp_token?: string | null;
};

export type LanaSession = GuestOnboardingFields & {
  session_id: string;
  purpose: string;
  status: string;
  assistant_message: string;
  ready_to_complete: boolean;
  ui?: LanaUi | null;
  orchestrator?: boolean;
};

export type LanaTurn = GuestOnboardingFields & {
  session_id: string;
  status: string;
  assistant_message: string;
  ready_to_complete: boolean;
  message_count: number;
  ui?: LanaUi | null;
  routing?: TurnRouting | null;
  orchestrator?: boolean;
};

export type ExtractedClaim = {
  concept: string;
  label: string;
  confidence: number;
  source_quote?: string | null;
  bucket?: string | null;
  synonyms?: string[];
};

export type CompleteResult = {
  session_id: string;
  status: string;
  assistant_message: string;
  mapped_summary?: string | null;
  claims: ExtractedClaim[];
  spans?: { text: string; bucket?: string }[];
};

async function lanaFetch<T>(
  path: string,
  token: string,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(`${BASE.replace(/\/$/, '')}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail =
      typeof body.detail === 'string'
        ? body.detail
        : body.message || res.statusText;
    throw new Error(detail || `Lana API ${res.status}`);
  }
  return body as T;
}

/** Unified Lana chat (default) — empty body, purpose `lana`. Resumes active session unless `forceNew`. */
export function startUnifiedSession(token: string, options?: { forceNew?: boolean }) {
  return lanaFetch<LanaSession>('/lana/sessions', token, {
    method: 'POST',
    body: JSON.stringify(options?.forceNew ? { force_new: true } : {}),
  });
}

/** Legacy guest onboarding — profile_intake. */
export function startProfileSession(token: string) {
  return lanaFetch<LanaSession>('/lana/sessions', token, {
    method: 'POST',
    body: JSON.stringify({ purpose: 'profile_intake' }),
  });
}

export type ProfilePhotoUploadResult = {
  profile_photo_url: string;
};

export async function uploadProfilePhoto(token: string, file: File) {
  const res = await fetch(`${BASE}/lana/profile-photo`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: (() => {
      const form = new FormData();
      form.append('file', file);
      return form;
    })(),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail =
      typeof body.detail === 'string'
        ? body.detail
        : body.message || res.statusText;
    throw new Error(detail || `Profile photo upload ${res.status}`);
  }
  return body as ProfilePhotoUploadResult;
}

export function sendMessage(token: string, sessionId: string, message: string) {
  return lanaFetch<LanaTurn>(`/lana/sessions/${sessionId}/messages`, token, {
    method: 'POST',
    body: JSON.stringify({ message }),
  });
}

export function completeSession(token: string, sessionId: string, force = false) {
  return lanaFetch<CompleteResult>(`/lana/sessions/${sessionId}/complete`, token, {
    method: 'POST',
    body: JSON.stringify({ force }),
  });
}
