import axios from "axios";

export const api = axios.create({ baseURL: "/api" });

let _accessToken: string | null = null;

export function setAccessToken(token: string | null) {
  _accessToken = token;
}

export function getAccessToken() {
  return _accessToken;
}

// Attach access token to every request
api.interceptors.request.use((config) => {
  if (_accessToken) {
    config.headers.Authorization = `Bearer ${_accessToken}`;
  }
  return config;
});

// On 401, try silent refresh then retry once
api.interceptors.response.use(
  (r) => r,
  async (error) => {
    const original = error.config;
    if (error.response?.status === 401 && !original._retry && !original.url?.includes("/auth/")) {
      original._retry = true;
      try {
        const { data } = await axios.post("/api/auth/refresh", {}, { withCredentials: true });
        setAccessToken(data.access_token);
        original.headers.Authorization = `Bearer ${data.access_token}`;
        return api(original);
      } catch {
        setAccessToken(null);
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

// Auth API
export const authApi = {
  login: (username: string, password: string) =>
    api.post("/auth/login", { username, password }),

  verify2fa: (temp_token: string, code: string) =>
    api.post("/auth/verify-2fa", { temp_token, code }),

  refresh: () =>
    axios.post("/api/auth/refresh", {}, { withCredentials: true }),

  logout: () =>
    api.post("/auth/logout", {}, { withCredentials: true }),

  me: () =>
    api.get("/auth/me"),

  setup2fa: () =>
    api.post("/auth/setup-2fa"),

  enable2fa: (secret: string, code: string) =>
    api.post("/auth/enable-2fa-confirm", { secret, code }),

  disable2fa: (code: string) =>
    api.post("/auth/disable-2fa", { code }),

  changePassword: (current_password: string, new_password: string) =>
    api.post("/auth/change-password", { current_password, new_password }),

  changeUsername: (new_username: string, current_password: string) =>
    api.post("/auth/change-username", { new_username, current_password }),

  updateMe: (patch: { audible_account_id?: string }) =>
    api.patch("/auth/me", patch),

  listSessions: () =>
    api.get("/auth/sessions"),

  revokeSession: (id: number) =>
    api.delete(`/auth/sessions/${id}`),

  revokeAllSessions: () =>
    api.delete("/auth/sessions"),
};

// Users API (admin only)
export const usersApi = {
  list: () =>
    api.get("/users"),

  create: (username: string, password: string, is_admin: boolean) =>
    api.post("/users", { username, password, is_admin }),

  update: (id: number, patch: { is_active?: boolean; is_admin?: boolean; new_password?: string; owner_name?: string; audible_account_id?: string | null }) =>
    api.patch(`/users/${id}`, patch),

  updatePermissions: (id: number, patch: {
    can_download?: boolean;
    can_scan?: boolean;
    can_manage_accounts?: boolean;
    can_liberate?: boolean;
    can_remove_downloads?: boolean;
    download_cap?: number | null;
  }) =>
    api.patch(`/users/${id}/permissions`, patch),

  delete: (id: number) =>
    api.delete(`/users/${id}`),
};

// Accounts API
export interface Account {
  account_id: string;
  name: string | null;
  locale: string;
  scan_library: boolean;
  authenticated: boolean;
  owner_name: string | null;
  owner_username: string | null;
  auto_download: boolean;
  added_by_user_id: number | null;
  /** Device registration predates Libation 14 (rmcrackan/Libation#2021); must be re-authenticated. */
  needs_reauth?: boolean;
}

export const accountsApi = {
  list: () => api.get<Account[]>("/accounts"),
  reauthenticateAccount: (accountId: string) =>
    api.post(`/accounts/${encodeURIComponent(accountId)}/reauthenticate`),
};

// Settings API
export const settingsApi = {
  getLibation: () =>
    api.get("/settings/libation"),

  updateLibation: (data: Record<string, unknown>) =>
    api.put("/settings/libation", data),

  getStats: () =>
    api.get("/settings/stats"),

  getAutomation: () =>
    api.get("/settings/automation"),

  updateAutomation: (data: Record<string, unknown>) =>
    api.put("/settings/automation", data),
};

/**
 * Text for an API error, safe to render. FastAPI `detail` is usually a string, but some endpoints
 * return an object (the scan cooldown guard sends `{message, last_scan_at, ...}`); putting that
 * object straight into JSX crashes the page with React error #31.
 */
export function errorText(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  return fallback;
}
