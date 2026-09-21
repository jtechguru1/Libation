import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, X } from "lucide-react";
import { accountsApi, type Account } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

/** Storage keys. Both are consulted only while an account's `needs_reauth` is true, so they go
 *  inert on their own once the account is re-registered or removed — nothing is garbage-collected.
 *  - sessionStorage `reauth-dismissed:<userId>` = "1": banner hidden for this tab; cleared by
 *    AuthContext.login(), so it comes back at the next sign-in (a reload keeps it).
 *  - localStorage `reauth-snooze:<userId>:<accountId>` = epoch ms: that account is left out of the
 *    banner until the timestamp passes (30 days). */
export const dismissedKey = (userId: number) => `reauth-dismissed:${userId}`;
export const snoozeKey = (userId: number, accountId: string) => `reauth-snooze:${userId}:${accountId}`;
const SNOOZE_MS = 30 * 24 * 60 * 60 * 1000;

/** True while the account's snooze timestamp is in the future. Any storage failure reads as
 *  "not snoozed" — the reminder is the safe default. */
export function isSnoozed(userId: number, accountId: string): boolean {
  try {
    const raw = localStorage.getItem(snoozeKey(userId, accountId));
    return raw !== null && Number(raw) > Date.now();
  } catch {
    return false;
  }
}

/** Name of the window event AccountsPage fires whenever the account list may have changed, so the
 *  banner re-fetches without any route-change coupling. */
export const ACCOUNTS_CHANGED_EVENT = "accounts:changed";

/** Semi-persistent reminder that accounts registered under Libation <= 13.x must be
 *  re-authenticated (rmcrackan/Libation#2021). No server-side state: dismiss lasts one login,
 *  snooze is per account for 30 days, and it disappears for good once no account needs it. */
export function ReauthBanner() {
  const { user } = useAuth();
  const userId = user?.id;
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [dismissed, setDismissed] = useState(false);
  // Bumped on snooze so the filtered list re-evaluates without refetching.
  const [snoozeTick, setSnoozeTick] = useState(0);

  const canManage = !!user && (user.is_admin || (user.permissions?.can_manage_accounts ?? true));

  const load = useCallback(() => {
    if (userId === undefined) return;
    accountsApi.list()
      .then(r => setAccounts(Array.isArray(r.data) ? r.data : []))
      .catch(() => setAccounts([])); // no error UI, no retry until the next trigger
  }, [userId]);

  // Fetch on login (user id change) and whenever the Accounts page reports a change. Route
  // changes deliberately do not trigger a fetch.
  useEffect(() => {
    if (userId === undefined || !canManage) return;
    try {
      setDismissed(sessionStorage.getItem(dismissedKey(userId)) === "1");
    } catch {
      setDismissed(false);
    }
    load();
    window.addEventListener(ACCOUNTS_CHANGED_EVENT, load);
    return () => window.removeEventListener(ACCOUNTS_CHANGED_EVENT, load);
  }, [userId, canManage, load]);

  const pending = useMemo(
    () => userId === undefined ? [] : accounts.filter(a => a.needs_reauth && !isSnoozed(userId, a.account_id)),
    [accounts, userId, snoozeTick],
  );

  if (!user || userId === undefined || !canManage || dismissed || pending.length === 0) return null;

  const dismiss = () => {
    try { sessionStorage.setItem(dismissedKey(userId), "1"); } catch { /* private mode etc. */ }
    setDismissed(true);
  };

  const snooze = (accountId: string) => {
    try { localStorage.setItem(snoozeKey(userId, accountId), String(Date.now() + SNOOZE_MS)); } catch { /* private mode etc. */ }
    setSnoozeTick(t => t + 1);
  };

  return (
    <div
      className="flex items-start gap-3 border-b border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40 px-4 py-3 shrink-0"
    >
      <AlertCircle className="h-4 w-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
          Audible changed how devices are registered. Downloads for these accounts will fail with
          &ldquo;Content License denied&rdquo; until they are re-authenticated:
        </p>
        <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-amber-700 dark:text-amber-400">
          {pending.map(a => (
            <li key={a.account_id} className="flex items-center gap-2">
              <span className="font-medium text-amber-800 dark:text-amber-300">{a.name || a.account_id}</span>
              <button
                type="button"
                onClick={() => snooze(a.account_id)}
                className="underline hover:text-amber-900 dark:hover:text-amber-200"
                title="Hide this account from the reminder for 30 days"
              >
                Remind me in 30 days
              </button>
            </li>
          ))}
        </ul>
        <Link
          to="/accounts"
          className="inline-block mt-2 rounded-lg border border-amber-300 dark:border-amber-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs font-medium text-amber-800 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-slate-700 transition-colors"
        >
          Go to Audible Accounts
        </Link>
      </div>
      <button
        type="button"
        onClick={dismiss}
        title="Dismiss until next sign-in"
        className="text-amber-400 hover:text-amber-600 dark:hover:text-amber-200 shrink-0"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
