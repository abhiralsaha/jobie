import React from 'react';
import { Flame, Zap, Calendar, Layers, ArrowUpDown } from 'lucide-react';
import type { TimeFilter, SortOption } from '../types';

interface StatsBarProps {
  totalCount: number;
  hotCount: number;
  warmCount: number;
  dayCount: number;
  activeFilter: TimeFilter;
  onFilterChange: (filter: TimeFilter) => void;
  activeSort: SortOption;
  onSortChange: (sort: SortOption) => void;
}

export const StatsBar: React.FC<StatsBarProps> = ({
  totalCount,
  hotCount,
  warmCount,
  dayCount,
  activeFilter,
  onFilterChange,
  activeSort,
  onSortChange,
}) => {
  return (
    <div className="stats-section">
      {/* 4 Overview Metric Cards */}
      <div className="metrics-grid">
        <div
          className={`metric-card ${activeFilter === 'all' ? 'metric-card-selected' : ''}`}
          onClick={() => onFilterChange('all')}
          role="button"
          tabIndex={0}
        >
          <div className="metric-icon-box bg-slate-800 text-slate-300">
            <Layers size={18} />
          </div>
          <div className="metric-details">
            <span className="metric-value">{totalCount}</span>
            <span className="metric-label">Total Tracked</span>
          </div>
        </div>

        <div
          className={`metric-card card-hot ${activeFilter === '5m' ? 'metric-card-selected' : ''}`}
          onClick={() => onFilterChange('5m')}
          role="button"
          tabIndex={0}
        >
          <div className="metric-icon-box bg-rose-950/60 text-rose-400">
            <Flame size={18} className="animate-bounce-subtle" />
          </div>
          <div className="metric-details">
            <span className="metric-value text-rose-400">{hotCount}</span>
            <span className="metric-label">Hot (&lt; 5m)</span>
          </div>
        </div>

        <div
          className={`metric-card card-warm ${activeFilter === '1h' ? 'metric-card-selected' : ''}`}
          onClick={() => onFilterChange('1h')}
          role="button"
          tabIndex={0}
        >
          <div className="metric-icon-box bg-amber-950/60 text-amber-400">
            <Zap size={18} />
          </div>
          <div className="metric-details">
            <span className="metric-value text-amber-400">{warmCount}</span>
            <span className="metric-label">Warm (&lt; 1h)</span>
          </div>
        </div>

        <div
          className={`metric-card card-day ${activeFilter === '24h' ? 'metric-card-selected' : ''}`}
          onClick={() => onFilterChange('24h')}
          role="button"
          tabIndex={0}
        >
          <div className="metric-icon-box bg-emerald-950/60 text-emerald-400">
            <Calendar size={18} />
          </div>
          <div className="metric-details">
            <span className="metric-value text-emerald-400">{dayCount}</span>
            <span className="metric-label">Today (&lt; 24h)</span>
          </div>
        </div>
      </div>

      {/* Filter and Sort Toolbar */}
      <div className="filter-toolbar">
        <div className="filter-pills" role="tablist">
          <span className="filter-toolbar-label">Time Window:</span>
          
          <button
            type="button"
            className={`filter-pill ${activeFilter === 'all' ? 'active' : ''}`}
            onClick={() => onFilterChange('all')}
          >
            All Time ({totalCount})
          </button>

          <button
            type="button"
            className={`filter-pill pill-hot ${activeFilter === '5m' ? 'active' : ''}`}
            onClick={() => onFilterChange('5m')}
          >
            🔥 Last 5 Mins ({hotCount})
          </button>

          <button
            type="button"
            className={`filter-pill pill-warm ${activeFilter === '1h' ? 'active' : ''}`}
            onClick={() => onFilterChange('1h')}
          >
            ⚡ Last 1 Hour ({warmCount})
          </button>

          <button
            type="button"
            className={`filter-pill pill-day ${activeFilter === '24h' ? 'active' : ''}`}
            onClick={() => onFilterChange('24h')}
          >
            ✨ Last 24 Hours ({dayCount})
          </button>

          <button
            type="button"
            className={`filter-pill ${activeFilter === '7d' ? 'active' : ''}`}
            onClick={() => onFilterChange('7d')}
          >
            📅 Last 7 Days
          </button>
        </div>

        {/* Sort dropdown */}
        <div className="sort-box">
          <ArrowUpDown size={15} className="sort-icon" />
          <select
            value={activeSort}
            onChange={(e) => onSortChange(e.target.value as SortOption)}
            className="sort-select"
          >
            <option value="newest">Newest First</option>
            <option value="oldest">Oldest First</option>
            <option value="company">Company (A-Z)</option>
          </select>
        </div>
      </div>
    </div>
  );
};
