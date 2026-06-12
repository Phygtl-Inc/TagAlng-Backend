import { createClient } from '@supabase/supabase-js';

const url = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

/** Demo neighbor session — separate storage from admin email login. */
export function getDemoSupabase() {
  if (!url || !anon) {
    throw new Error('Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY');
  }
  return createClient(url, anon, {
    auth: { storageKey: 'tagalng-lana-demo', persistSession: true },
  });
}

export type PeerMatch = {
  peer_user_id: string;
  nickname: string | null;
  avatar_url: string | null;
  similarity_score: number;
  matching_peer_label: string | null;
  matching_peer_concept: string | null;
  has_exact_concept_match: boolean;
};

export type ClusterEvent = {
  id: string;
  host_id: string;
  title: string;
  description: string | null;
  starts_at: string;
  ends_at: string | null;
  venue_name: string | null;
  cohort_tags: string[] | null;
  max_attendees: number | null;
  status: string;
};

/**
 * Dev signup test phone — use a fresh number each run; add in Dashboard as 15550999012=000000.
 * +15550000000 is reserved for returning-user login tests (existing account).
 */
export const DEMO_TEST_PHONE = '+15550999012';
export const DEMO_TEST_OTP = '000000';

type SupabaseAuthResponse = {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  token_type?: string;
  user?: { id?: string; is_anonymous?: boolean };
  error?: string;
  error_description?: string;
  msg?: string;
};

/** E.164 for Supabase Auth (test numbers in Dashboard are digits-only, e.g. 9233079925193=000000). */
export function normalizeE164Phone(raw: string): string {
  const trimmed = raw.trim();
  const digits = trimmed.replace(/\D/g, '');
  if (!digits) return trimmed;
  if (trimmed.startsWith('+')) return `+${digits}`;
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith('1')) return `+${digits}`;
  return `+${digits}`;
}

function authBaseUrl() {
  return url.replace(/\/$/, '');
}

function authErrorDetail(data: SupabaseAuthResponse, res: Response): string {
  return data.error_description || data.msg || data.error || res.statusText;
}

function authPhoneErrorHint(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  if (msg.includes('Twilio') || msg.includes('572002')) {
    return (
      `${msg} — Add test number in Dashboard → Auth → Phone (digits only, no +): ` +
      `15550999012=000000 or 15550000000=000000. Then start a fresh Meet Lana session.`
    );
  }
  return msg;
}

/**
 * Postman step 11 — link phone to anonymous user (same user_id as Lana session).
 * PUT /auth/v1/user with bearer = guest access_token (NOT anon key).
 */
export async function linkPhoneSignup(accessToken: string, phone: string): Promise<void> {
  const phoneE164 = normalizeE164Phone(phone);
  const res = await fetch(`${authBaseUrl()}/auth/v1/user`, {
    method: 'PUT',
    headers: {
      apikey: anon,
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ phone: phoneE164 }),
  });
  const data = (await res.json().catch(() => ({}))) as SupabaseAuthResponse;
  if (!res.ok) {
    throw new Error(authPhoneErrorHint(new Error(authErrorDetail(data, res))));
  }
}

/**
 * Postman B1 — send login OTP without touching the anonymous Lana session in storage.
 */
export async function sendLoginOtp(phone: string): Promise<void> {
  const phoneE164 = normalizeE164Phone(phone);
  const res = await fetch(`${authBaseUrl()}/auth/v1/otp`, {
    method: 'POST',
    headers: {
      apikey: anon,
      Authorization: `Bearer ${anon}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ phone: phoneE164, create_user: false }),
  });
  const data = (await res.json().catch(() => ({}))) as SupabaseAuthResponse;
  if (!res.ok) {
    throw new Error(authPhoneErrorHint(new Error(authErrorDetail(data, res))));
  }
}

/**
 * Postman POST /auth/v1/verify — same headers/body as E2E collections.
 * Signup: type phone_change (step 13). Login: type sms (Guest-InChat-Login step 7).
 */
async function supabaseVerifyOtp(
  phone: string,
  otp: string,
  type: 'sms' | 'phone_change',
): Promise<string> {
  const phoneE164 = normalizeE164Phone(phone);
  const res = await fetch(`${authBaseUrl()}/auth/v1/verify`, {
    method: 'POST',
    headers: {
      apikey: anon,
      Authorization: `Bearer ${anon}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      phone: phoneE164,
      token: otp.trim(),
      type,
    }),
  });
  const data = (await res.json().catch(() => ({}))) as SupabaseAuthResponse;
  if (!res.ok) {
    throw new Error(authPhoneErrorHint(new Error(authErrorDetail(data, res))));
  }
  if (!data.access_token || !data.refresh_token) {
    throw new Error(`Verify (${type}) returned no session`);
  }
  const supabase = getDemoSupabase();
  const { error } = await supabase.auth.setSession({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
  });
  if (error) throw error;
  return data.access_token;
}

/** Guest-InChat-Login step 7 — login verify, type sms. */
export async function verifyLoginOtp(phone: string, otp: string): Promise<string> {
  return supabaseVerifyOtp(phone, otp, 'sms');
}

export type DemoAuthProfile = {
  userId: string;
  phone: string | null;
  nickname: string | null;
  homeBlockId: string | null;
  isAnonymous: boolean;
};

export async function fetchAuthProfile(): Promise<DemoAuthProfile | null> {
  const supabase = getDemoSupabase();
  const { data: authData } = await supabase.auth.getSession();
  const user = authData.session?.user;
  if (!user) return null;
  const { data: row } = await supabase
    .from('users')
    .select('nickname, home_block_id')
    .eq('id', user.id)
    .maybeSingle();
  return {
    userId: user.id,
    phone: user.phone ?? null,
    nickname: (row?.nickname as string | null) ?? null,
    homeBlockId: (row?.home_block_id as string | null) ?? null,
    isAnonymous: Boolean(user.is_anonymous),
  };
}

export async function demoSignOut() {
  const supabase = getDemoSupabase();
  await supabase.auth.signOut();
}

/** Lana-Unified-Full-E2E step 13 — signup verify, type phone_change (same user_id as Lana). */
export async function verifyPhoneChangeSignup(phone: string, otp: string): Promise<string> {
  return supabaseVerifyOtp(phone, otp, 'phone_change');
}

/** Maria demo block (Lake Nona) — same as Postman assign_home_block. */
const MARIA_BLOCK_LAT = 28.3647;
const MARIA_BLOCK_LNG = -81.2568;

/**
 * Anonymous guest — same as Postman step 1:
 * POST /auth/v1/signup {} with anon key → access_token (is_anonymous: true).
 */
export async function guestAnonymousSignUp() {
  if (!url?.trim()) {
    throw new Error('Set NEXT_PUBLIC_SUPABASE_URL in apps/admin/.env.local');
  }
  if (!anon?.trim()) {
    throw new Error(
      'Set NEXT_PUBLIC_SUPABASE_ANON_KEY in apps/admin/.env.local (Supabase → API → anon public)',
    );
  }

  const res = await fetch(`${url.replace(/\/$/, '')}/auth/v1/signup`, {
    method: 'POST',
    headers: {
      apikey: anon,
      Authorization: `Bearer ${anon}`,
      'Content-Type': 'application/json',
    },
    body: '{}',
  });

  const data = (await res.json().catch(() => ({}))) as SupabaseAuthResponse;
  if (!res.ok) {
    const detail = data.error_description || data.msg || data.error || res.statusText;
    throw new Error(`Anonymous signup failed (${res.status}): ${detail}`);
  }
  if (!data.access_token || !data.refresh_token) {
    throw new Error('Anonymous signup returned no access_token — enable Anonymous sign-ins in Supabase');
  }

  const supabase = getDemoSupabase();
  const { error: sessionErr } = await supabase.auth.setSession({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
  });
  if (sessionErr) throw sessionErr;

  const { data: sessionData } = await supabase.auth.getSession();
  if (!sessionData.session?.access_token) {
    throw new Error('No session after anonymous signup');
  }
  return sessionData.session;
}

/** @deprecated Use guestAnonymousSignUp — kept as alias for callers. */
export async function guestAnonymousSignIn() {
  return guestAnonymousSignUp();
}

/**
 * Link phone to the current anonymous user (same user_id as Lana session).
 * OTP is sent automatically. Verify with guestVerifyPhoneLink (type phone_change).
 */
/** Refresh JWT before phone link — anonymous sessions expire after ~1h idle. */
export async function refreshDemoSession() {
  const supabase = getDemoSupabase();
  const { data: current } = await supabase.auth.getSession();
  if (!current.session) throw new Error('Session expired — tap Meet Lana to start over');
  const { data, error } = await supabase.auth.refreshSession();
  if (error) throw error;
  if (!data.session?.access_token) {
    throw new Error('Session expired — tap Meet Lana to start over');
  }
  return data.session;
}

export async function guestLinkPhone(phone: string) {
  const session = await refreshDemoSession();
  await linkPhoneSignup(session.access_token, phone);
}

export type LanaAuthAction = {
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

/**
 * Execute auth_action from Lana unified chat (FE responsibility per backend contract).
 * Returns fresh access_token (same user_id on signup path).
 */
/** Map API turn fields → auth_action (in-chat login/signup handoff). */
export function authActionFromTurn(turn: {
  auth_action?: LanaAuthAction | null;
  login_phone?: string | null;
  requires_login_otp?: boolean;
  login_otp_token?: string | null;
}): LanaAuthAction | null {
  if (turn.auth_action?.type) return turn.auth_action;
  if (turn.login_otp_token && turn.login_phone) {
    return {
      type: 'verify_login_otp',
      phone: turn.login_phone,
      token: turn.login_otp_token,
      verify_type: 'sms',
    };
  }
  if (turn.requires_login_otp && turn.login_phone) {
    return {
      type: 'send_login_otp',
      phone: turn.login_phone,
      verify_type: 'sms',
    };
  }
  return null;
}

/**
 * Execute Lana `auth_action` — mirrors Postman exactly.
 *
 * Signup (discovery): 11 PUT /user → 13 POST /verify phone_change → same Lana session_id
 * Login (in-chat):     5 POST /otp → 7 POST /verify sms → new user_id, new Lana session
 */
export async function handleLanaAuthAction(action: LanaAuthAction): Promise<string> {
  if (action.type === 'link_phone_signup') {
    const phone = action.phone ? normalizeE164Phone(action.phone) : null;
    if (!phone) throw new Error('auth_action missing phone');
    const session = await refreshDemoSession();
    await linkPhoneSignup(session.access_token, phone);
    return session.access_token;
  }

  if (action.type === 'verify_signup_otp') {
    const phone = action.phone ? normalizeE164Phone(action.phone) : null;
    const token = action.token;
    if (!phone || !token) throw new Error('auth_action missing phone or OTP');
    return verifyPhoneChangeSignup(phone, token);
  }

  if (action.type === 'send_login_otp') {
    const phone = action.phone;
    if (!phone) throw new Error('auth_action missing phone');
    await sendLoginOtp(phone);
    const session = await refreshDemoSession();
    return session.access_token;
  }

  if (action.type === 'verify_login_otp') {
    const phone = action.phone;
    const token = action.token;
    if (!phone || !token) throw new Error('auth_action missing phone or OTP');
    return verifyLoginOtp(phone, token);
  }

  if (action.type === 'logout') {
    await demoSignOut();
    const session = await guestAnonymousSignUp();
    return session.access_token;
  }

  throw new Error(`Unknown auth_action type: ${action.type}`);
}

export async function guestVerifyPhoneLink(phone: string, otp: string) {
  await refreshDemoSession();
  await verifyPhoneChangeSignup(phone, otp);
  const supabase = getDemoSupabase();
  const { data } = await supabase.auth.getSession();
  if (!data.session?.access_token) throw new Error('No session after phone verify');
  return data.session;
}

export async function assignMariaDemoBlock() {
  const supabase = getDemoSupabase();
  const { error } = await supabase.rpc('assign_home_block', {
    p_lat: MARIA_BLOCK_LAT,
    p_lng: MARIA_BLOCK_LNG,
  });
  if (error) throw error;
}

export async function sendJointMomentIntro(jointMomentId: string, opener?: string) {
  const supabase = getDemoSupabase();
  const { data, error } = await supabase.rpc('send_joint_moment_intro', {
    p_joint_moment_id: jointMomentId,
    p_opener_text: opener ?? 'Hi — Lana thought we should meet!',
  });
  if (error) throw error;
  return data as { status?: string; nudge_id?: string };
}

/** Same order as Postman B1 → B2: /otp then /verify. Test phone skips Twilio. */
export async function demoPhoneAuth(
  phone: string = DEMO_TEST_PHONE,
  otp: string = DEMO_TEST_OTP,
) {
  const supabase = getDemoSupabase();

  const { error: otpErr } = await supabase.auth.signInWithOtp({
    phone,
    options: { shouldCreateUser: true },
  });
  if (otpErr) throw otpErr;

  const { data, error: verifyErr } = await supabase.auth.verifyOtp({
    phone,
    token: otp,
    type: 'sms',
  });
  if (verifyErr) throw verifyErr;
  if (!data.session) throw new Error('No session after OTP verify');
  return data.session;
}

/** Silent dev bootstrap: session + home block. Used when user taps Meet Lana. */
export async function ensureDemoReady(
  phone: string = DEMO_TEST_PHONE,
  otp: string = DEMO_TEST_OTP,
): Promise<string> {
  const supabase = getDemoSupabase();
  let {
    data: { session },
  } = await supabase.auth.getSession();

  if (!session?.access_token) {
    await demoPhoneAuth(phone, otp);
    const next = await supabase.auth.getSession();
    session = next.data.session;
  }
  if (!session?.access_token) throw new Error('Could not sign in demo neighbor');

  const { data: userRow } = await supabase
    .from('users')
    .select('home_block_id')
    .eq('id', session.user.id)
    .maybeSingle();

  if (!userRow?.home_block_id) {
    await ensureDemoBlock();
  }

  return session.access_token;
}

export async function ensureDemoBlock(zip = '32827') {
  const supabase = getDemoSupabase();
  const { data: blocks, error: blocksErr } = await supabase.rpc('get_blocks_near_zip', {
    p_zip: zip,
    p_cluster_id: 'lake-nona',
    p_limit: 5,
  });
  if (blocksErr) throw blocksErr;
  const block = (blocks as { block_id: string }[])?.[0];
  if (!block?.block_id) throw new Error('No blocks found for ZIP ' + zip);

  const { error } = await supabase.rpc('assign_home_block', {
    p_block_id: block.block_id,
    p_home_zip: zip,
  });
  if (error) throw error;
  return block.block_id;
}

export async function loadPeerMatches(limit = 8): Promise<PeerMatch[]> {
  const supabase = getDemoSupabase();
  const { data, error } = await supabase.rpc('match_peers_by_claim_vectors', {
    p_limit: limit,
    p_min_similarity: 0.45,
  });
  if (error) throw error;
  return (data as PeerMatch[]) || [];
}

export async function loadClusterEvents(): Promise<ClusterEvent[]> {
  const supabase = getDemoSupabase();
  const { data, error } = await supabase.rpc('get_cluster_events', {
    p_cluster_id: 'lake-nona',
    p_window: '14 days',
    p_locale: 'en',
  });
  if (error) throw error;
  return (data as ClusterEvent[]) || [];
}
