'use client';

import { BUCKET_STYLES, normalizeBucket } from '@/lib/buckets';
import type { RapportGap } from '@/lib/supabase';

// Rapport gaps = the follow-up questions Lana generated for this user, with their "why":
// the source message that triggered each, and the claim its answer produced.
export function RapportCards({ gaps }: { gaps: RapportGap[] }) {
  if (!gaps.length) return null;

  return (
    <section className="claims-panel">
      <h4>Rapport gaps ({gaps.length})</h4>
      <p className="claims-intro">
        Follow-up questions Lana generated — what prompted each, and what the answer became.
      </p>
      {gaps.map((g) => {
        const b = normalizeBucket(g.parent_bucket);
        const s = BUCKET_STYLES[b];
        return (
          <article
            key={g.gap_id}
            className="claim-card"
            style={{ borderLeftColor: s.border }}
          >
            <div className="claim-head">
              <strong>{g.question || g.why_frame || g.gap_id}</strong>
              <span className="match-pct">{g.status}</span>
            </div>
            {g.source_message && (
              <p className="claim-from">
                Prompted by &ldquo;<em>{g.source_message}</em>&rdquo;
              </p>
            )}
            {g.answer_label && (
              <p className="claim-from">
                Answered → <strong>{g.answer_label}</strong>
              </p>
            )}
          </article>
        );
      })}
    </section>
  );
}
