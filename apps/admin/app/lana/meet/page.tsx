'use client';

import { LanaRichText } from '@/components/LanaRichText';
import { LanaSheep } from '@/components/LanaSheep';
import { ClaimCards } from '@/components/ClaimCards';
import { MappedSummary } from '@/components/MappedSummary';
import {
  completeSession,
  sendMessage,
  startUnifiedSession,
  type CompleteResult,
  type JointMomentPayload,
  type LanaTurn,
  type LanaUiIntent,
  type ActivityPreviewRow,
  type PeerMatchRow,
} from '@/lib/lana-client';
import {
  DEMO_TEST_OTP,
  DEMO_TEST_PHONE,
  assignMariaDemoBlock,
  authActionFromTurn,
  demoPhoneAuth,
  demoSignOut,
  ensureDemoBlock,
  fetchAuthProfile,
  guestAnonymousSignUp,
  guestLinkPhone,
  guestVerifyPhoneLink,
  getDemoSupabase,
  handleLanaAuthAction,
  loadClusterEvents,
  sendJointMomentIntro,
  type ClusterEvent,
  type DemoAuthProfile,
} from '@/lib/demo-user';
import Link from 'next/link';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';

type Screen = 'welcome' | 'chat' | 'phone' | 'discover';

type ChatLine = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
};

function fallbackUiIntent(
  phase: string,
  readyToComplete?: boolean,
): LanaUiIntent {
  if (readyToComplete) return 'confirm_profile';
  const map: Record<string, LanaUiIntent> = {
    need_zip: 'collect_zip',
    need_identity: 'collect_identity',
    need_display_name: 'collect_display_name',
    await_signup_phone: 'collect_phone',
    await_signup_otp: 'collect_otp',
    await_login_phone: 'collect_phone',
    await_login_otp: 'collect_otp',
    gate_verify: 'collect_phone',
    preview: 'show_peer_preview',
  };
  return map[phase] ?? 'chat';
}

function toE164(raw: string) {
  const digits = raw.replace(/\D/g, '');
  if (digits.startsWith('1') && digits.length === 11) return `+${digits}`;
  if (digits.length === 10) return `+1${digits}`;
  if (raw.startsWith('+')) return raw;
  return `+${digits}`;
}

export default function MeetLanaPage() {
  const [screen, setScreen] = useState<Screen>('welcome');
  const [token, setToken] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [draft, setDraft] = useState('');
  const [readyToComplete, setReadyToComplete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devOpen, setDevOpen] = useState(false);
  const [setupPhone, setSetupPhone] = useState(DEMO_TEST_PHONE);
  const [setupOtp, setSetupOtp] = useState(DEMO_TEST_OTP);
  const [completeResult, setCompleteResult] = useState<CompleteResult | null>(null);
  const [events, setEvents] = useState<ClusterEvent[]>([]);
  const [thinking, setThinking] = useState(false);
  const [onboardingStep, setOnboardingStep] = useState('early_chat');
  const [jointMoment, setJointMoment] = useState<JointMomentPayload | null>(null);
  const [phoneVerified, setPhoneVerified] = useState(false);
  const [phoneDraft, setPhoneDraft] = useState('15550999012');
  const [otpDraft, setOtpDraft] = useState(DEMO_TEST_OTP);
  const [otpSent, setOtpSent] = useState(false);
  const [introSent, setIntroSent] = useState(false);
  const [routingPhase, setRoutingPhase] = useState<string | null>('listening');
  const [uiIntent, setUiIntent] = useState<LanaUiIntent>('chat');
  const [loginPhoneHint, setLoginPhoneHint] = useState<string | null>(null);
  const [peerMatches, setPeerMatches] = useState<PeerMatchRow[]>([]);
  const [activityPreviews, setActivityPreviews] = useState<ActivityPreviewRow[]>([]);
  const [lastOrchestrator, setLastOrchestrator] = useState<boolean | null>(null);
  const [signedInUser, setSignedInUser] = useState<DemoAuthProfile | null>(null);
  const [accountOpen, setAccountOpen] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const refreshAccount = useCallback(async () => {
    try {
      const profile = await fetchAuthProfile();
      setSignedInUser(profile);
      if (profile && !profile.isAnonymous) {
        setPhoneVerified(true);
      }
    } catch {
      setSignedInUser(null);
    }
  }, []);

  useEffect(() => {
    void refreshAccount();
  }, [refreshAccount]);

  const scrollChat = useCallback(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollChat();
  }, [lines, screen, thinking, scrollChat]);

  function applyTurn(turn: LanaTurn, opts?: { forceVerified?: boolean }) {
    setReadyToComplete(turn.ready_to_complete);
    if (turn.joint_moment) setJointMoment(turn.joint_moment);
    if (turn.routing_phase) setRoutingPhase(turn.routing_phase);
    if (turn.ui_intent) {
      setUiIntent(turn.ui_intent);
    } else if (turn.routing_phase) {
      setUiIntent(fallbackUiIntent(turn.routing_phase, turn.ready_to_complete));
    }
    if (turn.login_phone) setLoginPhoneHint(turn.login_phone);
    setPeerMatches(turn.peer_matches ?? []);
    setActivityPreviews(turn.activity_previews ?? []);
    if (typeof turn.orchestrator === 'boolean') setLastOrchestrator(turn.orchestrator);

    const verified = Boolean(opts?.forceVerified || turn.phone_verified);
    if (verified) setPhoneVerified(true);

    // Unified lana: stay in chat; phone/OTP typed in thread (auth_action handled in pushTurn)
    if (turn.routing_phase != null || turn.active_intent != null || !turn.onboarding_step) {
      setScreen('chat');
      if (turn.onboarding_step) setOnboardingStep(turn.onboarding_step);
      return;
    }

    // Legacy profile_intake fallback
    if (verified || turn.onboarding_step === 'post_verify') {
      setOnboardingStep('post_verify');
      setScreen('chat');
      return;
    }
    if (turn.onboarding_step) setOnboardingStep(turn.onboarding_step);
    if (turn.onboarding_step === 'await_phone' || turn.requires_phone_verification) {
      setOtpSent(false);
      setScreen('phone');
    } else {
      setScreen('chat');
    }
  }

  async function completeSignedInSession(access: string, mode: 'login' | 'signup') {
    const profile = await fetchAuthProfile();
    setSignedInUser(profile);
    setPhoneVerified(true);
    setOnboardingStep('post_verify');
    setRoutingPhase('listening');
    setUiIntent('chat');
    setToken(access);

    const label = profile?.nickname || profile?.phone || 'your account';
    const blockHint = profile?.homeBlockId
      ? 'Your block is on file — ask me to find neighbors or activities.'
      : 'Tell me your ZIP when you want discovery, or keep chatting.';

    if (mode === 'login') {
      // Postman Guest-InChat-Login: new user_id → abandon guest Lana session, start fresh
      const lana = await startUnifiedSession(access);
      setSessionId(lana.session_id);
      setLines((prev) => [
        ...prev,
        {
          id: `a-signed-in-${Date.now()}`,
          role: 'assistant',
          content: `You're signed in as **${label}**. ${blockHint}`,
        },
      ]);
      return lana;
    }

    // Postman E2E step 14+: same lana_session_id, new access_token after phone_change
    setLines((prev) => [
      ...prev,
      {
        id: `a-verified-${Date.now()}`,
        role: 'assistant',
        content: `Phone verified — you're set up as **${label}**. ${blockHint}`,
      },
    ]);
    return null;
  }

  async function pushTurn(access: string, sid: string, text: string) {
    const userLine: ChatLine = { id: `u-${Date.now()}`, role: 'user', content: text };
    setLines((prev) => [...prev, userLine]);
    let token = access;
    let turn = await sendMessage(token, sid, text);

    setLines((prev) => [
      ...prev,
      { id: `a-${Date.now()}`, role: 'assistant', content: turn.assistant_message },
    ]);
    applyTurn(turn);

    const authAction = authActionFromTurn(turn);
    if (authAction) {
      try {
        token = await handleLanaAuthAction(authAction);
        setToken(token);
        if (authAction.type === 'verify_signup_otp') {
          await completeSignedInSession(token, 'signup');
          // Postman E2E step 14 — same session_id, new bearer after phone_change
          turn = await sendMessage(token, sid, 'ok');
          setLines((prev) => [
            ...prev,
            { id: `a-verify-${Date.now()}`, role: 'assistant', content: turn.assistant_message },
          ]);
          applyTurn(turn, { forceVerified: true });
        } else if (authAction.type === 'verify_login_otp') {
          await completeSignedInSession(token, 'login');
          return turn;
        }
      } catch (authErr) {
        const msg = authErr instanceof Error ? authErr.message : 'Auth failed';
        throw new Error(msg);
      }
    }

    return turn;
  }

  async function onMeetLana() {
    setBusy(true);
    setError(null);
    try {
      const session = await guestAnonymousSignUp();
      setToken(session.access_token);
      const lana = await startUnifiedSession(session.access_token);
      setSessionId(lana.session_id);
      setLines([{ id: 'open', role: 'assistant', content: lana.assistant_message }]);
      setRoutingPhase(lana.routing_phase ?? 'listening');
      setUiIntent(
        lana.ui_intent ??
          fallbackUiIntent(lana.routing_phase ?? 'listening', lana.ready_to_complete),
      );
      setOnboardingStep(lana.onboarding_step || 'early_chat');
      setJointMoment(lana.joint_moment ?? null);
      setReadyToComplete(lana.ready_to_complete);
      setPeerMatches(lana.peer_matches ?? []);
      setActivityPreviews(lana.activity_previews ?? []);
      setLastOrchestrator(lana.orchestrator ?? false);
      setScreen('chat');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start — check env keys');
      setDevOpen(true);
    } finally {
      setBusy(false);
    }
  }

  async function runDevSetup(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await demoPhoneAuth(setupPhone, setupOtp);
      await ensureDemoBlock();
      const supabase = getDemoSupabase();
      const { data } = await supabase.auth.getSession();
      if (!data.session?.access_token) throw new Error('No session after sign-in');
      setToken(data.session.access_token);
      setPhoneVerified(true);
      setDevOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Setup failed');
    } finally {
      setBusy(false);
    }
  }

  async function onSend(e?: FormEvent) {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || !token || !sessionId || busy) return;
    setDraft('');
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      await pushTurn(token, sessionId, text);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Send failed');
    } finally {
      setBusy(false);
      setThinking(false);
    }
  }

  async function onJointChoice(accept: boolean) {
    if (!token || !sessionId || busy) return;
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      await pushTurn(token, sessionId, accept ? 'Yes' : 'Not now');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Send failed');
    } finally {
      setBusy(false);
      setThinking(false);
    }
  }

  async function onSendPhoneCode(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const phone = toE164(phoneDraft);
      await guestLinkPhone(phone);
      setOtpSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send code');
    } finally {
      setBusy(false);
    }
  }

  async function onVerifyPhone(e?: FormEvent) {
    e?.preventDefault();
    if (!token || !sessionId) return;
    if (!otpSent) {
      setError('Tap Send code first, then enter the OTP.');
      return;
    }
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      const phone = toE164(phoneDraft);
      const session = await guestVerifyPhoneLink(phone, otpDraft.trim());
      setToken(session.access_token);
      setPhoneVerified(true);
      setOnboardingStep('post_verify');
      setScreen('chat');
      setError(null);
      const userLine: ChatLine = { id: `u-${Date.now()}`, role: 'user', content: 'ok' };
      setLines((prev) => [...prev, userLine]);
      const turn = await sendMessage(session.access_token, sessionId, 'ok');
      setLines((prev) => [
        ...prev,
        { id: `a-${Date.now()}`, role: 'assistant', content: turn.assistant_message },
      ]);
      applyTurn(turn, { forceVerified: true });
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Verify failed';
      setError(
        msg.includes('expired') || msg.includes('invalid')
          ? `${msg} — tap Send code again for a fresh OTP (dev: 000000)`
          : msg,
      );
      setOtpSent(false);
    } finally {
      setBusy(false);
      setThinking(false);
    }
  }

  async function onThatsMe() {
    if (!token || !sessionId) return;
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      const result = await completeSession(token, sessionId, readyToComplete);
      setCompleteResult(result);
      if (jointMoment?.joint_moment_id && !introSent) {
        await assignMariaDemoBlock();
        await sendJointMomentIntro(jointMoment.joint_moment_id);
        setIntroSent(true);
      }
      const eventRows = await loadClusterEvents();
      setEvents(eventRows.slice(0, 6));
      setScreen('discover');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Complete failed');
    } finally {
      setBusy(false);
      setThinking(false);
    }
  }

  const latestAssistant = [...lines].reverse().find((l) => l.role === 'assistant');
  const latestUser = [...lines].reverse().find((l) => l.role === 'user');
  const mariaNick = jointMoment?.candidate?.nickname || 'Maria';
  const showJointCard = screen === 'chat' && onboardingStep === 'offered_intro' && jointMoment;
  const showAuthField = uiIntent === 'collect_phone' || uiIntent === 'collect_otp';
  const showZipField = uiIntent === 'collect_zip';

  const composerPlaceholder = (() => {
    if (uiIntent === 'collect_zip') return 'Your 5-digit ZIP (e.g. 32827)…';
    if (uiIntent === 'collect_identity') return 'Tell Lana one thing about you…';
    if (uiIntent === 'collect_display_name') return 'Your first name…';
    if (uiIntent === 'collect_phone') return 'Phone on your account (e.g. +923…)…';
    if (uiIntent === 'collect_otp') {
      return loginPhoneHint
        ? `Code sent to ${loginPhoneHint} (dev: ${DEMO_TEST_OTP})…`
        : `6-digit code (dev: ${DEMO_TEST_OTP})…`;
    }
    if (onboardingStep === 'awaiting_intro_name') return `What should ${mariaNick} call you?`;
    if (onboardingStep === 'post_verify' || phoneVerified) {
      return 'Chat with Lana — find neighbors, activities…';
    }
    return 'Say hi, ask anything, or try “find people like me”…';
  })();

  async function onStructuredSubmit(e: FormEvent) {
    e.preventDefault();
    if (!token || !sessionId || busy) return;
    const text =
      uiIntent === 'collect_otp'
        ? otpDraft.trim()
        : uiIntent === 'collect_phone'
          ? toE164(phoneDraft)
          : uiIntent === 'collect_zip'
            ? draft.trim()
            : draft.trim();
    if (!text) return;
    if (uiIntent === 'collect_phone') setPhoneDraft(text);
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      await pushTurn(token, sessionId, text);
      if (uiIntent === 'collect_phone') setOtpDraft(DEMO_TEST_OTP);
      if (uiIntent === 'collect_otp') setOtpDraft('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
    } finally {
      setBusy(false);
      setThinking(false);
    }
  }

  return (
    <div className="meet-lana-page">
      <header className="meet-lana-topbar meet-lana-topbar--minimal">
        <Link href="/lana" className="meet-lana-back">
          ← Inbox
        </Link>
        {screen === 'chat' && (
          <button
            type="button"
            className={`meet-lana-account-btn${signedInUser && !signedInUser.isAnonymous ? ' meet-lana-account-btn--signed-in' : ''}`}
            onClick={() => setAccountOpen((o) => !o)}
            aria-expanded={accountOpen}
          >
            {signedInUser && !signedInUser.isAnonymous
              ? signedInUser.nickname || signedInUser.phone || 'Signed in'
              : 'Guest'}
          </button>
        )}
      </header>

      {accountOpen && screen === 'chat' && (
        <div className="meet-lana-account-panel" role="region" aria-label="Account">
          {signedInUser && !signedInUser.isAnonymous ? (
            <>
              <p className="meet-lana-account-status">Signed in</p>
              <p className="meet-lana-account-line">
                <strong>Phone:</strong> {signedInUser.phone || '—'}
              </p>
              <p className="meet-lana-account-line">
                <strong>Name:</strong>{' '}
                {signedInUser.nickname || '(not set — tell Lana what to call you)'}
              </p>
              <p className="meet-lana-account-line">
                <strong>Block:</strong>{' '}
                {signedInUser.homeBlockId ? 'assigned' : 'not set — say your ZIP in chat'}
              </p>
              <button
                type="button"
                className="meet-lana-account-signout"
                onClick={async () => {
                  await demoSignOut();
                  setSignedInUser(null);
                  setPhoneVerified(false);
                  setToken(null);
                  setSessionId(null);
                  setLines([]);
                  setScreen('welcome');
                  setAccountOpen(false);
                }}
              >
                Sign out
              </button>
            </>
          ) : (
            <>
              <p className="meet-lana-account-status">Browsing as guest</p>
              <p className="meet-lana-account-line">
                Say <strong>log in</strong> in chat to use an existing phone account, or verify
                during discovery to sign up.
              </p>
            </>
          )}
        </div>
      )}

      <div className="meet-lana-phone">
        {error && screen !== 'welcome' && <p className="meet-lana-error">{error}</p>}

        {screen === 'welcome' && (
          <div className="meet-lana-card meet-lana-welcome">
            <div className="meet-lana-pill meet-lana-pill--orange">
              <span className="meet-lana-dot meet-lana-dot--orange" />
              Lana · here for moms
            </div>
            <h1 className="meet-lana-serif">
              Hi. I&apos;m <span className="meet-lana-accent">Lana</span>.
            </h1>
            <p className="meet-lana-lede">
              Your block concierge — chat first, find neighbors when you&apos;re ready.
              Dev phone <strong>{DEMO_TEST_PHONE}</strong> · OTP <strong>{DEMO_TEST_OTP}</strong>.
            </p>
            <LanaSheep mood="idle" size="lg" />
            <button
              type="button"
              className="meet-lana-cta meet-lana-cta--wide"
              onClick={onMeetLana}
              disabled={busy}
            >
              {busy ? 'One sec…' : 'Meet Lana →'}
            </button>
            <p className="meet-lana-muted meet-lana-login-hint">
              Already have an account? Tap <strong>Meet Lana</strong>, then say{' '}
              <strong>&ldquo;log in&rdquo;</strong> in chat.
            </p>
            <nav className="meet-lana-footer-nav" aria-label="Demo links">
              <button type="button" onClick={() => setDevOpen(true)}>
                dev sign-in
              </button>
              <Link href="/lana">lana history</Link>
            </nav>
            {error && <p className="meet-lana-error meet-lana-error--inline">{error}</p>}
          </div>
        )}

        {screen === 'chat' && (
          <div className={`meet-lana-card meet-lana-chat ${thinking ? 'meet-lana-chat--thinking' : ''}`}>
            <div className={`meet-lana-pill ${showJointCard ? 'meet-lana-pill--purple' : 'meet-lana-pill--orange'}`}>
              <span
                className={`meet-lana-dot ${
                  showJointCard
                    ? 'meet-lana-dot--purple'
                    : thinking
                      ? 'meet-lana-dot--pulse-blue'
                      : 'meet-lana-dot--rec'
                }`}
              />
              Lana · {showJointCard ? 'noticing something' : thinking ? 'writing' : 'listening'}
            </div>

            {showJointCard ? (
              <div className="meet-lana-joint">
                <div className="meet-lana-joint-label">JOINT MOMENT</div>
                <p className="meet-lana-joint-text">
                  <LanaRichText
                    text={
                      jointMoment.lana_copy ||
                      `${mariaNick} told me she's looking for neighbors like you. Want me to put you two together?`
                    }
                  />
                </p>
                <div className="meet-lana-choice-row">
                  <button
                    type="button"
                    className="meet-lana-choice meet-lana-choice--on-purple"
                    onClick={() => onJointChoice(false)}
                    disabled={busy}
                  >
                    Keep exploring
                  </button>
                  <button
                    type="button"
                    className="meet-lana-choice meet-lana-choice--white"
                    onClick={() => onJointChoice(true)}
                    disabled={busy}
                  >
                    Yes · introduce us →
                  </button>
                </div>
              </div>
            ) : thinking ? (
              <div className="meet-lana-bubble meet-lana-bubble--speech meet-lana-bubble--typing">
                <div className="meet-lana-typing-dots" aria-label="Lana is thinking">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            ) : (
              latestAssistant && (
                <div className="meet-lana-bubble meet-lana-bubble--speech">
                  <p className="meet-lana-bubble-q">
                    <LanaRichText text={latestAssistant.content} />
                  </p>
                  {!showJointCard && (
                    <p className="meet-lana-bubble-sub">tap to talk · or type</p>
                  )}
                </div>
              )
            )}

            <LanaSheep mood={showJointCard ? 'noticing' : thinking ? 'thinking' : 'idle'} size="md" />

            {showJointCard && (
              <div className="meet-lana-peer-card">
                <div className="meet-lana-peer-avatar">{mariaNick[0]?.toUpperCase()}</div>
                <div>
                  <div className="meet-lana-peer-name">
                    {mariaNick}
                    {jointMoment.is_demo ? ' · Paulista' : ''}
                  </div>
                  <div className="meet-lana-peer-meta">
                    {jointMoment.match_reason || 'on your block'}
                    {jointMoment.is_demo ? ' · 3 blocks' : ''}
                  </div>
                </div>
                {jointMoment.is_demo && <div className="meet-lana-peer-score">5/7</div>}
              </div>
            )}

            {activityPreviews.length > 0 && !showJointCard && (
              <div className="meet-lana-peer-preview-list">
                {activityPreviews.slice(0, 5).map((ev, i) => (
                  <div key={`act-${i}`} className="meet-lana-peer-card meet-lana-activity-card">
                    <div className="meet-lana-peer-avatar meet-lana-activity-icon">📅</div>
                    <div>
                      <div className="meet-lana-peer-name">{ev.title}</div>
                      <div className="meet-lana-peer-meta">
                        {[ev.starts_label, ev.venue_name].filter(Boolean).join(' · ')}
                        {ev.preview ? ' · preview' : ''}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {peerMatches.length > 0 && activityPreviews.length === 0 && !showJointCard && (
              <div className="meet-lana-peer-preview-list">
                {peerMatches.slice(0, 3).map((p, i) => (
                  <div key={`peer-${i}`} className="meet-lana-peer-card">
                    <div className="meet-lana-peer-avatar">
                      {p.preview ? '?' : (p.nickname?.[0] ?? 'N')}
                    </div>
                    <div>
                      <div className="meet-lana-peer-name">
                        {p.preview ? `Neighbor ${i + 1}` : p.nickname || 'Neighbor'}
                      </div>
                      <div className="meet-lana-peer-meta">
                        {p.matching_peer_label || 'shared interests'}
                        {p.preview ? ' · preview' : ''}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {readyToComplete && !thinking && !showJointCard && (
              <button
                type="button"
                className="meet-lana-thats-me"
                onClick={onThatsMe}
                disabled={busy}
              >
                That&apos;s me ✓
              </button>
            )}

            {!showJointCard && showAuthField && (
              <form
                className="meet-lana-composer meet-lana-composer--phone meet-lana-composer--intent"
                onSubmit={onStructuredSubmit}
              >
                <input
                  value={uiIntent === 'collect_otp' ? otpDraft : phoneDraft}
                  onChange={(e) =>
                    uiIntent === 'collect_otp'
                      ? setOtpDraft(e.target.value)
                      : setPhoneDraft(e.target.value)
                  }
                  placeholder={
                    uiIntent === 'collect_otp'
                      ? `6-digit code (dev: ${DEMO_TEST_OTP})`
                      : 'Phone with country code'
                  }
                  inputMode={uiIntent === 'collect_otp' ? 'numeric' : 'tel'}
                  autoComplete={uiIntent === 'collect_otp' ? 'one-time-code' : 'tel'}
                  maxLength={uiIntent === 'collect_otp' ? 6 : 20}
                  disabled={busy}
                  aria-label={uiIntent === 'collect_otp' ? 'Verification code' : 'Phone number'}
                />
                <button
                  type="submit"
                  className="meet-lana-send meet-lana-send--wide"
                  disabled={
                    busy ||
                    (uiIntent === 'collect_otp' ? !otpDraft.trim() : !phoneDraft.trim())
                  }
                >
                  {busy ? '…' : uiIntent === 'collect_otp' ? 'Verify →' : 'Continue →'}
                </button>
              </form>
            )}

            {!showJointCard && !showAuthField && (
              <form
                className={`meet-lana-composer ${thinking ? 'meet-lana-composer--busy' : ''}`}
                onSubmit={showZipField ? onStructuredSubmit : onSend}
              >
                <input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder={thinking ? 'Waiting for Lana…' : composerPlaceholder}
                  disabled={busy}
                  inputMode={showZipField ? 'numeric' : undefined}
                  maxLength={showZipField ? 5 : undefined}
                  aria-label="Message to Lana"
                />
                <button
                  type="button"
                  className="meet-lana-mic-btn"
                  aria-label="Demo fill"
                  disabled={busy}
                  onClick={() =>
                    setDraft(
                      (d) =>
                        d ||
                        "I'm a Latino mom in Lake Nona, new here about 3 months.",
                    )
                  }
                >
                  🎤
                </button>
                <button type="submit" className="meet-lana-send" disabled={busy || !draft.trim()}>
                  Send →
                </button>
              </form>
            )}

            {showJointCard && (
              <p className="meet-lana-muted meet-lana-tap-hint">Tap a choice above · or type instead</p>
            )}

            <details className="meet-lana-history">
              <summary>
                Full chat ({lines.length}) · intent: {uiIntent} · phase:{' '}
                {routingPhase || onboardingStep}
                {lastOrchestrator != null ? ` · AI: ${lastOrchestrator ? 'on' : 'rules'}` : ''}
              </summary>
              <div className="meet-lana-history-list">
                {lines.map((line) => (
                  <div
                    key={line.id}
                    className={`meet-lana-history-line meet-lana-history-line--${line.role}`}
                  >
                    <strong>{line.role === 'user' ? 'You' : 'Lana'}:</strong>{' '}
                    <LanaRichText text={line.content} />
                  </div>
                ))}
              </div>
            </details>
            <div ref={chatEndRef} />
          </div>
        )}

        {screen === 'phone' && (
          <div className="meet-lana-card meet-lana-phone-flow">
            <div className="meet-lana-pill meet-lana-pill--purple">
              <span className="meet-lana-dot meet-lana-dot--purple" />
              Lana · just one thing
            </div>

            {latestUser && (
              <div className="meet-lana-user-echo">&ldquo;{latestUser.content}&rdquo;</div>
            )}

            <div className="meet-lana-bubble meet-lana-bubble--card meet-lana-phone-card">
              <p className="meet-lana-phone-serif">
                <em>Beautiful.</em>
              </p>
              <p className="meet-lana-phone-body">
                To send {mariaNick} a real note — and so she knows you&apos;re her real neighbor — I
                just need to text you a code.
              </p>
              <div className="meet-lana-hint-box">
                <span className="meet-lana-hint-icon">?</span>
                <span>What&apos;s your number?</span>
              </div>
              <p className="meet-lana-phone-fine">verifying only · no spam, ever · we never share</p>
            </div>

            <LanaSheep mood="idle" size="md" />

            <form className="meet-lana-composer meet-lana-composer--phone" onSubmit={otpSent ? onVerifyPhone : onSendPhoneCode}>
              <input
                value={otpSent ? otpDraft : phoneDraft}
                onChange={(e) => (otpSent ? setOtpDraft(e.target.value) : setPhoneDraft(e.target.value))}
                placeholder={otpSent ? '6-digit code' : '(407) 555-0198'}
                disabled={busy}
                aria-label={otpSent ? 'OTP code' : 'Phone number'}
              />
              <button
                type="submit"
                className="meet-lana-send meet-lana-send--wide"
                disabled={busy || (otpSent && !otpDraft.trim())}
              >
                {busy ? '…' : otpSent ? 'Verify →' : 'Send code →'}
              </button>
            </form>

            {!otpSent && (
              <p className="meet-lana-muted">
                Dev: use a <strong>US test number</strong> (e.g. +15550999012) in Supabase → Auth →
                Phone → Test numbers · OTP <strong>000000</strong>. Real PK numbers need Twilio.
              </p>
            )}
          </div>
        )}

        {screen === 'discover' && completeResult && (
          <div className="meet-lana-card meet-lana-discover">
            <div className="meet-lana-pill meet-lana-pill--purple">
              <span className="meet-lana-dot meet-lana-dot--purple" />
              Lana · intro sent
            </div>
            <p className="meet-lana-lede">
              {introSent
                ? `Note sent to ${mariaNick}. Finish exploring your block.`
                : 'Profile saved.'}
            </p>
            <LanaSheep mood="noticing" size="md" />

            <section className="meet-lana-section">
              <h3>Activities nearby</h3>
              {events.length === 0 && (
                <p className="meet-lana-muted">No open events in the next 14 days.</p>
              )}
              <ul className="meet-lana-event-list">
                {events.map((ev) => (
                  <li key={ev.id}>
                    <strong>{ev.title}</strong>
                    <span>{new Date(ev.starts_at).toLocaleString()}</span>
                  </li>
                ))}
              </ul>
            </section>

            <section className="meet-lana-insights">
              <MappedSummary
                summary={completeResult.mapped_summary ?? undefined}
                spans={completeResult.spans}
              />
              <ClaimCards
                claims={completeResult.claims.map((c) => ({
                  label: c.label,
                  confidence: c.confidence,
                  source_quote: c.source_quote ?? null,
                  bucket: c.bucket ?? null,
                  synonyms: c.synonyms ?? [],
                }))}
              />
            </section>

            <button
              type="button"
              className="meet-lana-cta meet-lana-cta--wide"
              onClick={() => {
                setLines([]);
                setSessionId(null);
                setJointMoment(null);
                setOnboardingStep('early_chat');
                setRoutingPhase('listening');
                setPeerMatches([]);
                setIntroSent(false);
                setOtpSent(false);
                setPhoneVerified(false);
                setScreen('welcome');
              }}
            >
              Start over →
            </button>
          </div>
        )}

        <div className="meet-lana-dots" aria-hidden>
          <span className={screen === 'welcome' ? 'on' : ''} />
          <span className={screen === 'chat' || screen === 'phone' ? 'on' : ''} />
          <span className={screen === 'discover' ? 'on' : ''} />
        </div>
      </div>

      {devOpen && (
        <div className="meet-lana-dev-overlay" role="dialog" aria-label="Dev sign-in">
          <div className="meet-lana-dev-sheet">
            <h2>Dev sign-in</h2>
            <p className="meet-lana-muted">
              Test phone must be in Supabase Dashboard → Auth → Phone → Test numbers.
            </p>
            <form onSubmit={runDevSetup} className="meet-lana-setup-form">
              <label>
                Phone
                <input value={setupPhone} onChange={(e) => setSetupPhone(e.target.value)} />
              </label>
              <label>
                OTP
                <input value={setupOtp} onChange={(e) => setSetupOtp(e.target.value)} />
              </label>
              <button type="submit" className="meet-lana-cta" disabled={busy}>
                {busy ? 'Saving…' : 'Save & close'}
              </button>
            </form>
            <button type="button" className="meet-lana-link" onClick={() => setDevOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
