import { useCallback, useEffect, useState } from 'react';

export interface SearchLogJob {
  job_id: string;
  title: string | null;
  company: string | null;
  url: string;
}

export interface SearchLog {
  id: number;
  started_at: string;
  completed_at: string;
  status: 'success' | 'failed';
  matched_count: number;
  new_job_count: number;
  new_jobs: SearchLogJob[];
  error: string | null;
}

interface SearchLogsResponse {
  logs: SearchLog[];
  has_more: boolean;
}

export function useSearchLogs() {
  const [logs, setLogs] = useState<SearchLog[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(async (offset: number, append: boolean) => {
    try {
      const response = await fetch(`/api/search-logs?limit=25&offset=${offset}`);
      if (!response.ok) {
        throw new Error(`Search history API returned status ${response.status}`);
      }
      const data = (await response.json()) as SearchLogsResponse;
      setLogs((current) => {
        const merged = append ? [...current, ...data.logs] : [...data.logs, ...current];
        return [...new Map(merged.map((entry) => [entry.id, entry])).values()]
          .sort((a, b) => b.id - a.id);
      });
      setHasMore(data.has_more);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load search history');
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    const initialFetch = setTimeout(() => void fetchPage(0, false), 0);
    const interval = setInterval(() => void fetchPage(0, false), 30_000);
    return () => {
      clearTimeout(initialFetch);
      clearInterval(interval);
    };
  }, [fetchPage]);

  const loadMore = useCallback(() => {
    if (!loadingMore && hasMore) {
      setLoadingMore(true);
      void fetchPage(logs.length, true);
    }
  }, [fetchPage, hasMore, loadingMore, logs.length]);

  return { logs, hasMore, loading, loadingMore, error, loadMore };
}
