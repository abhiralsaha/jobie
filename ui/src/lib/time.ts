import type { FreshnessTier } from '../types';

/** Returns a human-readable relative time: "2m ago", "1h ago", "3d ago" */
export function timeAgo(value: number | string | null | undefined): string {
  if (value == null || value === '') return 'Unknown';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return 'Unknown';

  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 0) return 'Just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

/** Returns a full formatted date/time string in IST */
export function fullDate(value: number | string | null | undefined): string {
  if (value == null || value === '') return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return date.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
    timeZone: 'Asia/Kolkata',
  });
}

/** Returns the age in seconds of a value */
export function ageSeconds(value: number | string | null | undefined): number {
  if (value == null || value === '') return Infinity;
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return Infinity;
  return (Date.now() - date.getTime()) / 1000;
}

/** Returns a freshness tier for colour-coding */
export function freshnessTier(value: number | string | null | undefined): FreshnessTier {
  const s = ageSeconds(value);
  if (s < 300) return 'hot';       // < 5 min
  if (s < 3600) return 'warm';    // < 1 hr
  if (s < 86400) return 'fresh';  // < 24 hr
  return 'old';
}
