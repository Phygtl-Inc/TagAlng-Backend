'use client';

import { getSupabase } from '@/lib/supabase';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type AxisScore = {
  axis: 'goal_completion' | 'warmth_tone' | 'five_beat_landed' | 'no_hallucination';
  verdict: 'PASS' | 'SOFT_FAIL' | 'HARD_FAIL';
  score: number;
  reasoning: string;
};

type SimRun = {
  id: string;
  run_id: string;
  git_sha: string | null;
  persona_id: string;
  seed_label: string;
  bucket: string;
  weighted_score: number;
  scores_json: AxisScore[] | null;
  judge_summary: string | null;
  hitl_status: 'pending' | 'reviewed' | 'skipped';
  tim_verdict: string | null;
  tim_note: string | null;
  sft_eligible: boolean;
  created_at: string;
  transcript_json: {
    persona_name: string;
    turn_count: number;
    pass_criteria: string;
    must_not: string;
    turns: {
      turn_number: number;
      user_message: string;
      lana_reply: string;
      intent_class: string | null;
      intent_confidence: number | null;
      tool_called: string | null;
    }[];
  } | null;
};

type Filters = {
  persona: string;
  bucket: string;
  hitl_status: string;
  sft_eligible: string;
  date: string;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const AXIS_LABELS: Record<string, string> = {
  goal_completion: 'Goal completion',
  warmth_tone: 'Warmth & tone',
  five_beat_landed: '5-beat refusal',
  no_hallucination: 'No hallucination',
};

const VERDICT_CLASS: Record<string, string> = {
  PASS: 'sim-verdict--pass',
  SOFT_FAIL: 'sim-verdict--soft',
  HARD_FAIL: 'sim-verdict--hard',
};

const BUCKET_COLOR: Record<string, string> = {
  in_scope_success: '#128c7e',
  out_of_scope_rejection: '#e85d04',
  ambiguous_clarity: '#7c3aed',
  edge_cases: '#1976d2',
};

function scoreColor(s: number): string {
  if (s >= 0.85) return '#2e7d32';
  if (s >= 0.6) return '#e65100';
  return '#c62828';
}

function fmt(s: number): string {
  return s.toFixed(3);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SimsPage() {
  const router = useRouter();
  const [runs, setRuns] = useState<SimRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>({
    persona: '',
    bucket: '',
    hitl_status: '',
    sft_eligible: '',
    date: '',
  });
  const [verdictTarget, setVerdictTarget] = useState<{
    runId: string;
    axis: string;
  } | null>(null);
  const [noteInput, setNoteInput] = useState('');
  const [saving, setSaving] = useState(false);

  const ensureAuth = useCallback(async () => {
    const supabase = getSupabase();
    const { data } = await supabase.auth.getSession();
    if (!data.session) { router.replace('/login'); return false; }
    const { data: ok } = await supabase.rpc('is_tagalng_admin');
    if (!ok) { await supabase.auth.signOut(); router.replace('/login'); return false; }
    return true;
  }, [router]);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const supabase = getSupabase();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let q = (supabase as any)
        .from('simulations')
        .select('id,run_id,git_sha,persona_id,seed_label,bucket,weighted_score,scores_json,judge_summary,hitl_status,tim_verdict,tim_note,sft_eligible,created_at,transcript_json')
        .order('weighted_score', { ascending: true })
        .limit(200);

      if (filters.persona) q = q.eq('persona_id', filters.persona);
      if (filters.bucket) q = q.eq('bucket', filters.bucket);
      if (filters.hitl_status) q = q.eq('hitl_status', filters.hitl_status);
      if (filters.sft_eligible === 'true') q = q.eq('sft_eligible', true);
      if (filters.sft_eligible === 'false') q = q.eq('sft_eligible', false);
      if (filters.date) {
        q = q.gte('created_at', `${filters.date}T00:00:00Z`).lte('created_at', `${filters.date}T23:59:59Z`);
      }

      const { data, error: err } = await q;
      if (err) throw err;
      setRuns((data as SimRun[]) || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load simulations');
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    (async () => { if (await ensureAuth()) await loadRuns(); })();
  }, [ensureAuth, loadRuns]);

  async function saveVerdict(
    run: SimRun,
    verdict: 'confirm' | 'false_positive' | 'false_negative' | 'flag_bug',
  ) {
    if (!verdictTarget) return;
    setSaving(true);
    try {
      const supabase = getSupabase();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const { error: err } = await (supabase as any)
        .from('simulations')
        .update({
          tim_verdict: verdict,
          tim_note: noteInput.trim() || null,
          hitl_status: 'reviewed',
        })
        .eq('id', run.id);
      if (err) throw err;
      setRuns((prev) =>
        prev.map((r) =>
          r.id === run.id
            ? { ...r, tim_verdict: verdict, tim_note: noteInput.trim() || null, hitl_status: 'reviewed' }
            : r
        )
      );
      setVerdictTarget(null);
      setNoteInput('');
    } catch (e) {
      alert(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  }

  async function skipRun(run: SimRun) {
    const supabase = getSupabase();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (supabase as any).from('simulations').update({ hitl_status: 'skipped' }).eq('id', run.id);
    setRuns((prev) => prev.map((r) => r.id === run.id ? { ...r, hitl_status: 'skipped' } : r));
  }

  const personas = [...new Set(runs.map((r) => r.persona_id))].sort();
  const buckets = [...new Set(runs.map((r) => r.bucket))].sort();
  const dates = [...new Set(runs.map((r) => r.created_at.slice(0, 10)))].sort().reverse();
  const pendingCount = runs.filter((r) => r.hitl_status === 'pending').length;

  return (
    <div className="inbox-shell">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>Sim results</h1>
          <p className="sub">Sorted worst → best · {runs.length} runs</p>
          {pendingCount > 0 && (
            <p className="sub" style={{ color: '#e85d04', fontWeight: 700 }}>
              {pendingCount} pending review
            </p>
          )}
          <div style={{ marginTop: '0.75rem', display: 'flex', gap: '0.5rem' }}>
            <Link href="/lana" style={{ fontSize: '0.8rem', color: '#128c7e' }}>← Lana inbox</Link>
          </div>
        </div>

        {/* Filters */}
        <div style={{ padding: '0.75rem 1rem', borderBottom: '1px solid #d1d7db', background: '#fafafa' }}>
          <p style={{ margin: '0 0 0.5rem', fontSize: '0.7rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: '#8696a0' }}>
            Filters
          </p>
          <select
            className="sim-filter-select"
            value={filters.persona}
            onChange={(e) => setFilters((f) => ({ ...f, persona: e.target.value }))}
          >
            <option value="">All personas</option>
            {personas.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <select
            className="sim-filter-select"
            value={filters.bucket}
            onChange={(e) => setFilters((f) => ({ ...f, bucket: e.target.value }))}
          >
            <option value="">All buckets</option>
            {buckets.map((b) => <option key={b} value={b}>{b.replace(/_/g, ' ')}</option>)}
          </select>
          <select
            className="sim-filter-select"
            value={filters.hitl_status}
            onChange={(e) => setFilters((f) => ({ ...f, hitl_status: e.target.value }))}
          >
            <option value="">All statuses</option>
            <option value="pending">Pending</option>
            <option value="reviewed">Reviewed</option>
            <option value="skipped">Skipped</option>
          </select>
          <select
            className="sim-filter-select"
            value={filters.sft_eligible}
            onChange={(e) => setFilters((f) => ({ ...f, sft_eligible: e.target.value }))}
          >
            <option value="">SFT: all</option>
            <option value="true">SFT eligible</option>
            <option value="false">Not eligible</option>
          </select>
          <select
            className="sim-filter-select"
            value={filters.date}
            onChange={(e) => setFilters((f) => ({ ...f, date: e.target.value }))}
          >
            <option value="">All dates</option>
            {dates.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>

        {/* Run list */}
        <div className="session-list">
          {loading && <p className="loading">Loading…</p>}
          {error && <p className="error-banner">{error}</p>}
          {!loading && runs.map((run) => (
            <button
              key={run.id}
              type="button"
              className={`session-item ${expandedId === run.id ? 'session-item--active' : ''}`}
              onClick={() => setExpandedId(expandedId === run.id ? null : run.id)}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <span className="session-title">{run.persona_id} · {run.seed_label}</span>
                <span style={{ fontWeight: 800, fontSize: '0.85rem', color: scoreColor(run.weighted_score) }}>
                  {fmt(run.weighted_score)}
                </span>
              </div>
              <div className="session-preview" style={{ color: BUCKET_COLOR[run.bucket] || '#667' }}>
                {run.bucket.replace(/_/g, ' ')}
              </div>
              <div className="session-meta">
                <span className={`status-badge ${run.hitl_status === 'reviewed' ? 'status-badge--completed' : run.hitl_status === 'skipped' ? '' : 'status-badge--active'}`}>
                  {run.hitl_status}
                </span>
                {run.sft_eligible && <span style={{ color: '#2e7d32', fontWeight: 700 }}>SFT ✓</span>}
                {run.tim_verdict === 'flag_bug' && <span style={{ color: '#f59e0b', fontWeight: 700 }}>🚩</span>}
                {run.scores_json?.some((a) => a.verdict === 'HARD_FAIL') && (
                  <span style={{ color: '#c62828', fontWeight: 700 }}>HARD FAIL</span>
                )}
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* Detail pane */}
      <main className="chat-pane" style={{ background: '#f7f8f9' }}>
        {!expandedId && (
          <div className="chat-empty">Select a simulation run to review</div>
        )}

        {expandedId && (() => {
          const run = runs.find((r) => r.id === expandedId);
          if (!run) return null;
          const transcript = run.transcript_json;

          return (
            <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
              {/* Header */}
              <header className="chat-header">
                <div className="chat-header-main">
                  <h2>{run.persona_id} × {run.seed_label}</h2>
                  <span style={{ fontSize: '1.1rem', fontWeight: 800, color: scoreColor(run.weighted_score) }}>
                    {fmt(run.weighted_score)}
                  </span>
                  <span style={{
                    fontSize: '0.72rem', fontWeight: 700, padding: '0.2rem 0.5rem',
                    borderRadius: 4, background: BUCKET_COLOR[run.bucket] || '#667', color: '#fff',
                  }}>
                    {run.bucket.replace(/_/g, ' ')}
                  </span>
                  {run.sft_eligible && (
                    <span style={{ fontSize: '0.72rem', fontWeight: 700, color: '#2e7d32', background: '#e8f5e9', padding: '0.2rem 0.5rem', borderRadius: 4 }}>
                      SFT eligible
                    </span>
                  )}
                </div>
                <dl className="chat-header-facts">
                  <dt>Persona</dt><dd>{transcript?.persona_name || run.persona_id}</dd>
                  <dt>Turns</dt><dd>{transcript?.turn_count ?? '—'}</dd>
                  {run.git_sha && <><dt>SHA</dt><dd>{run.git_sha.slice(0, 7)}</dd></>}
                  <dt>Run</dt><dd style={{ fontSize: '0.7rem' }}>{run.run_id.slice(0, 8)}…</dd>
                  <dt>Date</dt><dd>{run.created_at.slice(0, 10)}</dd>
                </dl>
                {transcript && (
                  <div style={{ marginTop: '0.5rem', fontSize: '0.78rem', color: '#555' }}>
                    <strong>Pass criteria:</strong> {transcript.pass_criteria}
                  </div>
                )}
              </header>

              <div className="chat-pane-body" style={{ overflow: 'auto' }}>
                {/* Left: transcript */}
                <section className="conversation-section" style={{ minWidth: 0 }}>
                  <div className="section-head">
                    <h3>Transcript</h3>
                    {transcript && (
                      <p className="section-hint">must_not: {transcript.must_not}</p>
                    )}
                  </div>
                  <div className="chat-messages">
                    {transcript?.turns.map((t) => (
                      <div key={t.turn_number}>
                        <div className="bubble-row bubble-row--user">
                          <div className="bubble-speaker">User (turn {t.turn_number})</div>
                          <div className="bubble bubble--user">
                            <p className="bubble-text">{t.user_message}</p>
                          </div>
                        </div>
                        <div className="bubble-row bubble-row--lana">
                          <div className="bubble-speaker">
                            Lana
                            {t.intent_class && (
                              <span style={{ marginLeft: '0.5rem', color: '#8696a0', fontWeight: 400, textTransform: 'none' }}>
                                · {t.intent_class} ({t.intent_confidence != null ? (t.intent_confidence * 100).toFixed(0) : '?'}%)
                                {t.tool_called ? ` → ${t.tool_called}` : ''}
                              </span>
                            )}
                          </div>
                          <div className="bubble bubble--lana">
                            <p className="bubble-text">{t.lana_reply}</p>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>

                {/* Right: judge scores + HITL */}
                <section className="insights-section">
                  <div className="section-head">
                    <h3>Judge evaluation</h3>
                    <p className="section-hint">{run.judge_summary}</p>
                  </div>
                  <div className="insights-scroll">
                    {run.scores_json?.map((axis) => (
                      <div key={axis.axis} style={{
                        marginBottom: '0.85rem', padding: '0.75rem 0.9rem',
                        background: '#fff', borderRadius: 8,
                        borderLeft: `4px solid ${axis.verdict === 'PASS' ? '#2e7d32' : axis.verdict === 'SOFT_FAIL' ? '#e65100' : '#c62828'}`,
                        boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
                      }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.35rem' }}>
                          <span style={{ fontWeight: 700, fontSize: '0.82rem' }}>{AXIS_LABELS[axis.axis] || axis.axis}</span>
                          <span className={`sim-verdict ${VERDICT_CLASS[axis.verdict]}`}>
                            {axis.verdict.replace('_', ' ')} · {fmt(axis.score)}
                          </span>
                        </div>
                        <p style={{ margin: 0, fontSize: '0.82rem', color: '#444', lineHeight: 1.5 }}>{axis.reasoning}</p>
                      </div>
                    ))}

                    {/* HITL verdict panel */}
                    <div style={{ marginTop: '1rem', padding: '0.85rem', background: '#fff', borderRadius: 8, boxShadow: '0 1px 2px rgba(0,0,0,0.06)' }}>
                      <p style={{ margin: '0 0 0.5rem', fontSize: '0.75rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: '#128c7e' }}>
                        Your verdict
                      </p>

                      {run.tim_verdict && (
                        <p style={{ margin: '0 0 0.5rem', fontSize: '0.82rem' }}>
                          <strong style={{ color: run.tim_verdict === 'flag_bug' ? '#f59e0b' : undefined }}>
                            {run.tim_verdict === 'flag_bug' ? '🚩 flagged bug' : run.tim_verdict.replace('_', ' ')}
                          </strong>
                          {run.tim_note && <> · {run.tim_note}</>}
                        </p>
                      )}

                      {run.hitl_status !== 'reviewed' && (
                        <>
                          <textarea
                            placeholder="Note (optional) — recorded with verdict"
                            value={verdictTarget?.runId === run.run_id ? noteInput : ''}
                            onChange={(e) => {
                              setVerdictTarget({ runId: run.run_id, axis: 'overall' });
                              setNoteInput(e.target.value);
                            }}
                            style={{
                              width: '100%', padding: '0.5rem', fontSize: '0.82rem',
                              borderRadius: 6, border: '1px solid #ccc',
                              marginBottom: '0.5rem', resize: 'vertical', minHeight: 56,
                              fontFamily: 'inherit',
                            }}
                          />
                          <div style={{ marginBottom: '0.4rem', fontSize: '0.72rem', color: '#8696a0' }}>
                            <strong>Confirm</strong> — judge was right &nbsp;·&nbsp;
                            <strong>Flag bug</strong> — real Lana product bug, needs a fix PR &nbsp;·&nbsp;
                            <strong>False pos</strong> — judge over-penalised &nbsp;·&nbsp;
                            <strong>False neg</strong> — judge missed a real failure
                          </div>
                          <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => saveVerdict(run, 'confirm')}
                              style={{ flex: 1, padding: '0.5rem', borderRadius: 6, border: 'none', background: '#2e7d32', color: '#fff', fontWeight: 700, cursor: 'pointer', fontSize: '0.8rem' }}
                            >
                              Confirm
                            </button>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => saveVerdict(run, 'flag_bug')}
                              style={{ flex: 1, padding: '0.5rem', borderRadius: 6, border: 'none', background: '#f59e0b', color: '#fff', fontWeight: 700, cursor: 'pointer', fontSize: '0.8rem' }}
                            >
                              🚩 Flag bug
                            </button>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => saveVerdict(run, 'false_positive')}
                              style={{ flex: 1, padding: '0.5rem', borderRadius: 6, border: 'none', background: '#e65100', color: '#fff', fontWeight: 700, cursor: 'pointer', fontSize: '0.8rem' }}
                            >
                              False pos
                            </button>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => saveVerdict(run, 'false_negative')}
                              style={{ flex: 1, padding: '0.5rem', borderRadius: 6, border: 'none', background: '#c62828', color: '#fff', fontWeight: 700, cursor: 'pointer', fontSize: '0.8rem' }}
                            >
                              False neg
                            </button>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => skipRun(run)}
                              style={{ padding: '0.5rem 0.75rem', borderRadius: 6, border: '1px solid #ccc', background: '#fff', color: '#667', cursor: 'pointer', fontSize: '0.8rem' }}
                            >
                              Skip
                            </button>
                          </div>
                        </>
                      )}

                      {run.hitl_status === 'reviewed' && (
                        <p style={{ margin: 0, fontSize: '0.78rem', color: '#128c7e' }}>Reviewed ✓</p>
                      )}
                    </div>
                  </div>
                </section>
              </div>
            </div>
          );
        })()}
      </main>
    </div>
  );
}
