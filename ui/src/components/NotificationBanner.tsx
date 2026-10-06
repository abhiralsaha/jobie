import React, { useState } from 'react';
import { Bell, X, ShieldAlert } from 'lucide-react';

interface NotificationBannerProps {
  permission: NotificationPermission;
  onRequest: () => void;
}

export const NotificationBanner: React.FC<NotificationBannerProps> = ({ permission, onRequest }) => {
  const [dismissed, setDismissed] = useState(false);

  if (permission === 'granted' || dismissed) {
    return null;
  }

  return (
    <div className="notification-banner">
      <div className="banner-content">
        <div className="banner-icon-box">
          {permission === 'denied' ? <ShieldAlert size={20} className="text-amber-400" /> : <Bell size={20} className="text-cyan-400 animate-wiggle" />}
        </div>
        <div className="banner-text">
          <strong>
            {permission === 'denied'
              ? 'Desktop notifications are blocked in your browser'
              : 'Enable Instant Desktop Notifications'}
          </strong>
          <span>
            {permission === 'denied'
              ? 'Click the padlock icon next to your URL bar to allow notifications and get alerted the second a fresh job drops.'
              : 'Get notified in real-time as soon as new LinkedIn jobs are detected, even when this tab is in the background.'}
          </span>
        </div>
      </div>

      <div className="banner-actions">
        {permission !== 'denied' && (
          <button onClick={onRequest} className="banner-enable-btn" type="button">
            Enable Now
          </button>
        )}
        <button
          onClick={() => setDismissed(true)}
          className="banner-close-btn"
          title="Dismiss banner"
          type="button"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  );
};
