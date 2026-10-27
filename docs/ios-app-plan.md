# iOS app plan

**Status: shelved (2026-10-27).** Nothing has been built yet: there is no `ios/` folder, no branch and no workflow. This document records the plan and the decisions made so far, so work can pick up where it stopped.

## Goal

A native SwiftUI iPhone app, installed through TestFlight, that replaces the web page on the phone. The app is only a client: the Mac keeps running the whole pipeline, the database and the daily run.

```
iPhone app (TestFlight) ──HTTPS──▶ reverse proxy (Traefik + Authelia) ──▶ Mac: FastAPI (unchanged pipeline, DB, scheduler)
```

### Why the pipeline stays on the Mac

- **Models:** Parakeet MLX, Whisper and the Ollama model already strain a 16 GB Mac. iOS gives an app roughly 3 to 6 GB and kills it beyond that.
- **Background limits:** iOS won't let an app transcribe an hour of audio while it's closed. The daily run and the serial job queue need a machine that's always on.
- **Tools:** yt-dlp, ffmpeg and DuckDB don't fit an iOS app, and downloading YouTube audio would cause trouble at App Store review.
- **Shared processing:** each episode is processed once for every user. Running the pipeline on each phone would throw that away.

On-device processing (Apple's SpeechAnalyzer and Foundation Models) was considered and rejected: the on-device model's context, about 4K tokens, is far too small to digest a podcast transcript.

## Decisions made

| Topic | Decision |
|---|---|
| Kind of app | Native SwiftUI, not a web wrapper (see [Options considered](#options-considered)) |
| Sign-in | Authelia OIDC (authorization code + PKCE) with bearer tokens accepted by forward-auth, so P3 keeps trusting `Remote-User` unchanged (route A below) |
| Minimum iOS | iOS 26 |
| Code location | `ios/` in this repo, so an API change and the matching client change land in one commit |
| Push notifications | Deferred to a later phase |
| Tooling | No local Xcode. Build, sign and upload to TestFlight only through GitHub Actions macOS runners |
| Private details | The server hostname, team ID and bundle ID prefix never go into this public repo. The app asks for the server URL at first sign-in; identifiers live in GitHub secrets and variables |

## App structure

- **Shared code:** a Swift package, `ios/P3Kit`, holding the API client, models, sign-in and offline cache. Most logic lives here as plain Swift with unit tests, to keep the slow TestFlight loop rare.
- **Targets:** the `P3` app, a `P3Share` share extension, and later widgets.
- **Project file:** `ios/project.yml`, read by XcodeGen. CI generates the `.xcodeproj` on every run; it is never committed.
- **Identifiers:** a gitignored `Local.xcconfig`, with a committed template; in CI they come from repo secrets and variables.

## Phases

### Phase 1: Sign-in through Authelia (do first, as a test)

This is the only part that might not work as hoped: Authelia's forward-auth is built around browser sessions.

- **A. OIDC with bearer tokens (chosen).**
  - Register P3 as a public OIDC client in Authelia: PKCE (S256), redirect `p3://oauth/callback`, grant types `authorization_code` and `refresh_token`, scopes `openid profile email groups offline_access authelia.bearer.authz`, two-factor policy, audience set to the P3 URL.
  - Add `Bearer` to the `HeaderAuthorization` schemes of the forward-auth endpoint, keeping `Basic` and `CookieSession`.
  - The app signs in through `ASWebAuthenticationSession`, which shows Authelia's real login page, 2FA included. Tokens go in the Keychain and refresh automatically.
  - Authelia still sets `Remote-User`, so P3's server code and trust model don't change.
  - Short access tokens, long refresh tokens (30 to 90 days), and pre-configured consent so there's no consent screen at every login, if Authelia allows that for public clients.
- **B. Fallback: copy the browser session cookie.** Log in once in an in-app web view and copy the `authelia_session` cookie into the app's requests. No Authelia change, but the session lasts only as long as Authelia's expiry and inactivity settings, and an expired session comes back as a redirect to the login page, not a 401.

**Watch out for:** if an `access_control` rule bypasses authentication for the home network, a phone on home Wi-Fi may reach P3 without `Remote-User` even with a valid bearer token. Check this and adjust the rule (without breaking browser use at home) before relying on the app at home.

**Test without an app first:** run a manual authorization code + PKCE flow (log in in a browser, exchange the code with `curl`), then check that `curl -H "Authorization: Bearer …" https://<p3 host>/api/me` returns the user, that the same call without the header is refused, and that a refresh-token exchange works.

**Done when:** a throwaway app on the phone gets the user from `GET /api/me` over cellular, and still does after a token refresh.

**State on 2026-10-27:** Authelia 4.39.20, OIDC provider not enabled yet (the discovery document returns 404), `HeaderAuthorization` accepts only `Basic`. The detailed brief for the Authelia setup, with the host-specific facts, is kept outside the repo.

### Phase 2: Server prep (small Python changes, tested like the rest)

1. **Type the action responses.** About 25 endpoints return a bare `dict` such as `{"job_id": …}`. Give them response models (e.g. `JobStarted`) so FastAPI's OpenAPI description is complete.
2. **Stable operation IDs**, so generated Swift method names are readable.
3. **`scripts/export_openapi.py`** writes `ios/P3Kit/openapi.json`. A pytest test checks that the committed file matches `app.openapi()`, so CI fails when the API changes without the client being regenerated.
4. **`api_version` in `/api/me`**, so an old TestFlight build can say "update the app" instead of failing to decode a response.
5. **Optional: shared Apple Podcasts episode links.** They carry the episode in `?i=…`; the resolver ignores it today and only finds the show. Pre-selecting that episode in the preview would make sharing from the Podcasts app pick the right one.

None of this needs Apple or Authelia, so it can start any time.

### Phase 3: App skeleton

- **API client:** generated from `openapi.json` with Apple's Swift OpenAPI Generator. A middleware adds the bearer token and refreshes it on a 401.
- **First launch:** enter the server URL, sign in, load `/api/me` into an `@Observable` model (user, role, language), playing the role `MeContext` plays in the web app.
- **Tabs:** Today, Library, Content, Search, Settings.
- **CI:** the simulator build workflow (see [Build and release](#build-and-release-without-xcode)), which works without signing.

**Done when:** it runs on the phone and shows the user's name and language.

### Phase 4: Reading MVP, then the first TestFlight build

- **Today:** the last daily run card, recent digests and active jobs.
- **Library:** podcast list → podcast → episode. The episode screen shows the summary, themes, quotes, synopsis and transcript, with the EN/FR toggle defaulting to the user's language. The transcript needs a lazy list, since some run to thousands of segments.
- **Content:** LinkedIn drafts first, blog posts in a second tab, with Copy and Share buttons (sharing a draft straight to the LinkedIn app).
- **Search.**
- **Offline cache:** SwiftData keeps whatever has been opened; cached text shows at once and refreshes in the background.

Ship TestFlight build 1 here, so the release process is proven early.

### Phase 5: Actions

- **Add podcast:** paste a URL → `/api/podcasts/preview` → pick episodes (a port of `EpisodePicker`) → `POST /api/podcasts`.
- **Episode actions:** transcribe, digest, synopsis, full pipeline, translate, and delete with a confirmation.
- **Podcast actions:** fetch, Add Past Episodes, edit, delete.
- **Jobs:** list and retry, with a port of `useJobPoller` (an async loop on `GET /api/jobs/{id}` that runs only while the screen is visible).
- **Personal settings:** language, tone, writing model, episodes per feed, daily run time and opt-ins, email.
- **Left on the web:** Admin, shared settings, setup, OPML import and exports. Settings gets an "Open web app" link.

### Phase 6: Share extension

- Appears in the share sheet of Podcasts, YouTube and Safari for a single web link. Those links (`youtu.be/…?si=…`, `podcasts.apple.com/…id…`) already resolve on the server.
- Flow: preview → compact picker → add.
- The extension is a separate process: it reads the token from a shared Keychain access group, and if the token has expired and can't refresh, says "Open P3 to sign in".
- Extensions get about 120 MB, plenty for JSON, but the extension must not load the whole app.

### Phase 7: Push notifications (deferred)

- **Server:** a `user_devices` table with `POST` / `DELETE /api/me/devices`; a new `p3/push.py` that sends through APNs with a `.p8` signing key (gitignored like `smtp_password.txt`) over HTTP/2 (`httpx[http2]`), outbound only. `daily_finish` sends "Daily run done: N new episodes" next to the email, following the mailer's rule: never raise, keep any error in `app_state`. A per-user `daily_run_push` opt-in. Tests with a mocked sender, like `test_mailer.py`.
- **App:** register for notifications, upload the device token, open the right screen on tap.
- TestFlight builds use APNs production, not the sandbox.

### Later

A widget with today's digest headline, a Live Activity showing the daily run's progress, and an App Intent so Siri can add a podcast.

## Build and release without Xcode

GitHub's macOS runners come with Xcode, and standard runners are free for this public repo.

- **Signing:** an App Store Connect API key stored as GitHub secrets. `xcodebuild -allowProvisioningUpdates` with that key creates and manages certificates and provisioning profiles, so there are no certificate files to handle.
- **Upload:** `xcodebuild -exportArchive` with `destination: upload` in the export options sends the build straight to App Store Connect. The build number comes from `github.run_number`.
- **Two workflows:**
  - On every push or PR touching `ios/`: build and run the tests on an iPhone simulator, and save screenshots of key screens as artifacts, so the UI can be checked without a phone.
  - On a push to `main` (or a manual run): archive, sign and upload to TestFlight. Never on a PR, so secrets can't leak to forks.
- Use the current Xcode 26 runner image.
- **Info.plist:** `ITSAppUsesNonExemptEncryption = NO`, to skip the export-compliance question on every build.

### Costs of skipping Xcode

- **Slow feedback:** push → about 10 minutes of CI → 5 to 30 minutes of Apple processing → install from TestFlight. No live previews, no debugger.
- **TestFlight is the only way onto the phone**, including for the Phase 1 sign-in test app. Internal testing needs no review, so it's only the processing wait.
- **Public logs:** anyone can read workflow logs in a public repo. Secrets are masked, but logs must not print anything personal.

## One-time Apple setup (in the browser)

1. Join the Apple Developer Program ($99/yr).
2. On developer.apple.com, register the bundle ID with App Groups (needed by the share extension), Keychain Sharing, and later Push.
3. In App Store Connect, create the app record, an internal testing group (up to 100 team members, no review), and an API key with the Admin role, which automatic signing needs. Download the `.p8`; Apple allows that only once.
4. In the GitHub repo, add four secrets (key ID, issuer ID, `.p8` contents, team ID) and a repo variable for the bundle ID prefix.
5. Install TestFlight on the phone.

TestFlight builds expire after 90 days, so a new one has to be uploaded at least that often.

## Options considered

- **Wrap the React app (Capacitor):** days of work and reuses `frontend/` almost as is (its web view sends `Origin: capacitor://localhost`, which would need allowing in `main.py`). Rejected: a thin wrapper adds little over the web page, and Apple often rejects website wrappers at review (guideline 4.2), which also applies to external TestFlight testing.
- **Add the site to the home screen from Safari:** free, but no share extension, offline reading or notifications.
- **P3's built-in sign-in (`P3_AUTH=local`):** a native app could use its cookie as is, but the server is reached through Authelia, so sign-in has to go through Authelia.

## Next steps when resuming

1. **Owner:** do the one-time Apple setup.
2. **Authelia:** enable the OIDC provider, register the `p3-ios` client, allow `Bearer` on forward-auth, and run the manual end-to-end test from Phase 1.
3. **In parallel (no Apple account needed):** Phase 2 server prep, then the `ios/` skeleton and the simulator workflow. Turn on the TestFlight upload workflow once the secrets are in.
