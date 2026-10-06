import React, { useState } from 'react';
import { X, KeyRound, Check, ExternalLink, ShieldCheck } from 'lucide-react';
import { getStoredAnonKey, setStoredAnonKey, SUPABASE_URL } from '../lib/supabase';

interface ApiKeyModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export const ApiKeyModal: React.FC<ApiKeyModalProps> = ({ isOpen, onClose, onSaved }) => {
  const [key, setKey] = useState(getStoredAnonKey() === 'your_anon_key_here' ? '' : getStoredAnonKey());
  const [savedSuccess, setSavedSuccess] = useState(false);

  if (!isOpen) return null;

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    setStoredAnonKey(key.trim());
    setSavedSuccess(true);
    setTimeout(() => {
      setSavedSuccess(false);
      onSaved();
      onClose();
    }, 800);
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-group">
            <KeyRound size={20} className="text-indigo-400" />
            <h3 className="modal-title">Supabase Connection Settings</h3>
          </div>
          <button onClick={onClose} className="modal-close-btn" type="button">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSave} className="modal-body">
          <p className="modal-desc">
            To enable <strong>instant Real-Time WebSocket streaming</strong> directly from Supabase, enter your project&apos;s <code>anon public</code> key.
          </p>

          <div className="form-group">
            <label className="form-label">Supabase Project URL</label>
            <input
              type="text"
              readOnly
              value={SUPABASE_URL}
              className="form-input form-input-readonly"
            />
          </div>

          <div className="form-group">
            <div className="label-with-link">
              <label className="form-label" htmlFor="anon-key-input">
                Supabase Anon Public Key
              </label>
              <a
                href="https://supabase.com/dashboard/project/vbqlvtxdzbsddmovwfpo/settings/api"
                target="_blank"
                rel="noopener noreferrer"
                className="external-guide-link"
              >
                <span>Find Key in Supabase</span>
                <ExternalLink size={12} />
              </a>
            </div>
            <textarea
              id="anon-key-input"
              rows={3}
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
              className="form-input form-textarea"
              required
            />
            <p className="form-help">
              <ShieldCheck size={14} className="inline mr-1 text-emerald-400" />
              The anon key is safe for client-side use and is stored only in your local browser storage.
            </p>
          </div>

          <div className="modal-footer">
            <button type="button" onClick={onClose} className="btn-cancel">
              Cancel
            </button>
            <button type="submit" className="btn-save">
              {savedSuccess ? (
                <>
                  <Check size={16} /> Saved!
                </>
              ) : (
                'Save & Connect'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
