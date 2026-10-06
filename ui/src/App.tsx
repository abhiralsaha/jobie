import React, { useState, useMemo, useEffect, useCallback } from 'react';
import { useJobs } from './hooks/useJobs';
import { ageSeconds } from './lib/time';
import { Header } from './components/Header';
import { StatsBar } from './components/StatsBar';
import { JobCard } from './components/JobCard';
import { NotificationBanner } from './components/NotificationBanner';
import { ApiKeyModal } from './components/ApiKeyModal';
import { EmptyState } from './components/EmptyState';
import type { TimeFilter, SortOption } from './types';
import { AlertCircle, RefreshCw, Radio } from 'lucide-react';

export const App: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilter, setActiveFilter] = useState<TimeFilter>('all');
  const [activeSort, setActiveSort] = useState<SortOption>('newest');
  const [isKeyModalOpen, setIsKeyModalOpen] = useState(false);
  const [notificationPerm, setNotificationPerm] = useState<NotificationPermission>('default');

  // Check initial notification permission
  useEffect(() => {
    if (typeof window !== 'undefined' && 'Notification' in window) {
      setNotificationPerm(Notification.permission);
    }
  }, []);

  const requestNotificationPermission = useCallback(async () => {
    if (typeof window !== 'undefined' && 'Notification' in window) {
      try {
        const perm = await Notification.requestPermission();
        setNotificationPerm(perm);
      } catch (err) {
        console.error('Failed to request notification permission:', err);
      }
    }
  }, []);

  const {
    jobs,
    loading,
    error,
    newJobIds,
    isRealtimeConnected,
    refetch,
    hasAnonKey,
  } = useJobs(notificationPerm === 'granted');

  // Force re-render of relative times every 15 seconds
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((t) => t + 1), 15_000);
    return () => clearInterval(timer);
  }, []);

  // Compute metrics across all jobs
  const { hotCount, warmCount, dayCount } = useMemo(() => {
    let hot = 0;
    let warm = 0;
    let day = 0;

    for (const j of jobs) {
      const s = ageSeconds(j.first_seen ?? j.posted_at);
      if (s < 300) hot++;
      if (s < 3600) warm++;
      if (s < 86400) day++;
    }

    return { hotCount: hot, warmCount: warm, dayCount: day };
  }, [jobs]);

  // Filter & Sort jobs
  const filteredJobs = useMemo(() => {
    let list = [...jobs];

    // Search filter
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      list = list.filter((j) => {
        const title = (j.title || '').toLowerCase();
        const company = (j.company || '').toLowerCase();
        const loc = (j.location || '').toLowerCase();
        return title.includes(q) || company.includes(q) || loc.includes(q);
      });
    }

    // Time window filter
    if (activeFilter !== 'all') {
      list = list.filter((j) => {
        const s = ageSeconds(j.first_seen ?? j.posted_at);
        if (activeFilter === '5m') return s < 300;
        if (activeFilter === '1h') return s < 3600;
        if (activeFilter === '24h') return s < 86400;
        if (activeFilter === '7d') return s < 86400 * 7;
        return true;
      });
    }

    // Sort order
    list.sort((a, b) => {
      if (activeSort === 'newest') {
        const tA = typeof a.first_seen === 'number' ? a.first_seen : 0;
        const tB = typeof b.first_seen === 'number' ? b.first_seen : 0;
        return tB - tA;
      }
      if (activeSort === 'oldest') {
        const tA = typeof a.first_seen === 'number' ? a.first_seen : 0;
        const tB = typeof b.first_seen === 'number' ? b.first_seen : 0;
        return tA - tB;
      }
      if (activeSort === 'company') {
        return (a.company || '').localeCompare(b.company || '');
      }
      return 0;
    });

    return list;
  }, [jobs, searchQuery, activeFilter, activeSort]);

  const hasActiveFilters = searchQuery.trim() !== '' || activeFilter !== 'all';

  return (
    <div className="jobie-app-container">
      {/* Background ambient lighting */}
      <div className="ambient-glow glow-top-left" />
      <div className="ambient-glow glow-top-right" />

      {/* Main Top Header */}
      <Header
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        isRealtime={isRealtimeConnected}
        loading={loading}
        onRefresh={refetch}
        notificationPerm={notificationPerm}
        onRequestNotification={requestNotificationPermission}
        onOpenKeyModal={() => setIsKeyModalOpen(true)}
        hasAnonKey={hasAnonKey}
      />

      <main className="main-content">
        {/* Permission Callout Banner */}
        <NotificationBanner
          permission={notificationPerm}
          onRequest={requestNotificationPermission}
        />

        {/* Global Stats & Filter Toolbar */}
        <StatsBar
          totalCount={jobs.length}
          hotCount={hotCount}
          warmCount={warmCount}
          dayCount={dayCount}
          activeFilter={activeFilter}
          onFilterChange={setActiveFilter}
          activeSort={activeSort}
          onSortChange={setActiveSort}
        />

        {/* Error notification if backend / supabase fails */}
        {error && (
          <div className="error-banner">
            <AlertCircle size={20} className="error-icon" />
            <div className="error-text">
              <strong>Database Connection Notice:</strong> {error}
            </div>
            <button onClick={() => refetch()} className="error-retry-btn" type="button">
              <RefreshCw size={14} /> Retry
            </button>
          </div>
        )}

        {/* Results Header Info */}
        <div className="results-header">
          <div className="results-count">
            Showing <strong>{filteredJobs.length}</strong> {filteredJobs.length === 1 ? 'job' : 'jobs'}
            {hasActiveFilters && <span className="results-filter-indicator"> (filtered)</span>}
          </div>
        </div>

        {/* Job Cards Stream */}
        {loading && jobs.length === 0 ? (
          <div className="loading-grid">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <div key={i} className="job-card-skeleton">
                <div className="skeleton-row-top" />
                <div className="skeleton-title" />
                <div className="skeleton-company" />
                <div className="skeleton-footer" />
              </div>
            ))}
          </div>
        ) : filteredJobs.length === 0 ? (
          <EmptyState
            hasFilters={hasActiveFilters}
            onResetFilters={() => {
              setSearchQuery('');
              setActiveFilter('all');
            }}
          />
        ) : (
          <div className="jobs-grid">
            {filteredJobs.map((job) => (
              <JobCard
                key={job.job_id}
                job={job}
                isNew={newJobIds.has(job.job_id)}
              />
            ))}
          </div>
        )}
      </main>

      <footer className="site-footer">
        <div className="footer-content">
          <div className="footer-left">
            <Radio size={16} className="text-indigo-400 inline mr-2" />
            <span>Jobie Live Radar · Tracking LinkedIn fresh openings</span>
          </div>
          <div className="footer-right">
            <span>Real-time Supabase PostgreSQL Stream</span>
          </div>
        </div>
      </footer>

      {/* Supabase Key Modal */}
      <ApiKeyModal
        isOpen={isKeyModalOpen}
        onClose={() => setIsKeyModalOpen(false)}
        onSaved={refetch}
      />
    </div>
  );
};

export default App;
