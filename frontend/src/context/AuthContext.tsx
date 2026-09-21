import React, { createContext, useContext, useEffect, useState, useCallback, useRef } from "react";
import { authApi, setAccessToken } from "@/lib/api";

interface User {
  id: number;
  username: string;
  totp_enabled: boolean;
  is_admin: boolean;
  audible_account_id: string | null;
  download_cap: number | null;
  permissions: Record<string, boolean> | null;
}

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  // The id of this browser's own session row, returned by login/refresh. SettingsPage uses it to mark
  // the "This device" row by id, so the badge works even when the browser withholds the refresh_token
  // cookie from GET /auth/sessions (e.g. hardened Brave profiles).
  currentSessionId: number | null;
  login: (accessToken: string, user: User, sessionId?: number | null) => void;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const ACCESS_TOKEN_REFRESH_MS = 13 * 60 * 1000; // refresh 2 min before 15-min expiry

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) clearInterval(refreshTimer.current);
    refreshTimer.current = setInterval(async () => {
      try {
        const { data } = await authApi.refresh();
        setAccessToken(data.access_token);
        if (data.session_id != null) setCurrentSessionId(data.session_id);
      } catch {
        setUser(null);
        setAccessToken(null);
        setCurrentSessionId(null);
      }
    }, ACCESS_TOKEN_REFRESH_MS);
  }, []);

  const login = useCallback((accessToken: string, userData: User, sessionId?: number | null) => {
    setAccessToken(accessToken);
    setUser(userData);
    setCurrentSessionId(sessionId ?? null);
    scheduleRefresh();
    // The re-authentication reminder (ReauthBanner) is dismissable per login, so its dismissal
    // is cleared here rather than in logout(): session expiry nulls the user without calling
    // logout(), and the silent refresh on mount does not call login(), so a reload keeps it.
    try { sessionStorage.removeItem(`reauth-dismissed:${userData.id}`); } catch { /* private mode etc. */ }
  }, [scheduleRefresh]);

  const logout = useCallback(async () => {
    try { await authApi.logout(); } catch { /* ignore */ }
    setAccessToken(null);
    setUser(null);
    setCurrentSessionId(null);
    if (refreshTimer.current) clearInterval(refreshTimer.current);
  }, []);

  const refreshUser = useCallback(async () => {
    const { data } = await authApi.me();
    setUser(data);
  }, []);

  // On mount: attempt silent token refresh using the httpOnly cookie
  useEffect(() => {
    authApi.refresh()
      .then(({ data }) => {
        setAccessToken(data.access_token);
        setUser(data.user);
        setCurrentSessionId(data.session_id ?? null);
        scheduleRefresh();
      })
      .catch(() => { /* not logged in */ })
      .finally(() => setIsLoading(false));

    return () => { if (refreshTimer.current) clearInterval(refreshTimer.current); };
  }, [scheduleRefresh]);

  return (
    <AuthContext.Provider value={{ user, isLoading, isAuthenticated: !!user, currentSessionId, login, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
