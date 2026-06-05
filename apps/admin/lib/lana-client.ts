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

export type LanaSession = {
  session_id: string;
  purpose: string;
  status: string;
  assistant_message: string;
  ready_to_complete: boolean;
  ui?: LanaUi | null;
  orchestrator?: boolean;
};

export type LanaTurn = {
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

export function startProfileSession(token: string) {
  return lanaFetch<LanaSession>('/lana/sessions', token, {
    method: 'POST',
    body: JSON.stringify({ purpose: 'profile_intake' }),
  });
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
