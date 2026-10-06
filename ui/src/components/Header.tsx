import React from 'react';
import { Radio, Search, X, RefreshCw, Bell, BellOff, Volume2, KeyRound } from 'lucide-react';
import { playNotificationSound } from '../lib/sound';

interface HeaderProps {
  searchQuery: string;
  onSearchChange: (q: string) => void;
  isRealtime: boolean;
  loading: boolean;
  onRefresh: () => void;
  notificationPerm: NotificationPermission;
  onRequestNotification: () => void;
  onOpenKeyModal: () => void;
  hasAnonKey: boolean;
}

export const Header: React.FC<HeaderProps> = ({
  searchQuery,
  onSearchChange,
  isRealtime,
  loading,
  onRefresh,
  notificationPerm,
  onRequestNotification,
  onOpenKeyModal,
  hasAnonKey,
}) => {
  const handleTestNotification = () => {
    playNotificationSound();
    if (notificationPerm === 'granted') {
      new Notification('🔔 Notification Test Working!', {
        body: 'You will receive immediate alerts whenever LinkedIn drops fresh roles.',
        icon: 'https://static.licdn.com/aero-v1/sc/h/al2o9zrvru7aqj8e1x2rzsrca',
      });
    } else {
      onRequestNotification();
    }
  };

  return (
    <header className="site-header">
      <div className="header-top">
        <div className="brand-group">
          <div className="brand-icon-wrapper">
            <Radio size={24} className="radar-icon-pulse text-indigo-400" />
            <span className="radar-wave"></span>
          </div>
          <div className="brand-text">
            <div className="brand-title-row">
              <h1 className="brand-name">JOBIE</h1>
              <span className="brand-badge">LIVE RADAR</span>
            </div>
            <p className="brand-tagline">Real-time LinkedIn Fresh Job Stream & Alerts</p>
          </div>
        </div>

        <div className="header-controls">
          {/* Connection Status Indicator */}
          <div
            className={`status-pill ${isRealtime ? 'status-live' : 'status-poll'}`}
            title={isRealtime ? 'Supabase Real-time stream active' : 'Polling mode active'}
          >
            <span className="status-dot"></span>
            <span>{isRealtime ? 'LIVE STREAM' : 'AUTO-POLL (30s)'}</span>
          </div>

          {/* Test Sound Button */}
          <button
            onClick={handleTestNotification}
            className="action-btn text-btn"
            title="Test notification sound & desktop notification"
            type="button"
          >
            <Volume2 size={16} />
            <span className="hidden-mobile">Test Alert</span>
          </button>

          {/* Desktop Notification Toggle */}
          <button
            onClick={onRequestNotification}
            className={`action-btn notif-btn ${
              notificationPerm === 'granted'
                ? 'notif-granted'
                : notificationPerm === 'denied'
                ? 'notif-denied'
                : 'notif-default'
            }`}
            title={`Notifications: ${notificationPerm}`}
            type="button"
          >
            {notificationPerm === 'granted' ? (
              <>
                <Bell size={16} />
                <span className="hidden-mobile">Alerts On</span>
              </>
            ) : notificationPerm === 'denied' ? (
              <>
                <BellOff size={16} />
                <span className="hidden-mobile">Blocked</span>
              </>
            ) : (
              <>
                <Bell size={16} />
                <span className="hidden-mobile">Enable Alerts</span>
              </>
            )}
          </button>

          {/* Supabase Key modal trigger */}
          <button
            onClick={onOpenKeyModal}
            className={`action-btn key-btn ${!hasAnonKey ? 'key-btn-warning' : ''}`}
            title="Configure Supabase Anon Key"
            type="button"
          >
            <KeyRound size={16} />
            <span className="hidden-mobile">{hasAnonKey ? 'Supabase' : 'Setup Key'}</span>
          </button>

          {/* Manual Refresh */}
          <button
            onClick={onRefresh}
            disabled={loading}
            className="action-btn icon-btn refresh-btn"
            title="Force refresh now"
            type="button"
          >
            <RefreshCw size={17} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Header Search Bar */}
      <div className="search-bar-wrapper">
        <div className="search-input-box">
          <Search size={18} className="search-icon" />
          <input
            type="text"
            placeholder="Search by job title, company name, tech stack or keywords..."
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            className="search-input"
          />
          {searchQuery && (
            <button
              onClick={() => onSearchChange('')}
              className="search-clear-btn"
              title="Clear search"
              type="button"
            >
              <X size={15} />
            </button>
          )}
        </div>
      </div>
    </header>
  );
};
