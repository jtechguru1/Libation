# Libation Web UI — CLAUDE.md

## Project overview
A Dockerized web application that wraps the LibationCli audiobook manager with a professional, mobile-responsive web UI. Built in phases.

## Architecture

### Single-container deployment
- **Backend**: Python 3.12 + FastAPI, served on port 8000
- **Frontend**: React 18 + Vite + Tailwind CSS, built to `/app/static` and served as static files by FastAPI
  - Only `/assets` (Vite's JS/CSS bundles) is mounted via `StaticFiles`. The catch-all `spa_fallback` route in `main.py` checks if the requested path exists as a file under `/app/static` first (serves it directly) before falling back to `index.html` — needed so root-level files in `frontend/public/` (favicons, logos, etc.) actually get served instead of silently returning the SPA shell
- **LibationCli**: Installed from the official `.deb` (`/usr/bin/libationcli`). Pinned by `LIBATION_VERSION` (Dockerfile ×2, docker-compose.yml) — currently **14.2.2**; upgraded from 13.4.9 on 2026-09-21 for upstream #2021 (License Denied)
- **LibationBridge**: ASP.NET Core 10 sidecar on `localhost:8001`; references Libation DLLs at `/usr/lib/libation/` directly. Handles downloads (with real `StreamingProgressChanged` progress) and scans. Login still uses the `libationcli` PTY subprocess.

### Volume layout
| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./data` | `/data` | App SQLite DB (`app.db`), session store |
| `./config` | `/config` | Libation config, `appsettings.json`, `LibationContext.db` |
| `./audiobooks` | `/audiobooks` | Downloaded audiobooks |

### Key paths
- Backend entry: `backend/app/main.py`
- Auth API: `backend/app/api/auth.py`
- Auth service: `backend/app/services/auth.py`
- Frontend entry: `frontend/src/main.tsx`
- Auth context: `frontend/src/context/AuthContext.tsx`
- Bridge source: `libation-bridge/Program.cs`
- Bridge project: `libation-bridge/LibationBridge.csproj`

## Auth system
- **Access token**: 15-min JWT in response body, stored in memory (React context)
- **Refresh token**: 60-day JWT in httpOnly cookie (`/api/auth` path), hashed in `sessions` table
- **2FA**: TOTP via `pyotp`, optional per user, toggled in Settings
- **Session persistence**: On page load, silently calls `/api/auth/refresh` using the cookie
- **Auto-refresh**: Timer in `AuthContext` refreshes access token 2 min before expiry

### Auth API endpoints (`backend/app/api/auth.py`)
- `GET /api/auth/default-credentials` — returns `{"using_default_credentials": bool}`; compares logged-in user's username against `ADMIN_USERNAME` env var and verifies stored hash still matches `ADMIN_PASSWORD`. Used by Settings page to show the amber warning banner.
- `PATCH /api/auth/me` — free-form dict body; updates `audible_account_id` and/or `owner_name` on the logged-in user
- `POST /api/auth/change-username` — body: `{new_username, current_password}`; validates ≥3 chars, 409 on conflict; returns updated `UserResponse`
- `POST /api/auth/change-password` — body: `{current_password, new_password}`; revokes all sessions on success
- `GET /api/auth/sessions` / `DELETE /api/auth/sessions/{id}` / `DELETE /api/auth/sessions` — session management for the logged-in user

## Database (SQLite at `/data/app.db`)
- `users`: id, username, hashed_password (bcrypt), totp_secret, totp_enabled, is_active, is_admin, permissions (JSON), download_cap (INTEGER), audible_account_id (TEXT), owner_name (TEXT), created_at
- `sessions`: id, user_id, refresh_token_hash (sha256), expires_at, created_at, last_used_at, ip_address, user_agent
- `downloads`: id, book_id, book_title, user_id, status, progress, started_at, completed_at, error_message, created_at
- `scans`: id, status, started_at, completed_at, books_added, output, error_message
- `audible_account_settings`: account_id (TEXT PK), added_by_user_id (INTEGER), auto_download (INTEGER DEFAULT 0) — created via `_migrate_db`; tracks which web UI user added each Audible account and whether auto-download is enabled
- `system_settings`: key (TEXT PK), value (TEXT DEFAULT '') — created via `_migrate_db`; holds:
  - `last_auto_download_at` — ISO timestamp of the last auto-download run (informational only since Phase 9; it no longer gates anything)
  - `scan_interval_minutes` — minutes between scheduled library scans, default `360`; `0` disables
  - `download_delay_seconds` — pause between consecutive downloads, default `30`; `0` means back-to-back

## Permissions system
- `DEFAULT_PERMISSIONS` in `models/user.py`: all flags `true` except `can_remove_downloads = false`
- Flags: `can_download`, `can_scan`, `can_manage_accounts`, `can_liberate`, `can_remove_downloads`
- Admins bypass all checks; non-admin users inherit `DEFAULT_PERMISSIONS` if their `permissions` column is NULL
- `PATCH /api/users/{id}/permissions` — admin-only, updates flags + `download_cap`
- `download_cap = null` means unlimited; positive integer = max downloads per 12-hour rolling window
- 12h window enforcement: `COUNT(downloads WHERE user_id=? AND created_at > NOW()-12h)`; 429 response includes `resets_at` ISO timestamp

## Liberate service
- `GET /api/liberate/books` — all books with status from `UserDefinedItem.BookStatus` (0=not_liberated, 1=liberated, 2=error) overlaid with active `downloads` table rows; accepts `account_id`, `search`, `filter_status`, `page`, `page_size` params; `filter_status` values: `all`, `downloaded`, `not_downloaded`, `in_progress`, `audible_plus` (IsAudiblePlus=1), `purchased` (IsAudiblePlus=0)
- `GET /api/liberate/book-ids` — returns all matching book IDs (no pagination) for Select All across pages; accepts same filter params including `purchased`
- `PATCH /api/liberate/books/{book_id}` — sets `UserDefinedItem.BookStatus` (1=liberated, 0=not liberated); INSERTs row if missing (provides all NOT NULL cols: BookStatus, IsFinished, Ratings, Tags)
- `GET /api/liberate/cap` — current cap accounting for logged-in user
- `POST /api/liberate/download-all` — **enqueues** every `not_liberated` book into the serial download queue (optional `account_id` filter); returns `{queued, skipped, total}`. Only available when the user has no cap; requires `can_liberate`. It no longer shells out to `libationcli liberate` — see *Download queue* below
- Individual downloads still go through `POST /api/downloads` with per-call cap enforcement

## Download queue (`backend/app/api/downloads.py`)
🔑 **Exactly one download runs at a time, process-wide.** Audible is liable to flag an account that
downloads many books simultaneously.
- `_download_worker()` — a single asyncio task started from the `main.py` lifespan. It is the **only**
  code that starts a download. It picks the oldest `status="queued"` row (by `created_at`, then `id`),
  awaits `_run_download()`, then sleeps `download_delay_seconds` if more work is waiting. It never dies:
  every exception is caught and logged, or the app would silently stop downloading forever.
- `enqueue_book(book_id, user_id, book_title)` — the single entry point for queueing. Returns `False`
  if the book is already `queued` or `running`. Manual, bulk and auto-download all go through it.
- `POST /api/downloads` inserts a `queued` row and returns. **It does not spawn a task** — previously
  every caller did, so queueing N books started N concurrent downloads.
- **Restart handling** (`main.py` lifespan): rows left `running` are set back to `queued` and resume;
  `queued` rows are untouched. Both used to be flipped to `error`, so restarting mid-batch discarded it.

## Scheduled scans (`_scan_scheduler` in `backend/app/api/downloads.py`)
- A second lifespan task. Every tick it re-reads `scan_interval_minutes` (so a Settings change applies
  without a restart), and if the interval has elapsed and no scan is running, starts one. `0` = off.
- On a successful scan, `_run_scan` chains into `_auto_download_if_enabled` exactly as before.
- Before this existed nothing ever re-scanned the library, so new books were never discovered and
  auto-download could not fire on its own.

## Version endpoint (`backend/app/api/updates.py`)
- `backend/app/version.py` — single source of truth for the web UI's own version, `APP_VERSION`. Imported by `main.py` (FastAPI `app` title/version, `/api/health`) and by `updates.py`.
- `GET /api/updates/version` — returns `{"cli_version": ..., "app_version": ...}`. `cli_version` is the installed CLI version (parsed from `libationcli --version`); `app_version` is `version.APP_VERSION`. Read-only, no GitHub polling.
- `GET /api/updates/changelog` — same auth dependency (`get_current_user`) as `/version`. Returns `{"markdown": <text>, "available": bool}`, reading `CHANGELOG.md` from `/app/CHANGELOG.md` (image) or the repo root (local dev) — never 500s; missing file returns `{"markdown": "", "available": false}`. Backs the Settings → About "What's new" viewer.
- The in-container self-update mechanism was removed because it is architecturally incompatible with LibationBridge: installing a new `.deb` replaces `/usr/lib/libation/*.dll` but leaves the bridge binary (compiled against the old DLL versions) unchanged, causing runtime `MissingMethodException` or container death on restart. To update LibationCLI, bump `LIBATION_VERSION` in the Dockerfile and rebuild the image.

## Entrypoint restart loop (`docker-entrypoint.sh`)
- Replaced `exec gosu ... uvicorn` with a `while true` loop so the container survives uvicorn crashes
- **Bridge bootstrap**: each iteration pre-seeds `/config/Libation/appsettings.json` with `{"LibationFiles":"/config"}` — Libation's startup bootstrap reads `{CWD}/Libation/appsettings.json` and `Program.cs` sets CWD to `/config`, so this tells it to use `/config` as its files dir (matching `libationcli --libationFiles /config`)
- **Bridge startup**: LibationBridge starts before uvicorn; `wait_for_bridge()` polls `GET /health` on `localhost:8001` up to 30×1s; fatal exit if it never responds
- Both `BRIDGE_PID` and `UVICORN_PID` tracked; bridge is killed when uvicorn exits and restarted on the next loop iteration
- Crash path: loop restarts both after 5s delay
- `SIGTERM` to container (e.g. `docker stop`) sets `SHOULD_EXIT=true`, kills both processes, exits loop cleanly

## Default credentials
Set via env vars `ADMIN_USERNAME` / `ADMIN_PASSWORD` (defaults: `admin` / `admin`).
Admin user is seeded on first startup if no users exist. `_seed_admin` uses raw SQL via `conn.execute()` (same as `_migrate_db`) rather than ORM — avoids the issue where `_migrate_db` calling `db.connection()` leaves a dangling DBAPI transaction that causes subsequent `db.add(User(...))` commits to silently not persist.

`GET /api/auth/default-credentials` detects whether the logged-in user is still on factory defaults by comparing their username/password against the env vars at runtime. When `using_default_credentials` is `true`, SettingsPage shows an amber warning banner and surfaces `UpdateCredentialsSection` — a single form that changes username + password together and signs the user out immediately after.

## Development
```bash
# Backend only
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend only (proxies /api to localhost:8000)
cd frontend && npm install && npm run dev

# Full stack via Docker
docker compose up --build
```

## Library service (`backend/app/services/libation.py`)
- Reads Libation's `LibationContext.db` at `{LIBATION_CONFIG}/LibationContext.db`
- Uses schema discovery (`PRAGMA table_info`) so it handles column name variations across Libation versions
- Returns `empty_reason: "no_accounts"` when no DB exists (user hasn't connected Audible yet)
- Authors/narrators via `BookContributors` + `Contributors`/`Persons` junction (contributor type 0=author, 1=narrator)
- Series via `BookSeries` + `Series` junction
- Cover paths stored in `PictureLarge` column; served via `GET /api/library/covers/{book_id}` (no auth required — images are not sensitive)

## CLI service (`backend/app/services/cli.py`)
- **Downloads and scans** route through LibationBridge HTTP (`BRIDGE_URL = http://localhost:8001`); login (`start_login` / `complete_login`) stays as a PTY subprocess because `libationcli login-external` requires a TTY
- `list_accounts()` → bridge `GET /accounts` (shim over `libationcli list-accounts --bare`; returns parsed tab-separated: account_id, name, locale, scan_library, authenticated)
- `run_liberate(book_ids, on_progress)` → bridge `POST /download/{asin}` (202), then polls `GET /progress/{asin}` every 2s; calls `on_progress(pct, output)` on each change; returns when status is `complete` or `error`
- `run_scan(on_line)` → bridge `POST /scan` (synchronous; 600s timeout); fires `on_line` callbacks by iterating the returned output string
- `login-external` subprocess is kept alive in `_PENDING_LOGINS` dict (keyed by UUID) between the two login steps; auto-expires after 10 min
- `ephemeralSettings: true` in LibationCli means all in-memory config changes (including Serilog sinks) are never persisted to `Settings.json`. The `/config/Logs/` directory is always empty at rest; stack traces only appear on stderr.

## LibationBridge sidecar (`libation-bridge/`)
- ASP.NET Core 10 minimal API on `localhost:8001`; self-contained single-file binary at `/usr/lib/libation/libation-bridge` (symlinked to `/usr/local/bin/libation-bridge`)
- References Libation DLLs at `/usr/lib/libation/` via `<Reference>` with `<Private>false</Private>` — DLLs are not bundled into the binary; loaded at runtime via `AssemblyResolve` hook
- `AssemblyResolve` hook registered before any Libation type is touched; all Libation code in `static class LibationBridgeApp` with `[MethodImpl(MethodImplOptions.NoInlining)]` to prevent JIT resolving DLLs before the hook fires
- Libation scaffolding called at startup: `RunPreConfigMigrations()` → `RunPostConfigMigrations()` → `RunPostMigrationScaffolding(Variety.Chardonnay, config)`; `Directory.SetCurrentDirectory("/config")` set first so bootstrap discovery resolves `{CWD}/Libation/appsettings.json` → `/config/Libation/appsettings.json`
- **Bridge API surface**:
  - `GET /health` — readiness probe (`{"status":"ok"}`)
  - `GET /debug` — diagnostic: DB path + book count + sample ASINs
  - `GET /accounts` — shim over `libationcli list-accounts --bare --libationFiles /config`
  - `POST /scan[?account=<id>]` — synchronous: runs `libationcli scan [<account>] --libationFiles /config`, awaits exit, returns `{"exit_code","output"}`; Kestrel keepalive set to 12 min. `libationcli scan` takes optional **positional** account IDs; omitting one scans every account. The account id is rejected with 400 if it starts with `-` or contains whitespace, so it cannot be parsed as a flag or split into extra arguments
  - `POST /download/{asin}` — 202 immediately; starts `DownloadDecryptBook.Create(config).ProcessAsync(book)` in background Task; `StreamingProgressChanged` handler updates in-memory `_progress[asin].Progress` (real 0–100%)
  - `GET /progress/{asin}` — returns `{"asin","progress","status","output"}` or 404
  - ~~`POST /download-all`~~ — **removed.** It ran `libationcli liberate --force`, which downloads concurrently under its own control — the opposite of the one-at-a-time rule the web UI now enforces. It also set `RedirectStandardOutput` without ever reading the pipe, so a chatty CLI could fill the buffer, block forever, and leave its `Interlocked` "already running" flag stuck at 1, permanently 409-ing every later call. Bulk downloads now arrive as individual `POST /download/{asin}` calls from the serial queue
- Completed progress entries expire after 1 hour via background cleanup Task
- **Dockerfile**: `bridge-builder` stage (between frontend-builder and runtime) installs Libation `.deb` so MSBuild resolves `<HintPath>/usr/lib/libation/*.dll>` at compile time; builds with `dotnet publish -r linux-x64 --self-contained true -p:PublishSingleFile=true -p:PublishTrimmed=false`; binary copied to runtime image at `/usr/lib/libation/libation-bridge`

## Docker / LibationCli quirks
- `libicu76` must be installed in the image. LibationCli uses .NET 10 which does NOT bundle its own ICU. Without ICU, `CultureInfo.GetCultures()` returns only the Invariant Culture (ID 0x7F), causing `new RegionInfo(c)` to throw `System.ArgumentException: There is no region associated with the Invariant Culture` inside `LocaleDto.GetRegion()` → called from `DownloadOptions..ctor` (line 82) → crash surfaces as "Error processing book. Skipping." with no file written. Never set `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`.
- InProgress directories land in `/tmp/Libation-{username}` (WinTemp default). Both `/tmp/Libation-root/` and `/tmp/Libation-libation/` may exist depending on which user ran the CLI.
- `DownloadDecryptBook.ProcessAsync` fires `OnCompleted` in a `finally` block, so "DownloadDecryptBook Completed" always appears in output even when an exception propagated — "Error processing book" follows immediately after from the outer `catch`.

## Accounts (`backend/app/api/accounts.py`)
- `GET /api/accounts` — lists Audible accounts from bridge, enriched with `owner_name`, `owner_username` (from `users` table), `auto_download`, and `added_by_user_id` (from `audible_account_settings`)
- `POST /api/accounts/login/start` / `POST /api/accounts/login/complete` — OAuth login flow via PTY subprocess; on `complete`, inserts a row into `audible_account_settings` marking which web UI user added the account
- `PATCH /api/accounts/{account_id}/auto-download` — body: `{auto_download: bool}`; updates `audible_account_settings.auto_download`; only callable by admin or the user who added that account (`added_by_user_id`)
- `DELETE /api/accounts/{account_id}` — removes account from `AccountsSettings.json`
- `POST /api/accounts/{account_id}/reauthenticate` — same auth as `DELETE` (any logged-in user); removes the account's entry from `AccountsSettings.json` (via the same `_remove_account_entry` helper `DELETE` uses — the `audible_account_settings` DB row is deliberately left alone) then calls `cli.start_login(email, locale)` and returns a `StartLoginResponse` (`session_id`, `login_url`) — same shape as `POST /login/start`, so the frontend drives the existing OAuth modal (skips the email/locale form step, starts at "open login URL") straight through to `POST /login/complete`. Locale is sourced from `cli.list_accounts()` (the bridge's parsed `list-accounts --bare` output), not from `AccountsSettings.json`'s raw `IdentityTokens.LocaleName`, since that's already in the format `login-external -l` expects. 404 if the account id isn't found; 500 (account-removed message, told to re-add via "Add account") if `start_login` fails after removal — the removed entry is never restored. Exists because Audible now refuses licences to Libation's old device registration (rmcrackan/Libation#2021); upstream's fix is remove-and-re-add, and this is a one-click version of that. Button on `AccountsPage.tsx`: `KeyRound` icon next to the remove (`Trash2`) icon on each account row.
- **`needs_reauth`** (`AccountResponse`, default `False`): set by `GET /api/accounts` from a guarded read of `AccountsSettings.json` (`_load_accounts_file()` returns `{}` on any failure, so a mid-rewrite parse error never 500s the list). Predicate `_needs_reauth(entry)`: `IdentityTokens.DeviceSerialNumber` is a bare string of exactly 40 hex chars — a Libation ≤13.x registration (rmcrackan/Libation#2021); 14.x mints 20. The serial is never returned.
- **Re-authenticate reminder** (`components/ReauthBanner.tsx`, rendered in `Layout.tsx` between the header and `<main>`): amber bar listing every account with `needs_reauth`, with a "Go to Audible Accounts" link. Shown only to admins or users with `can_manage_accounts` (null permissions = default = true). No server-side state — three client-side pieces:
  - `sessionStorage['reauth-dismissed:<userId>'] = "1"` — X button; hides the bar for this tab. Cleared by `AuthContext.login()` (not `logout()`, since session expiry never calls it), so it returns at the next sign-in; a reload keeps it.
  - `localStorage['reauth-snooze:<userId>:<accountId>'] = <epoch ms>` — "Remind me in 30 days" per account; excluded while the timestamp is in the future. Only consulted while that account's `needs_reauth` is true, so it goes inert on its own once the account is re-registered or removed; never garbage-collected.
  - `window` CustomEvent `accounts:changed` — `AccountsPage` dispatches it at the end of `fetchAccounts` and immediately in `handleReauthenticate` after the row is dropped; the banner re-fetches on it and on `user.id` change (login), never on route change. Fetch errors render nothing.
  `AccountsPage` shows an amber "Needs re-authentication" badge next to the status icon on such cards; if snoozed it adds "reminder snoozed" + an "unsnooze" link (removes the key, dispatches `accounts:changed`). Cancelling the reused login modal while `reauthMode` is on shows a page-level notice that the account was removed and not re-added.
- **Auto-scan after OAuth**: `AccountsPage.tsx` fires `POST /api/downloads/scan` silently in the background immediately after `login/complete` succeeds, then shows an info banner linking to `/liberate`
- **Owner name input**: when the logged-in user added an account (`added_by_user_id === user.id`) but has no `owner_name` set, an editable inline input appears in the Owner column (placeholder: "Fill in your first name"); saves via `PATCH /api/auth/me` on blur/Enter
- **Amber banner**: shown at top of AccountsPage when the current user has added at least one account but has no `owner_name`; text: "Fill in owner name to use split libraries."

## Downloads & Scan (`backend/app/api/downloads.py`)
- `POST /api/downloads/scan` creates a `Scan` row, fires `asyncio.create_task` to call `cli.run_scan()` → bridge `POST /scan`
- `POST /api/downloads` creates a `Download` row with `user_id`, fires task to call `cli.run_liberate(asin)` → bridge `POST /download/{asin}` + poll `GET /progress/{asin}`
- Background tasks update DB rows as progress changes; frontend polls `/api/downloads` every 2s
- Duplicate active downloads blocked with 409
- **Auto-download after scan** (`_auto_download_if_enabled`): called via `asyncio.create_task` after every successful scan, scheduled or manual. Reads `audible_account_settings` for accounts with `auto_download=1`; for each, fetches `not_liberated` book IDs and **enqueues** them under the admin user via `enqueue_book`, which skips anything already queued or running.
  - ⚠ The 30-minute global cooldown was **removed**. With scheduled scans able to run every 15 minutes it would have silently skipped auto-download on most of them — the setting would have said 15 and behaved like 30. The duplicate guard plus the serial queue already prevent the stampede it was guarding against. `system_settings.last_auto_download_at` is still written, but only as a record; nothing reads it to gate anything.
- `POST /api/downloads/scan?account_id=<id>` — optional `account_id` scans a single Audible account.

## User management (`backend/app/api/users.py`)
- Admin-only routes behind `require_admin` dependency
- `GET /api/users`, `POST /api/users`, `PATCH /api/users/{id}`, `DELETE /api/users/{id}`
- Cannot delete own account, cannot revoke own admin status
- `is_admin` column added via startup migration (`_migrate_db`) using `ALTER TABLE` + `PRAGMA table_info`

## Settings & Stats (`backend/app/api/settings.py`)
- `GET/PUT /api/settings/libation` — reads/writes `/config/appsettings.json` (resilient: merges only known keys)
- `GET/PUT /api/settings/automation` — **admin-only**; `scan_interval_minutes` (0/15/30/60/180/360/720/1440, default 360) and `download_delay_seconds` (0/15/30/60/300, default 30). Values live in `system_settings`; anything outside the allowed set is rejected with 422. Backed by `services/automation.py`, which upserts with `ON CONFLICT` — a bare `UPDATE` would affect zero rows for a key that was never seeded, making a saved setting look accepted while changing nothing. Surfaced as the **Automation** card at the top of the Settings page
- `GET /api/settings/stats` — total_books (LibationContext.db), total_downloads (our DB), accounts_count (bridge `/accounts`), downloads_per_user (JOIN)
- Field map: Python snake_case ↔ Libation PascalCase key names

## Logs API (`backend/app/api/logs.py`)
- `GET /api/logs?lines=200&level=all` — admin-only; reads `/config/logs/libation-web.log`, filters lines by `[LEVEL]` substring match, returns `{"lines": [...], "total": int, "truncated": bool}`; max 2000 lines per request
- `GET /api/logs/download` — admin-only; serves the full log file as `text/plain` download (`libation-web.log`)
- **LogsSection in Settings**: dark monospace terminal viewer (h-96), level filter tabs (ALL / INFO / WARN / ERROR / DEBUG), line count selector (100/200/500/1000), manual Refresh button, Auto-refresh toggle (polls every 5s), Download button; admin-only, shown at the bottom of SettingsPage
- **ApiDocsSection in Settings**: two links to FastAPI's built-in `/docs` (Swagger UI) and `/redoc`; admin-only, below LogsSection

## Logging (`backend/app/services/logger.py`)
- Writes to `/config/logs/libation-web.log` (on the mapped `/config` volume — survives container restarts)
- `RotatingFileHandler`: 5 MB per file, 3 backups (`libation-web.log`, `.1`, `.2`, `.3`)
- Log format: `YYYY-MM-DD HH:MM:SS [LEVEL] message`
- 🔑 **`log_cli` logs a non-zero exit at ERROR, with its output.** It used to log every call at INFO with the output at DEBUG regardless of outcome, so a failed download produced **nothing** under `GET /api/logs?level=error` while the full reason sat in the same file at DEBUG — the user saw a blank screen and had no way to find out why. Successful calls still log INFO + DEBUG output as before.
- Logged events:
  - **Startup**: server starting, stuck downloads/scans reset, ready
  - **list-accounts**: bridge `/accounts` call + duration
  - **Login**: start (email, locale), URL generated, completion success/failure
  - **Scan**: start, bridge `/scan` output, exit code, duration
  - **Liberate**: book IDs (or "all"), bridge `/download/{asin}` progress polling, final status
- OAuth URLs and response URLs are intentionally NOT logged (contain auth tokens)
- On Unraid: readable at `/mnt/user/appdata/libation/config/logs/libation-web.log`

## Rate limiting
- `slowapi` on `/api/auth/login` (20/min) and `/api/auth/verify-2fa` (10/min)
- Limiter instance in `backend/app/limiter.py` (separate to avoid circular imports)

## Dark mode
- `tailwind.config.js` has `darkMode: "class"` — `dark` class applied to `<html>` element
- `ThemeContext.tsx` persists choice to `localStorage`, toggles `<html class="dark">`
- Toggle button in sidebar (Moon/Sun icon)
- Dark mode variants added to: Layout, Sidebar, Card, Input components, and book grid pages

## PUID/PGID (Unraid support)
- `docker-entrypoint.sh` creates/modifies `libation` user/group at runtime using env vars PUID/PGID
- Uses `gosu` to drop privileges before exec'ing uvicorn
- Defaults to PUID=1000, PGID=1000; runs as root if PUID=0
- `unraid-template.xml` — Unraid Community Applications template (PUID=99, PGID=100 defaults for Unraid)

## Health check
- `GET /api/health` — public endpoint returning `{"status": "ok", "version": "0.5.0"}` (from `backend/app/version.py`)
- Dockerfile HEALTHCHECK uses `/api/health` instead of `/api/auth/me`

## Phase history
- **Phase 1** (complete): Project foundation, Docker setup, full auth system (login, 2FA, 60-day sessions, change password), React UI shell with sidebar navigation.
- **Phase 2** (complete): Library view — reads Libation SQLite DB, grid/list book view with cover art, search, sort, pagination, book detail slide-over, empty states.
- **Phase 3** (complete): Accounts & Downloads — add Audible accounts via `login-external` OAuth flow, library scan, per-book downloads via `liberate`, downloads page with progress polling, download button on book cards.
- **Phase 4** (complete): Settings & Polish — dashboard stat cards, Libation settings passthrough, multiple user management (admin CRUD), session management (list/revoke), dark mode toggle, PUID/PGID support, Unraid CA template, rate limiting on auth endpoints, improved health check.
- **Phase 5** (complete): Liberate view, My Books, per-user permissions, and download caps. New `/liberate` page shows all books with status overlays (green ✓ downloaded, red ✕ not downloaded, animated spinner for in-progress) and filter tabs. New `/my-books` page filters books by the user's linked Audible account. Per-user permission flags (`can_download`, `can_scan`, `can_manage_accounts`, `can_liberate`, `can_remove_downloads`) stored as JSON on users row; admin toggle matrix in Settings. 12-hour rolling window download cap: uncapped users get "Download All" (fires `libationcli liberate`), capped users get "Download Next N" auto-selecting books; cap enforced on both individual and bulk downloads (429 with `resets_at`). Enhanced book metadata via `UserDefinedItem` JOIN (BookStatus, Subtitle, ContentType, Language, IsAbridged, community ratings).
- **Phase 5 bug fix** (complete): Root cause of "Error processing book. Skipping." identified and fixed. `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` broke `CultureInfo.GetCultures()`, causing `RegionInfo` crash in `LocaleDto.GetRegion()` during every download attempt. Fix: removed the env var, added `libicu76` to Dockerfile apt-get install.
- **Phase 6** (complete, then partially removed): CLI self-update was built — entrypoint restart loop, GitHub Releases API polling, `.deb` download+install, rollback. Subsequently removed because it is architecturally incompatible with LibationBridge: `dpkg -i` replaces `/usr/lib/libation/*.dll` but the bridge binary (compiled against the old DLL versions) is not rebuilt, causing `MissingMethodException` or container death on the next restart. The in-container update mechanism is replaced by: bump `LIBATION_VERSION` in the Dockerfile and rebuild the image. A read-only About card in Settings still shows the installed CLI version via `GET /api/updates/version`.
- **Phase 5 Extended** (complete): User Management gains inline `owner_name` field and Audible Account dropdown (sets `users.audible_account_id`). Liberate page gains owner filter tabs, centered search bar (300ms debounce), per-book Mark Downloaded/Not Downloaded (`PATCH /api/liberate/books/{book_id}`), Multi Select mode with Select All spanning all pages (via `GET /api/liberate/book-ids`) plus bulk mark actions, and per-page selector [24/48/96/200]. Accounts page shows post-login "go to Downloads → Scan Library" info banner. Liberate moved to top of sidebar and set as default view (`/` redirects to `/liberate`). Library and My Books removed from sidebar nav and routes entirely (pages still exist in codebase but are not linked).
- **Phase 7 seed fix** (complete): `_seed_admin` in `main.py` rewritten to use raw SQL (`conn.execute()`) instead of ORM (`db.add(User(...))`). Root cause: `_migrate_db` calls `db.connection()` which acquires a DBAPI connection and begins a transaction; if no migrations run, no `db.commit()` is called, leaving the session with a dangling connection. The subsequent ORM `db.commit()` in the old `_seed_admin` did not reliably persist the row. The raw SQL approach shares the same connection path as `_migrate_db` and works correctly. Also added try/except with explicit logger.error logging and flush=True on print so failures are never silent.
- **Phase 7** (complete): LibationBridge ASP.NET Core 10 sidecar replaces subprocess calls for downloads and scans. New `libation-bridge/` directory with `LibationBridge.csproj` and `Program.cs`. Bridge references Libation DLLs at `/usr/lib/libation/` directly via `AssemblyResolve` hook + `[MethodImpl(NoInlining)]` isolation. Real `StreamingProgressChanged` events (0–100%) replace fake 5/95 progress jumps from stdout parsing. Dockerfile gains a `bridge-builder` stage (Stage 2) that installs the Libation `.deb` for compile-time DLL resolution then publishes a self-contained single-file binary. Entrypoint pre-seeds `/config/Libation/appsettings.json` with `{"LibationFiles":"/config"}` so the bridge's Libation scaffolding uses the same config path as `libationcli`. `cli.py` rewritten to route downloads and scans through bridge HTTP; login stays PTY subprocess. `BRIDGE_URL` added to `config.py`.
- **Phase 8** (complete): Operational hardening — auto-download, default-credentials UX, log viewer, and sidebar polish. Per-Audible-account auto-download toggle stored in new `audible_account_settings` table; `_auto_download_if_enabled()` fires after every successful scan with a 30-min global cooldown via `system_settings`. OAuth flow auto-triggers a library scan and shows a dismissable info banner on completion. `GET /api/auth/default-credentials` detects factory-default credentials; SettingsPage shows amber warning banner + `UpdateCredentialsSection` (change username + password in one step, then signs out). `POST /api/auth/change-username` added. Logs API (`GET /api/logs`, `GET /api/logs/download`) + `LogsSection` embedded in Settings (level filter, line count, auto-refresh, download). `ApiDocsSection` in Settings links to `/docs` and `/redoc`. Sidebar nav renamed "Accounts" → "Audible Accounts". `UserAdminResponse.created_at` made Optional to handle NULL rows from early-seeded users. `AccountResponse` gains `auto_download` and `added_by_user_id` fields.
- **Phase 9 Extended** (complete): First-run onboarding, account-owner assignment on the Accounts page, and a scan-result parsing fix.
  - **First-run onboarding** (`pages/OnboardingPage.tsx` + `OnboardingGate` in `App.tsx`): a signed-in user still on the seeded `admin`/`admin` is shown a full-screen onboarding step ahead of every route, and cannot reach the app until username + password are changed. Previously the only signal was an amber banner inside Settings — a page a brand-new user has no reason to open — so an install could run indefinitely on factory credentials. The gate **fails open**: if `GET /api/auth/default-credentials` errors, the app loads normally, because a network blip must not lock someone out of their own library. Username is changed before password, since `change-password` revokes all sessions.
  - **Owner assignment on the Accounts page** (`AccountsPage.tsx`): admins get a user dropdown per Audible account row plus that user's owner name. This is the same operation as `OwnerInfoCell` in Settings → User Management, keyed by *account* instead of by *user*. Assigning clears the previous holder first — otherwise two users could both claim one account and the displayed owner would depend on row order. `GET /api/users` is admin-only, so non-admins keep the existing self-service owner-name input and never trigger a 403.
  - 🔑 **`books_added` was always 0** (`_parse_books_added`). `_run_scan` searched for `N new book`, but LibationCli 13.x prints `Total processed: 682` / `New: 1`. A scan that imported a book therefore reported "Scan complete — 0 new books added". Now parses `New: <n>` with the old phrasing kept as a fallback. Confirmed against real captured scan output (`New: 1` → 1, `New: 681` → 681).
- **Libation 14.2.2 upgrade** (2026-09-21): `LIBATION_VERSION` 13.4.9 → 14.2.2 for upstream #2021 — every download failed with `AudibleApi.ContentLicenseDeniedException` because 13.x registered Android devices with a serial twice the expected length. Verified against the v14.2.2 source and a no-volume throwaway container:
  - `Program.cs` compiled unchanged: `DownloadDecryptBook.Create(config)`, `ProcessAsync`, `StreamingProgressChanged`, `DbContexts.GetLibrary_Flat_NoTracking`, `SqliteStorage.DatabasePath` and the scaffolding trio all kept their signatures; exceptions still propagate out of `ProcessAsync` (the bridge's `catch` → `status: error` path is intact).
  - `libationcli --version` output, `scan` output (`Total processed: N` / `New: N`), `liberate`/`scan` flags and the `LiberatedStatus` values (0/1/2) are unchanged. `list-accounts --bare` gained a 6th column (`also-scans`, multi-marketplace); the bridge reads columns 0–4 and ignores it.
  - DB schema is additive only: new tables `DownloadHistory`, `DownloadAttemptFailures`; new column `Books.Copyright`. No backend SQL touched.
  - 🔴 **Updating alone does not fix #2021** (upstream release note): accounts registered under 13.x keep the bad device registration. Remove and re-add each affected account so it registers again.
  - `login-external` gained `--device-registration {CurrentAndroid|RetailAndroid|Mkb79IPhone}` (defaults to Settings; CurrentAndroid is the corrected default) and `--response-url` (required when stdin is not a TTY — the PTY login path in `cli.py` is unaffected).
  - **Token encryption** is on by default (`TokenStorageMethod: Encrypted`). Docker has no OS secret store, so Libation mints `/config/libation-master.key` (+ `.NOTICE.txt`) on first run and both the CLI and the bridge use it. A plaintext 13.x `AccountsSettings.json` still loads; each token is re-written encrypted the next time it changes. `AccountsSettings.json` also gained a top-level `Cdm` (Widevine) key.
- **Phase 9** (complete): Download queue, scheduled scans, per-account scan. Six reported issues, which reduced to three defects and three missing features that chained together.
  - **`POST /api/liberate/download-all` had never worked on this branch** — it was `def`, not `async def`, so the `asyncio.create_task` it ended with raised `RuntimeError: no running event loop` in FastAPI's sync threadpool. A 500 on every call, for every user including admin, surfacing as "Failed to start bulk download". It is now `async def` and enqueues rather than shelling out.
  - **Serial download queue.** A single `_download_worker` lifespan task is the only thing that starts a download; `POST /api/downloads` merely inserts a `queued` row. Previously each caller spawned its own task, so queueing N books started N concurrent downloads — and Download All handed the whole job to `libationcli liberate --force`, which parallelises internally. Configurable pause between books.
  - **Scheduled scans.** A `_scan_scheduler` lifespan task re-scans on a configurable interval (default 6h, as low as 15 min, `0` = off). Nothing re-scanned the library before, which is why auto-download appeared broken: it only ever runs after a scan.
  - **Per-account scan.** Bridge `POST /scan` takes an optional `account` positional arg; a **Scan Library** button sits on each row of the Audible Accounts page, and adding an account now scans *that* account and reports failures instead of swallowing them with `.catch(() => {})`.
  - **Removed:** bridge `POST /download-all` (concurrent + latent pipe-buffer wedge) and the 30-minute auto-download cooldown (incompatible with a 15-minute scan interval).
  - **Error surfacing:** `log_cli` logs non-zero exits at ERROR; the bulk-download handler reads the server's real message instead of discarding the exception; the multi-select loop reports which book it stopped at and why.
  - **Restart safety:** interrupted `running` downloads are requeued rather than failed, so restarting mid-batch no longer discards the rest of the queue.
- **Phase 8 Extended** (complete): Owner name editable input added directly to AccountsPage for accounts the logged-in user added (`added_by_user_id === user.id`); saves via `PATCH /api/auth/me` on blur/Enter; amber banner shown when `owner_name` is unset. "Purchased" filter tab added to Liberate page between All and Audible Plus; filters on `LibraryBooks.IsAudiblePlus=0` in both `get_liberate_books()` and `get_liberate_book_ids()`.
- **Phase 10** (complete): "Re-authenticate" on the Accounts page. Audible now refuses licences to Libation's old device registration (rmcrackan/Libation#2021); upstream's fix is remove-and-re-add the account. `POST /api/accounts/{account_id}/reauthenticate` does both steps as one call — same auth as `DELETE`, removes the entry from `AccountsSettings.json` via a `_remove_account_entry` helper shared with `delete_account`, then calls `cli.start_login` and returns a `StartLoginResponse` so the frontend can drive the existing OAuth modal straight to "open login URL" → paste redirect. `audible_account_settings` DB row is left alone on purpose. `AccountsPage.tsx` gets a `KeyRound` icon button next to the remove control, a `window.confirm` warning, and a read-only email/locale banner while the reused login modal is in re-auth mode (`reauthMode`); `accountsApi.reauthenticateAccount` added to `api.ts`.
- **Phase 11 — "What's new" changelog viewer** (complete): The web UI gets its own real version number, `APP_VERSION` in the new `backend/app/version.py` (currently `0.5.0`), used by `/api/health` and the FastAPI app version. `GET /api/updates/version` gains `app_version` alongside `cli_version`. New `GET /api/updates/changelog` (same auth as `/version`) reads `CHANGELOG.md` — `/app/CHANGELOG.md` in the image (Dockerfile now copies it next to `backend/app`), the repo root in local dev — and never 500s on a missing file. Settings → About (`AboutSection` in `SettingsPage.tsx`) now shows `Web UI v<app_version> · LibationCli v<cli_version>` and, below it, a "What's new" panel that fetches the changelog, splits it on `\n## ` release headings, renders the newest release expanded and every older one collapsed behind a native `<details>`/`<summary>`, and scrolls internally past `60vh`. Rendered with `react-markdown` + `remark-gfm`, Tailwind-styled (dark mode included) to match the rest of Settings. The old "rebuild the Docker image to update LibationCLI" hint text was removed from the card — it's a maintainer instruction, not something an end user of the image needs to see.

## Pre-push sanitization (REQUIRED before any `git push`)

Before pushing to GitHub, the working tree must be fully sanitized. The container
should be in a clean "factory default" state — only the default `admin/admin`
credentials remain, no real Audible accounts, no real library data, no downloads.

### Files/directories to delete

| Path (host) | Reason |
|-------------|--------|
| `./config/AccountsSettings.json` | Real Audible OAuth tokens |
| `./config/LibationContext.db` | Real library data tied to a real Audible account |
| `./config/SearchEngine/` | Lucene index built from real library |
| `./config/logs/` | Log files that may contain real email addresses |
| `./config/Log*.log` | Libation's own monthly logs (written by both `libationcli` and the bridge) |
| `./config/libation-master.key` + `.NOTICE.txt` | AES-GCM master key for the encrypted tokens in `AccountsSettings.json` (v14+) — treat like a password |
| `./data/app.db` | Real user accounts and sessions; container recreates it with default `admin/admin` on next start |
| `./audiobooks/` (contents) | Downloaded audiobook files — purge all content, keep the directory |

### Inside the running container (ephemeral, non-volume)

| Path (container) | Reason |
|-----------------|--------|
| `/tmp/Libation-*/` | In-progress download staging directories |

### Files to keep / verify

| Path | Expected content |
|------|-----------------|
| `./config/Settings.json` | Only `{"Books": "/audiobooks"}` — no credentials |
| `./config/appsettings.json` | Libation download toggles only — no credentials |
| `./config/Libation/appsettings.json` | Only `{"LibationFiles":"/config"}` — recreated by entrypoint anyway |
| `docker-compose.yml` | `SECRET_KEY` must still be the placeholder `change-me-use-a-long-random-string`; `ADMIN_USERNAME`/`ADMIN_PASSWORD` must be `admin`/`admin` |

### Post-purge verification

After deleting the above, restart the container (`docker compose restart`). On startup:
- `_seed_admin` recreates `app.db` with only the default `admin/admin` user
- No Audible accounts are connected
- The Liberate page shows "no accounts" empty state
- `/audiobooks/` directory exists but is empty

> **Important:** Always do a final `docker compose restart` after sanitizing, even if the container was already restarted mid-process. Deleting `app.db` while the container is live causes a disk I/O error on the stale file handle; the entrypoint restart loop recovers and re-seeds the DB, but a subsequent sanitization pass will delete that freshly-seeded file too — leaving the container running with no database and login broken. The final restart ensures `app.db` is cleanly re-created after all deletions are complete.

## Conventions
- API routes: `/api/<resource>/<action>`
- All API responses use snake_case JSON
- Frontend uses `@/` alias for `frontend/src/`
- No ads, no telemetry, no external dependencies at runtime
