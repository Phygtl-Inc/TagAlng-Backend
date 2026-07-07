import { createClient } from '@supabase/supabase-js';
import type { LanaUi } from './buckets';

export type { LanaUi };

const url = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

export function getSupabase() {
  if (!url || !anon) {
    throw new Error(
      'Missing Supabase env. Copy apps/admin/.env.example to .env.local and set NEXT_PUBLIC_SUPABASE_ANON_KEY ' +
        '(Dashboard → Project Settings → API → anon public). Restart: npm run dev'
    );
  }
  return createClient(url, anon);
}

export type InboxSession = {
  session_id: string;
  user_id: string;
  phone: string | null;
  nickname: string | null;
  purpose: string;
  status: string;
  message_count: number;
  last_message_at: string | null;
  last_message_preview: string | null;
  last_role: string | null;
  created_at: string;
};

export type ConversationMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  ui?: LanaUi | null;
  created_at: string;
};

export type ConversationClaim = {
  label: string;
  confidence: number;
  source_quote: string | null;
  bucket: string | null;
  synonyms: string[];
};

export type RapportGap = {
  gap_id: string;
  question: string | null;
  why_frame: string | null;
  parent_bucket: string | null;
  status: string;
  unlock_score: number | null;
  skipped_count: number | null;
  opened_at: string | null;
  asked_at: string | null;
  answered_at: string | null;
  /** The user message that triggered this gap (the "why"), if recorded. */
  source_message: string | null;
  /** What the answer became, once answered. */
  answer_label: string | null;
  answer_concept: string | null;
};

export type Conversation = {
  session: {
    id: string;
    status: string;
    context?: {
      mapped_summary?: string;
      spans?: { text: string; bucket?: string; claim_concept?: string }[];
    };
  };
  user: {
    phone: string | null;
    nickname: string | null;
    home_block_id: string | null;
    home_zip: string | null;
  };
  messages: ConversationMessage[];
  claims: ConversationClaim[];
};
