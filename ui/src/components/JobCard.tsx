import React, { useState } from 'react';
import { ExternalLink, Building2, MapPin, Clock, Copy, Check, Sparkles } from 'lucide-react';
import type { Job } from '../types';
import { timeAgo, fullDate, freshnessTier } from '../lib/time';

interface JobCardProps {
  job: Job;
  isNew?: boolean;
}

const TIER_META = {
  hot: {
    label: '🔥 Hot (< 5m)',
    pillClass: 'tier-hot',
    glowClass: 'card-glow-hot',
  },
  warm: {
    label: '⚡ Warm (< 1h)',
    pillClass: 'tier-warm',
    glowClass: 'card-glow-warm',
  },
  fresh: {
    label: '✨ Today',
    pillClass: 'tier-fresh',
    glowClass: '',
  },
  old: {
    label: '📅 Earlier',
    pillClass: 'tier-old',
    glowClass: '',
  },
};

export const JobCard: React.FC<JobCardProps> = ({ job, isNew }) => {
  const [copied, setCopied] = useState(false);
  const tier = freshnessTier(job.first_seen ?? job.posted_at);
  const tierInfo = TIER_META[tier];
  const relativeTime = timeAgo(job.first_seen ?? job.posted_at);
  const formattedDate = fullDate(job.first_seen ?? job.posted_at);

  const handleCopy = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!job.url) return;
    try {
      await navigator.clipboard.writeText(job.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback
    }
  };

  return (
    <article className={`job-card ${tierInfo.glowClass} ${isNew ? 'job-card-flash' : ''}`}>
      {/* Newly arrived badge overlay */}
      {isNew && (
        <div className="new-alert-ribbon">
          <Sparkles size={14} className="animate-spin-slow" />
          <span>JUST DISCOVERED</span>
        </div>
      )}

      <div className="card-top-row">
        <div className="card-badges">
          <span className={`tier-badge ${tierInfo.pillClass}`}>
            {tierInfo.label}
          </span>
          <span className="time-badge" title={`Discovered: ${formattedDate}`}>
            <Clock size={13} />
            <span>{relativeTime}</span>
          </span>
        </div>

        <button
          onClick={handleCopy}
          className={`copy-btn ${copied ? 'copied' : ''}`}
          title="Copy direct job URL"
          type="button"
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>

      <div className="card-main">
        <h3 className="job-title" title={job.title || 'Untitled Role'}>
          {job.title || 'Untitled Role'}
        </h3>

        <div className="company-row">
          <div className="meta-item company-name">
            <Building2 size={16} className="meta-icon company-icon" />
            <span>{job.company || 'Confidential / Not specified'}</span>
          </div>

          {job.location && (
            <div className="meta-item">
              <MapPin size={15} className="meta-icon" />
              <span>{job.location}</span>
            </div>
          )}
        </div>
      </div>

      <div className="card-footer">
        <div className="exact-timestamp" title="Exact timestamp">
          <span>First tracked:</span> <strong>{formattedDate || 'Recent'}</strong>
        </div>

        {job.url ? (
          <a
            href={job.url}
            target="_blank"
            rel="noopener noreferrer"
            className="apply-btn"
          >
            <span>View Job</span>
            <ExternalLink size={14} />
          </a>
        ) : (
          <span className="no-url-label">No Link</span>
        )}
      </div>
    </article>
  );
};
