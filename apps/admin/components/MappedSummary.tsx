'use client';

import { BUCKET_STYLES, normalizeBucket } from '@/lib/buckets';

type Span = { text: string; bucket?: string; claim_concept?: string };

export function MappedSummary({
  summary,
  spans,
}: {
  summary?: string;
  spans?: Span[];
}) {
  if (!summary && (!spans || spans.length === 0)) return null;

  return (
    <section className="mapped-panel">
      <h4>Summary sentence</h4>
      <p className="mapped-line">
        {spans && spans.length > 0
          ? spans.map((sp, i) => {
              const b = normalizeBucket(sp.bucket);
              const s = BUCKET_STYLES[b];
              return (
                <span key={sp.text + (sp.claim_concept || '') + i}>
                  {i > 0 ? ', ' : null}
                  <span className="mapped-span" style={{ background: s.highlight }}>
                    {sp.text}
                  </span>
                </span>
              );
            })
          : summary}
      </p>
    </section>
  );
}
