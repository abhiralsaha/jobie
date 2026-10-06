import React from 'react';
import { Activity, CheckCircle2, ChevronDown, Clock3, ExternalLink, XCircle } from 'lucide-react';
import { timeAgo, fullDate } from '../lib/time';
import type { SearchLog } from '../hooks/useSearchLogs';

interface SearchHistoryProps {
  logs: SearchLog[];
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  error: string | null;
  onLoadMore: () => void;
}

export const SearchHistory: React.FC<SearchHistoryProps> = ({
  logs,
  loading,
  loadingMore,
  hasMore,
  error,
  onLoadMore,
}) => (
  <section className="search-history" aria-labelledby="search-history-title">
    <div className="search-history-header">
      <div className="search-history-heading">
        <Activity size={18} />
        <div>
          <h2 id="search-history-title">Search history</h2>
          <p>Every automatic LinkedIn search, newest first</p>
        </div>
      </div>
      {logs.length > 0 && <span className="search-history-count">{logs.length} shown</span>}
    </div>

    {error && <p className="search-history-error">{error}</p>}
    {loading && logs.length === 0 ? (
      <p className="search-history-empty">Loading search history…</p>
    ) : logs.length === 0 ? (
      <p className="search-history-empty">No search runs recorded yet.</p>
    ) : (
      <div className="search-history-list">
        {logs.map((entry) => (
          <article className="search-history-entry" key={entry.id}>
            <div className={`search-history-status ${entry.status}`}>
              {entry.status === 'success' ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
            </div>
            <div className="search-history-body">
              <div className="search-history-entry-top">
                <strong>{entry.status === 'success' ? 'Search completed' : 'Search failed'}</strong>
                <time dateTime={entry.started_at} title={fullDate(entry.started_at)}>
                  <Clock3 size={13} />
                  {timeAgo(entry.started_at)}
                </time>
              </div>
              {entry.status === 'success' ? (
                <p className="search-history-summary">
                  Checked {entry.matched_count} matching {entry.matched_count === 1 ? 'job' : 'jobs'}
                  {' · '}{entry.new_job_count} new
                </p>
              ) : (
                <p className="search-history-summary search-history-failure">
                  Search error: {entry.error ?? 'Unknown error'}
                </p>
              )}
              {entry.new_jobs.length > 0 && (
                <ul className="search-history-jobs">
                  {entry.new_jobs.map((job) => (
                    <li key={job.job_id}>
                      {job.url ? (
                        <a href={job.url} target="_blank" rel="noreferrer">
                          <span>{job.title || 'Untitled role'}</span>
                          <ExternalLink size={13} />
                        </a>
                      ) : (
                        <span>{job.title || 'Untitled role'}</span>
                      )}
                      <span className="search-history-company">{job.company || 'Unknown company'}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </article>
        ))}
      </div>
    )}

    {hasMore && (
      <button
        className="search-history-more"
        type="button"
        onClick={onLoadMore}
        disabled={loadingMore}
      >
        <ChevronDown size={15} />
        {loadingMore ? 'Loading…' : 'Load older searches'}
      </button>
    )}
  </section>
);
