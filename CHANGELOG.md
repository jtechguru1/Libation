# Changelog

All notable changes to the Libation Web UI fork.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

---

## [0.5.0] — 2026-09-21 — Libation 14.2.2, download queue, scheduled scans, first-run onboarding

Libation 14.2.2 fixes "Content License denied" on every download. Six further reported issues,
which turned out to be three defects and three missing features that chained together, plus
several problems found while verifying the fixes.

### 🔒 Security

- **A default-credential admin account was recreated on every container restart.**
  `_seed_admin` looked for a user *named* `ADMIN_USERNAME`. Renaming the admin account — which the
  new onboarding flow tells every operator to do — left no matching row, so a fresh `admin` / `admin`
  account with **full admin rights** was silently created on each restart. Seeding now happens only
  when the users table is empty, which is what the documentation always claimed it did.
  Verified by renaming the admin and restarting three times: no account reappeared.

### Fixed
- **"This device" badge did not appear in Brave** (it did in Chrome/Firefox). The session list request did not send credentials explicitly; Brave's privacy hardening then withheld the login cookie from it, so the server could not identify the caller's session. The API client now sends credentials on every request.
- **Active Sessions showed times 5 hours off and never marked "This device"; the current device could also be logged out unexpectedly.** Session timestamps now carry a UTC offset so the browser shows them in local time (they were emitted naive and read as local). The session cap now evicts the *least-recently-used* session instead of the oldest-created — a silent token refresh keeps `last_used_at` current but not `created_at`, so an actively-used session was being evicted the moment newer logins piled up, which logged the user out, broke the "This device" match and forced another login.

- **Every download failed with `Content License denied`.** Upstream bug
  [rmcrackan/Libation#2021](https://github.com/rmcrackan/Libation/issues/2021): Libation registered
  its Android device with a serial number twice the expected length and Audible began refusing
  licences to it. Fixed upstream in v14.2.0; this image moves from **13.4.9 to 14.2.2**. The
  LibationBridge sidecar compiled unchanged against the v14 DLLs.
  ⚠ **Updating alone is not enough** — the old device registration stays broken until the account
  is removed and re-added. Use the new Re-authenticate button below.
- **A download could finish, then silently vanish before reaching the Books folder — and still
  show as complete.** Two defects. (1) Libation deletes everything in its `DecryptInProgress` temp
  folder every time `libationcli` starts, and the web app starts `libationcli list-accounts` on a
  timer; with one shared `/tmp`, a poll landing inside the ~50-second decrypt window deleted the
  half-written M4B out from under LibationBridge. The bridge now runs with its own `TMPDIR`
  (`/tmp/libation-bridge`), so the CLI's startup cleanup only touches the CLI's own files.
  (2) LibationBridge ignored the status that `ProcessAsync` returns — upstream reports most
  failures there rather than by throwing — so a book that never reached the Books folder was
  reported as "complete". The bridge now reports those failures as errors with the message.
  Present since the first LibationBridge build; not specific to Libation 14.
- **The Accounts page crashed** ("Something went wrong", React error #31) when the automatic scan
  after adding or re-authenticating an account was refused by the 10-minute scan guard. The
  guard's reply is an object, and the page rendered it as text. It now shows the guard's message.
- **The Audible login URL could arrive truncated** (Amazon showed "not a functioning page"). The
  v14 login URL is ~940 characters and the backend returned it as soon as its *start* appeared in
  the CLI output; it now waits for the end of the line.
- **"Failed to start bulk download" — Download All had never worked on this branch.**
  `download_all` was declared `def` rather than `async def`; FastAPI runs sync handlers in a
  threadpool with no event loop, so its `asyncio.create_task` raised
  `RuntimeError: no running event loop` — an HTTP 500 on every call, for every user including admin.
- **Downloads ran concurrently, from two places.** Every queued book spawned its own task, so
  queueing N books started N simultaneous downloads; and Download All shelled out to
  `libationcli liberate --force`, which parallelises internally and outside the app's control.
  Both now route through the serial queue.
- **A restart mid-batch discarded the rest of the queue.** Interrupted `running` downloads were
  marked `error`; they are now returned to `queued` and resume.
- **Scan results always reported 0 new books.** The parser looked for `N new book`; LibationCli 13.x
  prints `Total processed: 683` / `New: 1`. A scan that imported a book reported finding nothing.
- **Error messages were unreadable.** Failures stored the *last* 500 characters of a stack trace, so
  the UI showed fragments like `s.Factory.cs:line 124` while the line that says what went wrong
  (`ContentLicenseDeniedException: Content License denied for asin: […]`) sits at the top and was cut
  off. Errors now lead with the exception message.
- **Failed downloads were invisible.** CLI/bridge failures were logged at INFO with detail at DEBUG,
  so `GET /api/logs?level=error` returned nothing while a full stack trace sat in the same file.
  Non-zero exits now log at ERROR, with output.
- **Timestamps could display five hours out.** `downloads` and `scans` use `Column(DateTime)`, which
  drops tzinfo on write, so the API emitted offset-less strings — and JavaScript reads those as
  *local* time. All API datetimes now carry an explicit UTC offset.
- **Auto-download was suppressed by a hidden cooldown.** A hardcoded 30-minute global cooldown meant
  most scheduled scans skipped auto-download entirely once intervals below 30 minutes were possible.
  Removed; the duplicate guard and serial queue already prevent a stampede.
- **Auto-scan on account add scanned every account and hid its failures.** It now scans the account
  that was just added and surfaces errors instead of discarding them with `.catch(() => {})`.
- **Download All was gated on the wrong permission.** The UI showed it for `can_download` while the
  endpoint enforces `can_liberate`, so a user with one flag but not the other saw a button
  guaranteed to return 403.
- **Bulk queueing failed silently.** The multi-select loop broke on the first error with no message;
  it now reports how many were queued, which book stopped it, and why.
- **The Active Sessions list grew without bound** (expired rows were never pruned, no per-user cap);
  expired sessions are now cleaned on login and startup, and each user keeps at most 10 most-recent
  sessions.
- **Duplicate "Failed" rows piled up for one book.** The queue's "already present" guard only checked
  for `queued`/`running` rows, so a still-not-downloaded book that kept failing got a brand-new `error`
  row on every automatic scan (observed: two rows for the same ASIN, one with no title). Now
  `enqueue_book` reuses an existing `error` row (resets it to `queued`) instead of inserting a second;
  a successful download deletes any leftover `error` rows for the same book; and the automatic
  post-scan auto-download skips any book that already has an `error` row, so a licence-denied book is
  no longer re-attempted on every scan. Manual and bulk downloads can still retry a failed book — the
  user re-authenticates, then retries deliberately.

### Added

- **Downloads filter pills.** The Downloads page now has a three-pill filter row — **Downloading**,
  **Downloaded**, **Failed** — each showing a live count, replacing the old stacked sections. It opens
  on Downloading every time.
- **Clear all failed.** A one-click button on the Failed pill (with a confirm) removes every failed
  download at once, backed by a new `DELETE /api/downloads/failed` endpoint.
- **Re-authenticate button** (key icon) on each Audible account card. Removes the account from
  Libation and immediately starts a fresh sign-in for the same email and locale, so the account is
  registered with Audible as a new device. Library data and per-account settings (auto-download,
  owner) are kept. New endpoint `POST /api/accounts/{id}/reauthenticate`.
- **Re-authenticate reminder.** Accounts registered under Libation 13.x still carry the broken
  device registration after the upgrade, and nothing told you. `GET /api/accounts` now flags them
  (`needs_reauth`, from the length of the stored device serial — 40 hex chars on 13.x, 20 on 14.x),
  an amber bar under the page header lists them with a link to Audible Accounts, and each account
  card gets a "Needs re-authentication" badge. The bar can be dismissed (comes back at the next
  sign-in), snoozed per account for 30 days ("Remind me in 30 days"; "unsnooze" on the card), and
  disappears on its own once every account has been re-authenticated. Nothing is stored on the
  server.
- **Serial download queue.** Exactly one book downloads at a time, process-wide, with a configurable
  pause between books. Audible is liable to flag an account that downloads in bulk simultaneously.
- **Scheduled library scans.** Configurable interval (default 6h; 15m–24h, or off) so new purchases
  are discovered without anyone clicking. The schedule is **persisted to disk**, so restarting the
  container resumes it rather than triggering a fresh scan.
- **Per-account "Scan Library" button** on the Audible Accounts page. `libationcli scan` accepts
  positional account IDs, so this scans only that account.
- **First-run onboarding.** An install still on `admin` / `admin` is taken to a full-screen setup
  step ahead of every route and cannot reach the app until the credentials are changed. Previously
  the only hint was a banner inside Settings, which a new user has no reason to open.
- **Automation settings** (`GET`/`PUT /api/settings/automation`, admin-only) plus an Automation card
  in Settings: scan interval and pause-between-downloads.
- **Back-to-back scan guard.** A manual scan within 10 minutes of the last one asks for confirmation
  and explains the risk, with an explicit "Scan anyway" override. Repeated scanning is what gets an
  account rate-limited, and a user clicking refresh has no way to know that.
- **Queue stand-down.** Three consecutive download failures pause the queue for 30 minutes. Waiting
  books stay queued and resume automatically, so a rate-limited account is not hammered further.
- **Timezone support.** `tzdata` in the image and a `TZ` environment variable (compose + Unraid
  template). Without it the container was pinned to UTC with nothing saying so.
- **Owner assignment on the Audible Accounts page.** Admins can set which user owns an account, and
  that user's display name, from the account's own row — previously only possible in
  Settings → User Management, keyed by user.
- **Liberate page refreshes itself** when a scan completes, with a banner naming the number of new
  books. Scans can now happen unattended, so the page can no longer assume the library only changes
  when the user acts.
- **"What's new" in Settings → About (v0.5.0).** The About card now shows the web UI version
  alongside the installed LibationCLI version, and a new "What's new" panel renders this changelog
  in place — the current release expanded, older releases collapsed.
- **Active Sessions now marks your current device.** The session list flags the row you're signed in
  from with a "This device" badge, sorts it first, and hides its revoke button (dropping it would log
  you out — use "Revoke all" for that).

### Changed

- The "What's new" changelog in Settings → About is now collapsed by default; the version line stays
  visible and the panel expands on click.
- Libation 14 encrypts stored tokens by default. With no OS secret store in a container it writes a
  portable `libation-master.key` next to `AccountsSettings.json` in `/config`. Treat it like a
  password; it lives in the `config` volume and is never part of the image.

### Removed

- **Bridge endpoint `POST /download-all`.** It ran `libationcli liberate --force` (concurrent
  downloads) and set `RedirectStandardOutput` without ever reading the pipe, so a chatty CLI could
  fill the buffer, block indefinitely, and leave its "already running" flag stuck — permanently
  returning 409 to every later call. Bulk downloads now arrive as individual `POST /download/{asin}`
  calls from the serial queue.

### Notes for operators

- **Scan intervals under 3 hours carry a warning in Settings.** Audible does rate-limit accounts
  that hammer its API, and the persisted schedule, the manual-scan guard and the queue stand-down
  exist to keep an install well inside that. ⚠ *Correction (2026-09-21):* during Phase 9 development
  every download returned `"RejectionReason": "CustomerThrottled"` and this was attributed to five
  scans in 21 minutes. It was not — that is the exact symptom of upstream bug #2021 (see the 14.2.2
  entry above), which affected every Libation 13.x install regardless of scan frequency.
