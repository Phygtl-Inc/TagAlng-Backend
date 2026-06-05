'use client';

import { LanaRichText } from '@/components/LanaRichText';
import { LanaSheep } from '@/components/LanaSheep';
import { ClaimCards } from '@/components/ClaimCards';
import { MappedSummary } from '@/components/MappedSummary';
import {
  completeSession,
  sendMessage,
  startProfileSession,
  type CompleteResult,
  type LanaTurn,
} from '@/lib/lana-client';
import {
  DEMO_TEST_OTP,
  DEMO_TEST_PHONE,
  demoPhoneAuth,
  ensureDemoBlock,
  ensureDemoReady,
  getDemoSupabase,
  loadClusterEvents,
  loadPeerMatches,
  type ClusterEvent,
  type PeerMatch,
} from '@/lib/demo-user';
import Link from 'next/link';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';

type Screen = 'welcome' | 'intro' | 'chat' | 'discover';

type ChatLine = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  ui?: LanaTurn['ui'];
};

function formatEventWhen(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
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
  const [peers, setPeers] = useState<PeerMatch[]>([]);
  const [events, setEvents] = useState<ClusterEvent[]>([]);
  const [thinking, setThinking] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const scrollChat = useCallback(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollChat();
  }, [lines, screen, thinking, scrollChat]);

  /** Restore demo session quietly — never hijacks the welcome screen. */
  useEffect(() => {
    (async () => {
      try {
        const supabase = getDemoSupabase();
        const { data } = await supabase.auth.getSession();
        if (data.session?.access_token) setToken(data.session.access_token);
      } catch {
        /* welcome stays default */
      }
    })();
  }, []);

  async function bootstrapNeighbor(): Promise<string> {
    const access = await ensureDemoReady(setupPhone, setupOtp);
    setToken(access);
    return access;
  }

  async function onMeetLana() {
    setBusy(true);
    setError(null);
    try {
      await bootstrapNeighbor();
      setScreen('intro');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start — check dev sign-in');
      setDevOpen(true);
    } finally {
      setBusy(false);
    }
  }

  async function onAlreadyHaveAccount() {
    setBusy(true);
    setError(null);
    try {
      await bootstrapNeighbor();
      setScreen('intro');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed');
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
      setDevOpen(false);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Setup failed');
    } finally {
      setBusy(false);
    }
  }

  /** Starts real Lana profile intake — only after intro screen. */
  async function beginProfileChat() {
    setBusy(true);
    setThinking(true);
    setError(null);
    try {
      const access = token || (await bootstrapNeighbor());
      const session = await startProfileSession(access);
      setSessionId(session.session_id);
      setLines([
        {
          id: 'open',
          role: 'assistant',
          content: session.assistant_message,
          ui: session.ui,
        },
      ]);
      setReadyToComplete(session.ready_to_complete);
      setScreen('chat');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Lana could not start the chat');
      setDevOpen(true);
    } finally {
      setBusy(false);
      setThinking(false);
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
    const userLine: ChatLine = { id: `u-${Date.now()}`, role: 'user', content: text };
    setLines((prev) => [...prev, userLine]);
    try {
      const turn = await sendMessage(token, sessionId, text);
      setLines((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          role: 'assistant',
          content: turn.assistant_message,
          ui: turn.ui,
        },
      ]);
      setReadyToComplete(turn.ready_to_complete);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Send failed');
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
      const [peerRows, eventRows] = await Promise.all([
        loadPeerMatches(),
        loadClusterEvents(),
      ]);
      setPeers(peerRows);
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

  return (
    <div className="meet-lana-page">
      <header className="meet-lana-topbar meet-lana-topbar--minimal">
        <Link href="/lana" className="meet-lana-back">
          ← Inbox
        </Link>
      </header>

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
              The concierge for moms on your block. I help you{' '}
              <strong>meet, exchange, and host</strong> — no feeds, no forms.
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
            <button
              type="button"
              className="meet-lana-link meet-lana-link--account"
              onClick={onAlreadyHaveAccount}
              disabled={busy}
            >
              I already have an account
            </button>
            <nav className="meet-lana-footer-nav" aria-label="Demo links">
              <button type="button" onClick={() => setDevOpen(true)}>
                dev sign-in
              </button>
              <Link href="/lana">lana history</Link>
            </nav>
            {error && <p className="meet-lana-error meet-lana-error--inline">{error}</p>}
          </div>
        )}

        {screen === 'intro' && (
          <div className="meet-lana-card meet-lana-intro">
            <div className="meet-lana-pill meet-lana-pill--orange">
              <span className="meet-lana-dot meet-lana-dot--orange" />
              Lana · ready
              <span className="meet-lana-chip meet-lana-chip--blue">writes</span>
            </div>
            <div className="meet-lana-bubble meet-lana-bubble--card">
              <p className="meet-lana-intro-greet">
                Hi. I&apos;m <em>Lana</em>. I help moms on your block meet, exchange, and host.
              </p>
              <div className="meet-lana-hint-box">
                <span className="meet-lana-hint-icon">?</span>
                <span>
                  Want me to find people nearby in <em>your life stage</em>?
                </span>
              </div>
              <div className="meet-lana-choice-row">
                <button
                  type="button"
                  className="meet-lana-choice meet-lana-choice--ghost"
                  onClick={beginProfileChat}
                  disabled={busy}
                >
                  Maybe later
                </button>
                <button
                  type="button"
                  className="meet-lana-choice meet-lana-choice--primary"
                  onClick={beginProfileChat}
                  disabled={busy}
                >
                  {busy ? 'Starting…' : 'Yes · find them →'}
                </button>
              </div>
            </div>
            <LanaSheep mood="idle" size="md" />
            <button
              type="button"
              className="meet-lana-mic-hint"
              onClick={beginProfileChat}
              disabled={busy}
            >
              <span className="meet-lana-mic-ring">
                <span className="meet-lana-mic-icon">🎤</span>
              </span>
              Tap to talk · or pick a chip
            </button>
            <button type="button" className="meet-lana-link" onClick={beginProfileChat} disabled={busy}>
              ⌨️ type instead
            </button>
          </div>
        )}

        {screen === 'chat' && (
          <div className={`meet-lana-card meet-lana-chat ${thinking ? 'meet-lana-chat--thinking' : ''}`}>
            <div className="meet-lana-pill meet-lana-pill--orange">
              <span
                className={`meet-lana-dot ${thinking ? 'meet-lana-dot--pulse-blue' : 'meet-lana-dot--rec'}`}
              />
              Lana · {thinking ? 'writing' : 'listening'}
              {thinking && <span className="meet-lana-chip meet-lana-chip--blue">writes</span>}
            </div>

            {thinking ? (
              <div className="meet-lana-bubble meet-lana-bubble--speech meet-lana-bubble--typing">
                <div className="meet-lana-typing-dots" aria-label="Lana is thinking">
                  <span />
                  <span />
                  <span />
                </div>
                <p className="meet-lana-bubble-sub">Lana is thinking…</p>
              </div>
            ) : (
              latestAssistant && (
                <div className="meet-lana-bubble meet-lana-bubble--speech">
                  <p className="meet-lana-bubble-q">
                    <LanaRichText text={latestAssistant.content} />
                  </p>
                  <p className="meet-lana-bubble-sub">tap to talk · or type</p>
                </div>
              )
            )}

            <LanaSheep mood={thinking ? 'thinking' : 'idle'} size="md" />

            {readyToComplete && !thinking && (
              <button
                type="button"
                className="meet-lana-thats-me"
                onClick={onThatsMe}
                disabled={busy}
              >
                That&apos;s me ✓
              </button>
            )}

            <form
              className={`meet-lana-composer ${thinking ? 'meet-lana-composer--busy' : ''}`}
              onSubmit={onSend}
            >
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={thinking ? 'Waiting for Lana…' : 'Tell Lana about yourself…'}
                disabled={busy}
                aria-label="Message to Lana"
                aria-busy={thinking}
              />
              <button
                type="button"
                className="meet-lana-mic-btn"
                aria-label="Voice (demo: type instead)"
                disabled={busy}
                onClick={() =>
                  setDraft((d) => d || "I'm a mom in Lake Nona — new here, two toddlers.")
                }
              >
                🎤
              </button>
              <button
                type="submit"
                className="meet-lana-send"
                disabled={busy || (!thinking && !draft.trim())}
              >
                {thinking ? (
                  <span className="meet-lana-send-spinner" aria-hidden />
                ) : (
                  'Send →'
                )}
              </button>
            </form>

            {error && <p className="meet-lana-error meet-lana-error--inline">{error}</p>}

            {lines.length > 0 && (
              <details className="meet-lana-history" open={thinking}>
                <summary>Full chat ({lines.length} messages)</summary>
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
                  {thinking && (
                    <div className="meet-lana-history-line meet-lana-history-line--assistant meet-lana-history-line--pending">
                      <strong>Lana:</strong> <span className="meet-lana-typing-inline">…</span>
                    </div>
                  )}
                </div>
              </details>
            )}
            <div ref={chatEndRef} />
          </div>
        )}

        {screen === 'discover' && completeResult && (
          <div className="meet-lana-card meet-lana-discover">
            <div className="meet-lana-pill meet-lana-pill--purple">
              <span className="meet-lana-dot meet-lana-dot--purple" />
              Lana · noticing something
            </div>

            {peers[0] ? (
              <div className="meet-lana-joint">
                <div className="meet-lana-joint-label">JOINT MOMENT</div>
                <p className="meet-lana-joint-text">
                  <strong>{peers[0].nickname || 'A neighbor'}</strong> shares{' '}
                  <em>{peers[0].matching_peer_label || 'something in common'}</em> with you on your
                  block. Want me to put you two together?
                </p>
                <div className="meet-lana-choice-row">
                  <button
                    type="button"
                    className="meet-lana-choice meet-lana-choice--ghost meet-lana-choice--on-purple"
                  >
                    Keep exploring
                  </button>
                  <button type="button" className="meet-lana-choice meet-lana-choice--white">
                    Yes · introduce us →
                  </button>
                </div>
              </div>
            ) : (
              <div className="meet-lana-joint meet-lana-joint--muted">
                <div className="meet-lana-joint-label">PROFILE SAVED</div>
                <p className="meet-lana-joint-text">
                  Your threads are in — I&apos;m still looking for strong matches on your block.
                </p>
              </div>
            )}

            <LanaSheep mood="noticing" size="md" />

            {peers[0] && (
              <div className="meet-lana-peer-card">
                <div className="meet-lana-peer-avatar">
                  {(peers[0].nickname || '?')[0]?.toUpperCase()}
                </div>
                <div>
                  <div className="meet-lana-peer-name">{peers[0].nickname || 'Neighbor'}</div>
                  <div className="meet-lana-peer-meta">
                    {peers[0].matching_peer_label || 'Shared thread'} · your block
                  </div>
                </div>
                <div className="meet-lana-peer-score">
                  {Math.round(peers[0].similarity_score * 100)}%
                </div>
              </div>
            )}

            <section className="meet-lana-section">
              <h3>People on your block</h3>
              {peers.length === 0 && (
                <p className="meet-lana-muted">No vector matches yet — try seed data or more claims.</p>
              )}
              <ul className="meet-lana-peer-list">
                {peers.map((p) => (
                  <li key={p.peer_user_id}>
                    <span>{p.nickname || p.peer_user_id.slice(0, 8)}</span>
                    <span>{p.matching_peer_label}</span>
                    <span>{Math.round(p.similarity_score * 100)}%</span>
                  </li>
                ))}
              </ul>
            </section>

            <section className="meet-lana-section">
              <h3>Activities nearby</h3>
              {events.length === 0 && (
                <p className="meet-lana-muted">No open events in the next 14 days.</p>
              )}
              <ul className="meet-lana-event-list">
                {events.map((ev) => (
                  <li key={ev.id}>
                    <strong>{ev.title}</strong>
                    <span>{formatEventWhen(ev.starts_at)}</span>
                    {ev.venue_name && <span>{ev.venue_name}</span>}
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
                setScreen('welcome');
              }}
            >
              Start over →
            </button>
          </div>
        )}

        <div className="meet-lana-dots" aria-hidden>
          <span className={screen === 'welcome' ? 'on' : ''} />
          <span className={screen === 'intro' || screen === 'chat' ? 'on' : ''} />
          <span className={screen === 'discover' ? 'on' : ''} />
        </div>
      </div>

      {devOpen && (
        <div className="meet-lana-dev-overlay" role="dialog" aria-label="Dev sign-in">
          <div className="meet-lana-dev-sheet">
            <h2>Dev sign-in</h2>
            <p className="meet-lana-muted">
              Hidden setup — uses Supabase test phone <strong>+15550000000</strong> /{' '}
              <strong>000000</strong> (same as Postman).
            </p>
            <form onSubmit={runDevSetup} className="meet-lana-setup-form">
              <label>
                Phone
                <input
                  value={setupPhone}
                  onChange={(e) => setSetupPhone(e.target.value)}
                />
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
