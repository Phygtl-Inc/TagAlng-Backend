export type Bucket =
  | 'heritage'
  | 'stage'
  | 'vicinity'
  | 'faith'
  | 'activity'
  | 'interest'
  | 'general';

export const BUCKET_STYLES: Record<
  Bucket,
  { label: string; pill: string; border: string; highlight: string }
> = {
  heritage: {
    label: 'HERITAGE',
    pill: '#b8860b',
    border: '#e8c547',
    highlight: 'rgba(232, 197, 71, 0.35)',
  },
  stage: {
    label: 'STAGE',
    pill: '#c45c3e',
    border: '#e8a090',
    highlight: 'rgba(232, 160, 144, 0.35)',
  },
  vicinity: {
    label: 'VICINITY',
    pill: '#2d6a4f',
    border: '#95d5b2',
    highlight: 'rgba(149, 213, 178, 0.35)',
  },
  faith: {
    label: 'FAITH',
    pill: '#1d4e89',
    border: '#90caf9',
    highlight: 'rgba(144, 202, 249, 0.35)',
  },
  activity: {
    label: 'ACTIVITY',
    pill: '#00796b',
    border: '#80cbc4',
    highlight: 'rgba(128, 203, 196, 0.35)',
  },
  interest: {
    label: 'INTEREST',
    pill: '#ad1457',
    border: '#f48fb1',
    highlight: 'rgba(244, 143, 177, 0.35)',
  },
  general: {
    label: 'GENERAL',
    pill: '#616161',
    border: '#bdbdbd',
    highlight: 'rgba(189, 189, 189, 0.35)',
  },
};

export function normalizeBucket(raw: string | null | undefined): Bucket {
  const b = (raw || 'general').toLowerCase() as Bucket;
  return b in BUCKET_STYLES ? b : 'general';
}

export type LanaUi = {
  bucket?: string | null;
  focus_phrase?: string | null;
  highlights?: { text: string; bucket?: string }[];
};
