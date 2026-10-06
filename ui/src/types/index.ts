export interface Job {
  job_id: string;
  title: string | null;
  company: string | null;
  location?: string | null;
  first_seen: number | null;   // unix epoch seconds
  posted_at: string | null;    // ISO string or formatted string
  url: string | null;
}

export type FreshnessTier = 'hot' | 'warm' | 'fresh' | 'old';

export type TimeFilter = 'all' | '5m' | '1h' | '24h' | '7d';

export type SortOption = 'newest' | 'oldest' | 'company';
