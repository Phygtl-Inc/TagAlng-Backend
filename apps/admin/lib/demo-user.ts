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

/** Supabase test phone — add in Dashboard → Auth → Phone → Test numbers. */
export const DEMO_TEST_PHONE = '+15550999012';
export const DEMO_TEST_OTP = '000000';

/** Maria demo block (Lake Nona) — same as Postman assign_home_block. */
const MARIA_BLOCK_LAT = 28.3647;
const MARIA_BLOCK_LNG = -81.2568;

/** Meet Lana — anonymous guest (no phone until joint-moment intro). */
export async function guestAnonymousSignIn() {
  const supabase = getDemoSupabase();
  const { data, error } = await supabase.auth.signInAnonymously();
  if (error) throw error;
  if (!data.session?.access_token) throw new Error('No session after anonymous sign-in');
  return data.session;
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
  const supabase = getDemoSupabase();
  await refreshDemoSession();
  const { error } = await supabase.auth.updateUser({ phone });
  if (error) throw error;
}

export async function guestVerifyPhoneLink(phone: string, otp: string) {
  const supabase = getDemoSupabase();
  await refreshDemoSession();
  const { data, error } = await supabase.auth.verifyOtp({
    phone,
    token: otp,
    type: 'phone_change',
  });
  if (error) throw error;
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
