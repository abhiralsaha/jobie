import { useState, useEffect, useCallback, useRef } from 'react';
import { getSupabase } from '../lib/supabase';
import { playNotificationSound } from '../lib/sound';
import type { Job } from '../types';

interface UseJobsReturn {
  jobs: Job[];
  loading: boolean;
  error: string | null;
  newJobIds: Set<string>;
  isRealtimeConnected: boolean;
  refetch: () => Promise<void>;
  hasAnonKey: boolean;
}

export function useJobs(notifyEnabled: boolean = true): UseJobsReturn {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [newJobIds, setNewJobIds] = useState<Set<string>>(new Set());
  const [isRealtimeConnected, setIsRealtimeConnected] = useState<boolean>(false);
  const hasAnonKey = Boolean(getSupabase());
  
  // Track seen IDs to distinguish fresh arrivals from initial load
  const knownIdsRef = useRef<Set<string>>(new Set());
  const initialLoadDoneRef = useRef<boolean>(false);

  const triggerJobAlert = useCallback((job: Job) => {
    // Flash card in UI
    setNewJobIds((prev) => new Set([...prev, job.job_id]));
    setTimeout(() => {
      setNewJobIds((prev) => {
        const next = new Set(prev);
        next.delete(job.job_id);
        return next;
      });
    }, 12_000);

    // Audio chime
    playNotificationSound();

    // Browser Push Notification
    if (notifyEnabled && typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted') {
      try {
        const notif = new Notification(`🚀 Fresh Job: ${job.title ?? 'New Opportunity'}`, {
          body: `${job.company ? job.company + ' · ' : ''}Discovered just now on LinkedIn`,
          icon: 'https://static.licdn.com/aero-v1/sc/h/al2o9zrvru7aqj8e1x2rzsrca',
          tag: job.job_id,
        });

        if (job.url) {
          notif.onclick = () => {
            window.open(job.url!, '_blank');
            notif.close();
          };
        }
      } catch (e) {
        console.debug('Failed to display desktop notification:', e);
      }
    }
  }, [notifyEnabled]);

  const fetchJobs = useCallback(async () => {
    try {
      const supabase = getSupabase();
      let fetched: Job[] = [];

      if (supabase) {
        const { data, error: sbError } = await supabase
          .from('seen_jobs')
          .select('*')
          .order('first_seen', { ascending: false })
          .limit(500);

        if (sbError) {
          throw new Error(sbError.message);
        }
        fetched = (data as Job[]) ?? [];
      } else {
        // Fallback to local FastAPI backend if running
        const resp = await fetch('/api/jobs');
        if (!resp.ok) {
          throw new Error(`API returned status ${resp.status}`);
        }
        const data = await resp.json();
        fetched = (data.jobs as Job[]) ?? [];
      }

      // Check for incoming new jobs if already loaded
      if (initialLoadDoneRef.current && knownIdsRef.current.size > 0) {
        for (const job of fetched) {
          if (!knownIdsRef.current.has(job.job_id)) {
            triggerJobAlert(job);
          }
        }
      }

      // Update known IDs
      const newKnown = new Set<string>();
      for (const j of fetched) {
        newKnown.add(j.job_id);
      }
      knownIdsRef.current = newKnown;
      initialLoadDoneRef.current = true;

      setJobs(fetched);
      setError(null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to fetch jobs';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [triggerJobAlert]);

  useEffect(() => {
    fetchJobs();

    const supabase = getSupabase();
    if (supabase) {
      const channel = supabase
        .channel('seen_jobs_stream')
        .on(
          'postgres_changes',
          { event: 'INSERT', schema: 'public', table: 'seen_jobs' },
          (payload) => {
            const newJob = payload.new as Job;
            if (!knownIdsRef.current.has(newJob.job_id)) {
              knownIdsRef.current.add(newJob.job_id);
              setJobs((prev) => [newJob, ...prev.filter((j) => j.job_id !== newJob.job_id)]);
              triggerJobAlert(newJob);
            }
          }
        )
        .on(
          'postgres_changes',
          { event: 'UPDATE', schema: 'public', table: 'seen_jobs' },
          () => {
            fetchJobs();
          }
        )
        .subscribe((status) => {
          setIsRealtimeConnected(status === 'SUBSCRIBED');
        });

      return () => {
        supabase.removeChannel(channel);
        setIsRealtimeConnected(false);
      };
    } else {
      setIsRealtimeConnected(false);
    }
  }, [fetchJobs, triggerJobAlert]);

  // Periodic refresh interval every 30 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      fetchJobs();
    }, 30_000);
    return () => clearInterval(interval);
  }, [fetchJobs]);

  return {
    jobs,
    loading,
    error,
    newJobIds,
    isRealtimeConnected,
    refetch: fetchJobs,
    hasAnonKey,
  };
}
