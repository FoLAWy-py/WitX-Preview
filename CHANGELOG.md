# Changelog

What each published update of the WitX developer preview brought. Dates are when the update was published here.

## 2026-11-02

### Added
- **Files for tasks.** Attach up to 10 files (100 MiB each) to a message. Files live in the object store and are deleted with the task; uploads not attached within a day are cleaned up.
- **WitX iPhone app** (`apps/ios`). A thin app that takes a file from the Gateway by deep link, downloads it from your own server, checks its SHA-256 and keeps it in Files › On My iPhone › WitX. The agent hands files over with `send_file`. Android is not supported yet.
- **Removing devices.** Devices that are no longer connected are listed apart, and an Owner can remove one that has no task, takeover or pending ruling.
- **`witx admin force-stop`** finishes a stop whose Gateway will never come back, after showing what it will do.
- **Conversation time markers**, and "N need you" in the sidebar leads to the waiting tasks.
- **One search** across Skills and Plugins on the Customize page.

### Changed
- When an input's effect is UNKNOWN, the run waits for your ruling once and the ruling becomes that input's result.
- Each model call carries only the tools the run can use; the separate app search tool (`find_app`) is gone.
- The agent is told the language you write in, from your first message.
- A Gateway that disappears without disconnecting has its devices marked offline after 90 seconds.
- The message box states file limits before you pick, and says what a pick left out.
- Local development ports: API **47100**, web **47173**, browser tests **47174**.
- CI runs lint, Python tests and the browser tests (Chromium, WebKit) as parallel jobs.

### Fixed
- The agent opens the web address it was given, not the page the browser kept.
- An Android app opened by name is the app, not a system component.
- A search for an option stops when its time is up, even mid-swipe.
- A takeover is not started after you have left the task page.

### Upgrading
Back up first, then run the new migrations `0037`–`0041`. Rebuild the web app. If you proxy the API yourself, allow 101 MiB request bodies on `/api/v1/files` (see `deploy/nginx-locations.inc`). Update `cloud_url` in `gateway.toml` if you used the old dev port 8000.

## 2026-10-25
- The agent answers in the language of your newest message, and opens an app by its name in one input.
- Typing never goes into a password field the focus moved to after the check; a person taking over can still type into one.
- An input that failed before anything was sent is returned instead of becoming UNKNOWN.
- A message to a paused task resumes it in the same write; the API refuses to start unless `WITX_AUTH` is set.
- The web app gets a CSP and HSTS; dialogs keep keyboard focus; every browser flow is tested in WebKit too.
- Getting-started guides rewritten in English and Chinese.

## 2026-10-23
- An approved input is delivered only with the parameters it was approved for.
- No input starts after a stop or lease change that arrived while it was being checked; an unexpected error during the check rejects it.
- A run whose steps keep ending unfinished waits for you instead of paying for more retries.
- Every table with a tenant column is covered by row-level security, with a test.

## 2026-10-22
- Workspace changes are pushed to open pages instead of polled.
- Long tasks open at their latest rounds and load earlier ones as you scroll up; task lists page by cursor.
- Pages and screenshots load on demand and are compressed.

## 2026-10-21
- First public developer preview under Apache-2.0, with `witx-preview` as the built-in plugin marketplace.
- Skills: draft them with `/skill` (several at once), try them in a plan-mode task, save, edit and restore your own, name one with `$name`; install Plugins from the web.
- iOS text input by keys or paste, replacing a field's text, pinch to zoom.
- Purging a deleted task clears every screenshot; alerts when the screenshot sweep stops.
