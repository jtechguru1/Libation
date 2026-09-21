import { useState } from "react";
import { KeyRound, ShieldAlert, BookOpen } from "lucide-react";
import { authApi } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert } from "@/components/ui/alert";

/**
 * First-run onboarding.
 *
 * A fresh install seeds `admin` / `admin`. Those defaults used to be flagged only by an amber
 * banner inside Settings, which a new user has no particular reason to open — so an install could
 * sit on factory credentials indefinitely without anything insisting otherwise. This screen takes
 * over the whole app while the defaults are in place: the account is secured before the library,
 * the Audible connection, or anything else is reachable.
 *
 * Deliberately offers no way to skip. The one escape is signing out.
 */
export function OnboardingPage({ onDone }: { onDone: () => void }) {
  const { user, logout } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (username.trim().length < 3) { setError("Username must be at least 3 characters."); return; }
    if (username.trim() === user?.username) { setError("Choose a username different from the default."); return; }
    if (password.length < 8) { setError("Password must be at least 8 characters."); return; }
    if (password !== confirmPassword) { setError("Passwords do not match."); return; }
    if (!currentPassword) { setError("Enter the current password to confirm."); return; }

    setLoading(true);
    try {
      // Username first: changing the password revokes every session, which would invalidate the
      // token the username call needs.
      await authApi.changeUsername(username.trim(), currentPassword);
      await authApi.changePassword(currentPassword, password);
      setDone(true);
      onDone();
      // changePassword revokes all sessions, so the only correct next step is a fresh sign-in.
      setTimeout(() => { logout(); }, 1800);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail ?? "Could not update the credentials.";
      setError(msg);
      setLoading(false);
    }
  };

  if (done) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50 dark:bg-slate-900 px-4">
        <div className="w-full max-w-md text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-green-100 dark:bg-green-900/40 mb-4">
            <KeyRound className="h-6 w-6 text-green-600 dark:text-green-400" />
          </div>
          <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">Credentials updated</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-2">
            Signing you out so you can log in with your new username and password…
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50 dark:bg-slate-900 px-4 py-10">
      <div className="w-full max-w-md">
        <div className="text-center mb-6">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-600 mb-4">
            <BookOpen className="h-6 w-6 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Welcome to Libation</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            One step before you start — secure this account.
          </p>
        </div>

        <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-6 shadow-sm">
          <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40 px-4 py-3 mb-5">
            <ShieldAlert className="h-4 w-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
            <p className="text-sm text-amber-800 dark:text-amber-300">
              This install is still using the default <strong className="font-semibold">admin / admin</strong>{" "}
              credentials. Anyone who can reach this page can sign in until you change them.
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {error && <Alert variant="error">{error}</Alert>}

            <div>
              <Label htmlFor="ob-current">Current password</Label>
              <Input
                id="ob-current" type="password" autoComplete="current-password"
                value={currentPassword} onChange={e => setCurrentPassword(e.target.value)}
                required placeholder="The password you just signed in with"
              />
            </div>

            <div className="pt-1 border-t border-slate-100 dark:border-slate-700" />

            <div>
              <Label htmlFor="ob-username">New username</Label>
              <Input
                id="ob-username" autoComplete="username" value={username}
                onChange={e => setUsername(e.target.value)} required minLength={3}
                placeholder="At least 3 characters"
              />
            </div>
            <div>
              <Label htmlFor="ob-password">New password</Label>
              <Input
                id="ob-password" type="password" autoComplete="new-password" value={password}
                onChange={e => setPassword(e.target.value)} required minLength={8}
                placeholder="At least 8 characters"
              />
            </div>
            <div>
              <Label htmlFor="ob-confirm">Confirm new password</Label>
              <Input
                id="ob-confirm" type="password" autoComplete="new-password" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)} required
              />
            </div>

            <Button type="submit" className="w-full" loading={loading}>
              Secure this account
            </Button>

            <p className="text-xs text-center text-slate-400 dark:text-slate-500">
              You'll be signed out and can log straight back in. Next: connect an Audible account.
            </p>
          </form>
        </div>

        <button
          onClick={() => logout()}
          className="mt-4 w-full text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
        >
          Sign out instead
        </button>
      </div>
    </div>
  );
}
