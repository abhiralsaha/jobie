import React from 'react';
import { SearchX, Radio, RotateCcw } from 'lucide-react';

interface EmptyStateProps {
  hasFilters: boolean;
  onResetFilters: () => void;
}

export const EmptyState: React.FC<EmptyStateProps> = ({ hasFilters, onResetFilters }) => {
  return (
    <div className="empty-state">
      <div className="empty-icon-halo">
        {hasFilters ? (
          <SearchX size={36} className="text-slate-400" />
        ) : (
          <Radio size={36} className="text-indigo-400 animate-pulse" />
        )}
      </div>

      <h3 className="empty-title">
        {hasFilters ? 'No jobs match your filter criteria' : 'Waiting for incoming LinkedIn jobs...'}
      </h3>

      <p className="empty-desc">
        {hasFilters
          ? 'Try adjusting your search terms or expanding the time window to view more postings.'
          : 'Jobie is actively polling LinkedIn guest job feeds in the background. As soon as recruiters post new positions, they will stream directly into this dashboard with instant audio and desktop alerts.'}
      </p>

      {hasFilters && (
        <button onClick={onResetFilters} className="empty-reset-btn" type="button">
          <RotateCcw size={15} />
          <span>Reset All Filters</span>
        </button>
      )}
    </div>
  );
};
