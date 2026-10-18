import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Clock, Loader2, PauseCircle } from "lucide-react";
import { api, errorText } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";

// Queued books must never sit still without the page saying why. Shows what the
// queue is doing whenever books are waiting; an admin can start it again whatever the reason.

interface QueueState {
  state: "starting" | "idle" | "downloading" | "waiting" | "paused" | "stalled";
  book: string | null;
  until: string | null;
  reason: string;
  queued: number;
}

const time = (iso: string | null, seconds = false) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }) : "";

export function QueuePauseBanner() {
  const { user } = useAuth();
  const [q, setQ] = useState<QueueState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api.get("/downloads/queue/state").then(r => setQ(r.data)).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  if (!q || (q.queued === 0 && q.state !== "paused")) return null;

  const waiting = `${q.queued} book${q.queued === 1 ? "" : "s"} waiting`;
  let icon = <Clock className="h-5 w-5 text-slate-500 shrink-0" />;
  let title = "";
  let detail = "";
  let warn = false;
  switch (q.state) {
    case "downloading":
      icon = <Loader2 className="h-5 w-5 text-brand-600 animate-spin shrink-0" />;
      title = `Downloading ${q.book ?? "a book"}`;
      detail = `${waiting}. Books download one at a time.`;
      break;
    case "waiting":
      title = `Next download starts at ${time(q.until, true)}`;
      detail = `${waiting} - ${q.reason}.`;
      break;
    case "paused":
      icon = <PauseCircle className="h-5 w-5 text-amber-600 shrink-0" />;
      title = `Downloads paused until ${time(q.until)}`;
      detail = `${q.reason}. ${q.queued ? waiting + " - they" : "Waiting books"} start again on their own.`;
      warn = true;
      break;
    case "stalled":
      icon = <AlertTriangle className="h-5 w-5 text-red-600 shrink-0" />;
      title = `${waiting}, but the download queue isn't running`;
      detail = "Start now restarts it. If it stops again, check Settings → Logs.";
      warn = true;
      break;
    default:
      title = `${waiting}`;
      detail = "Starting the next download…";
  }

  const resume = async () => {
    setBusy(true); setError("");
    try { await api.post("/downloads/queue/resume"); load(); }
    catch (e) { setError(errorText(e, "Could not start the queue.")); }
    finally { setBusy(false); }
  };

  return (
    <div className={`rounded-xl border px-4 py-3 flex flex-wrap items-center gap-3 ${
      warn ? "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20"
           : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800"}`}>
      {icon}
      <div className={`min-w-0 flex-1 text-sm ${warn ? "text-amber-800 dark:text-amber-300" : "text-slate-700 dark:text-slate-200"}`}>
        <p className="font-semibold">{title}</p>
        <p className="text-xs opacity-90">{detail}</p>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      {user?.is_admin && ["waiting", "paused", "stalled"].includes(q.state) && (
        <Button size="sm" onClick={resume} loading={busy} title="Start the next download now, whatever the reason">
          Start now
        </Button>
      )}
    </div>
  );
}
