import { createClient, SupabaseClient } from '@supabase/supabase-js';

const defaultUrl = (import.meta.env.VITE_SUPABASE_URL as string) || 'https://vbqlvtxdzbsddmovwfpo.supabase.co';
const envKey = (import.meta.env.VITE_SUPABASE_ANON_KEY as string) || '';

export function getStoredAnonKey(): string {
  if (typeof window !== 'undefined') {
    const local = localStorage.getItem('supabase_anon_key');
    if (local && local.trim() !== '') return local.trim();
  }
  return envKey.trim();
}

export function setStoredAnonKey(key: string): void {
  if (typeof window !== 'undefined') {
    if (key.trim()) {
      localStorage.setItem('supabase_anon_key', key.trim());
    } else {
      localStorage.removeItem('supabase_anon_key');
    }
  }
}

let clientInstance: SupabaseClient | null = null;
let currentKey = '';

export function getSupabase(): SupabaseClient | null {
  const activeKey = getStoredAnonKey();
  if (!activeKey || activeKey === 'your_anon_key_here') {
    return null;
  }
  if (!clientInstance || currentKey !== activeKey) {
    currentKey = activeKey;
    clientInstance = createClient(defaultUrl, activeKey, {
      realtime: { params: { eventsPerSecond: 10 } },
    });
  }
  return clientInstance;
}

export const SUPABASE_URL = defaultUrl;
