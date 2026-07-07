'use client';

import { ClaimCards } from '@/components/ClaimCards';
import { LanaBubble } from '@/components/LanaBubble';
import { MappedSummary } from '@/components/MappedSummary';
import { RapportCards } from '@/components/RapportCards';
import {
  getSupabase,
  type Conversation,
  type InboxSession,
  type RapportGap,
} from '@/lib/supabase';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

export default function LanaInboxPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<InboxSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [rapport, setRapport] = useState<RapportGap[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingChat, setLoadingChat] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ensureAuth = useCallback(async () => {
    const supabase = getSupabase();
    const { data } = await supabase.auth.getSession();
    if (!data.session) {
      router.replace('/login');
      return false;
    }
    const { data: ok } = await supabase.rpc('is_tagalng_admin');
    if (!ok) {
      await supabase.auth.signOut();
      router.replace('/login');
      return false;
    }
    return true;
  }, [router]);

  const loadSessions = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    try {
      const supabase = getSupabase();
      const { data, error: rpcErr } = await supabase.rpc('admin_list_lana_sessions', {
        p_limit: 80,
        p_offset: 0,
        p_status: null,
      });
      if (rpcErr) throw rpcErr;
      setSessions((data as InboxSession[]) || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load inbox');
    } finally {
      setLoadingList(false);
    }
  }, []);

  const loadConversation = useCallback(async (sessionId: string) => {
    setLoadingChat(true);
    setError(null);
    try {
      const supabase = getSupabase();
      const { data, error: rpcErr } = await supabase.rpc('admin_get_lana_conversation', {
        p_session_id: sessionId,
      });
      if (rpcErr) throw rpcErr;
      setConversation(data as Conversation);
      // Rapport gaps are keyed on the user, not the session — fetch them for this user.
      const userId = (data as { session?: { user_id?: string } })?.session?.user_id;
      if (userId) {
        const { data: gaps } = await supabase.rpc('admin_get_rapport_gaps', {
          p_user_id: userId,
        });
        setRapport((gaps as RapportGap[]) ?? []);
      } else {
        setRapport([]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load conversation');
      setConversation(null);
      setRapport([]);
    } finally {
      setLoadingChat(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (await ensureAuth()) await loadSessions();
    })();
  }, [ensureAuth, loadSessions]);

  useEffect(() => {
    if (selectedId) loadConversation(selectedId);
    else {
      setConversation(null);
      setRapport([]);
    }
  }, [selectedId, loadConversation]);

  async function signOut() {
    const supabase = getSupabase();
    await supabase.auth.signOut();
    router.replace('/login');
  }

  function sessionTitle(s: InboxSession) {
    return s.nickname || s.phone || s.user_id.slice(0, 8);
  }

  return (
    <div className="inbox-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>Lana inbox</h1>
          <p className="sub">All signup chats · tagalng-dev</p>
          <button type="button" onClick={signOut}>
            Sign out
          </button>
        </div>
        <div className="meet-lana-sidebar-links">
          <Link href="/lana/meet" className="meet-lana-demo-link">
            Meet Lana (unified chat) →
          </Link>
        </div>
        <div className="session-list">
          {loadingList && <p className="loading">Loading…</p>}
          {!loadingList &&
            sessions.map((s) => (
              <button
                key={s.session_id}
                type="button"
                className={`session-item ${selectedId === s.session_id ? 'session-item--active' : ''}`}
                onClick={() => setSelectedId(s.session_id)}
              >
                <div className="session-title">{sessionTitle(s)}</div>
                <div className="session-preview">
                  {s.last_role === 'assistant' ? 'Lana: ' : ''}
                  {s.last_message_preview || '(no messages)'}
                </div>
                <div className="session-meta">
                  <span
                    className={`status-badge status-badge--${s.status === 'completed' ? 'completed' : 'active'}`}
                  >
                    {s.status}
                  </span>
                  <span>{s.message_count} msgs</span>
                </div>
              </button>
            ))}
        </div>
      </aside>

      <main className="chat-pane">
        {!selectedId && <div className="chat-empty">Select a conversation</div>}
        {selectedId && conversation && (
          <>
            <header className="chat-header">
              <div className="chat-header-main">
                <h2>
                  {sessionTitle(
                    sessions.find((x) => x.session_id === selectedId) || {
                      session_id: selectedId,
                      user_id: '',
                      phone: conversation.user.phone,
                      nickname: conversation.user.nickname,
                      purpose: '',
                      status: conversation.session.status,
                      message_count: 0,
                      last_message_at: null,
                      last_message_preview: null,
                      last_role: null,
                      created_at: '',
                    }
                  )}
                </h2>
                <span
                  className={`status-badge status-badge--${conversation.session.status === 'completed' ? 'completed' : 'active'}`}
                >
                  {conversation.session.status}
                </span>
              </div>
              <dl className="chat-header-facts">
                {conversation.user.phone && (
                  <>
                    <dt>Phone</dt>
                    <dd>{conversation.user.phone}</dd>
                  </>
                )}
                {conversation.user.home_zip && (
                  <>
                    <dt>ZIP</dt>
                    <dd>{conversation.user.home_zip}</dd>
                  </>
                )}
                {conversation.user.home_block_id && (
                  <>
                    <dt>Block</dt>
                    <dd title={conversation.user.home_block_id}>
                      {conversation.user.home_block_id.slice(0, 12)}…
                    </dd>
                  </>
                )}
                <dt>Messages</dt>
                <dd>{conversation.messages.length}</dd>
              </dl>
            </header>

            {error && <p className="error-banner">{error}</p>}
            {loadingChat && <p className="loading">Loading thread…</p>}

            <div className="chat-pane-body">
              <section className="conversation-section" aria-label="Conversation">
                <div className="section-head">
                  <h3>Conversation</h3>
                  <p className="section-hint">Lana intake chat — user messages on the right</p>
                </div>
                <div className="chat-messages">
                  {conversation.messages.length === 0 && (
                    <p className="chat-messages-empty">No messages in this session yet.</p>
                  )}
                  {conversation.messages.map((m) => (
                    <LanaBubble
                      key={m.id}
                      role={m.role}
                      content={m.content}
                      ui={m.ui}
                      time={m.created_at}
                    />
                  ))}
                </div>
              </section>

              {(conversation.session.context?.mapped_summary ||
                (conversation.session.context?.spans?.length ?? 0) > 0 ||
                conversation.claims.length > 0 ||
                rapport.length > 0) && (
                <section className="insights-section" aria-label="Profile extraction">
                  <div className="section-head">
                    <h3>Profile extraction</h3>
                    <p className="section-hint">
                      Parsed from the chat after completion — color = identity bucket
                    </p>
                  </div>
                  <div className="insights-scroll">
                    <MappedSummary
                      summary={conversation.session.context?.mapped_summary}
                      spans={conversation.session.context?.spans}
                    />
                    <ClaimCards claims={conversation.claims} />
                    <RapportCards gaps={rapport} />
                  </div>
                </section>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
