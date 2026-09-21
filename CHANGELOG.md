# Changelog

All notable changes to the Libation Web UI fork.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Unreleased] — Libation 14.2.2: fixes "Content License denied" on every download

### Fixed

- **Every download failed with `Content License denied`.** Upstream bug
  [rmcrackan/Libation#2021](https://github.com/rmcrackan/Libation/issues/2021): Libation registered
  its Android device with a serial number twice the expected length and Audible began refusing
  licences to it. Fixed upstream in v14.2.0; this image moves from **13.4.9 to 14.2.2**. The
  LibationBridge sidecar compiled unchanged against the v14 DLLs.
  ⚠ **Updating alone is not enough** — the old device registration stays broken until the account
  is removed and re-added. Use the new Re-authenticate button below.
- **The Audible login URL could arrive truncated** (Amazon showed "not a functioning page"). The
  v14 login URL is ~940 characters and the backend returned it as soon as its *start* appeared in
  the CLI output; it now waits for the end of the line.

### Added

- **Re-authenticate button** (key icon) on each Audible account card. Removes the account from
  Libation and immediately starts a fresh sign-in for the same email and locale, so the account is
  registered with Audible as a new device. Library data and per-account settings (auto-download,
  owner) are kept. New endpoint `POST /api/accounts/{id}/reauthenticate`.

### Changed

- Libation 14 encrypts stored tokens by default. With no OS secret store in a container it writes a
  portable `libation-master.key` next to `AccountsSettings.json` in `/config`. Treat it like a
  password; it lives in the `config` volume and is never part of the image.

## [Unreleased] — Phase 9: download queue, scheduled scans, first-run onboarding

Six reported issues, which turned out to be three defects and three missing features that chained
together, plus several problems found while verifying the fixes.

### 🔒 Security

- **A default-credential admin account was recreated on every container restart.**
  `_seed_admin` looked for a user *named* `ADMIN_USERNAME`. Renaming the admin account — which the
  new onboarding flow tells every operator to do — left no matching row, so a fresh `admin` / `admin`
  account with **full admin rights** was silently created on each restart. Seeding now happens only
  when the users table is empty, which is what the documentation always claimed it did.
  Verified by renaming the admin and restarting three times: no account reappeared.

### Added

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

### Fixed

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

### Removed

- **Bridge endpoint `POST /download-all`.** It ran `libationcli liberate --force` (concurrent
  downloads) and set `RedirectStandardOutput` without ever reading the pipe, so a chatty CLI could
  fill the buffer, block indefinitely, and leave its "already running" flag stuck — permanently
  returning 409 to every later call. Bulk downloads now arrive as individual `POST /download/{asin}`
  calls from the serial queue.

### Notes for operators

- **Scanning frequently can get your Audible account rate-limited.** Once that happens, downloads
  fail with a licence denial *even for books you own outright*. This was observed during development:
  five scans of a 681-title library within 21 minutes produced `"RejectionReason": "CustomerThrottled"`
  on every subsequent download. The persisted schedule, the manual-scan guard, and the queue
  stand-down all exist because of it. Intervals under 3 hours now carry a warning in Settings.
- `LIBATION_VERSION` is unchanged at `13.4.9`. Bumping it requires rebuilding LibationBridge against
  the new DLLs and is a separate change.
