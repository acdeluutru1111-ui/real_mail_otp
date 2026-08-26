# Real Mail OTP — Frontend SPA

A Vite + React + TypeScript single-page app for the temporary-inbox / pay-per-read
service. Implements item 8 of `SYSTEM_BUILD_PLAN_v2.md` (SPA Pages + polling scheduler).

## Setup

```bash
cd frontend
npm install
cp .env.example .env        # then edit VITE_API_BASE_URL
npm run dev                 # http://localhost:5173
```

Build & preview:

```bash
npm run build
npm run preview
```

### Environment

| Variable            | Description                                                        |
| ------------------- | ------------------------------------------------------------------ |
| `VITE_API_BASE_URL` | Base URL of the backend. Feature endpoints live under `/v1`. No trailing slash. |

## Project layout

```
frontend/
  index.html                strict CSP <meta> + app mount
  vite.config.ts / tsconfig*
  src/
    main.tsx  App.tsx  router.tsx
    api/        client.ts (fetch wrapper + ApiError), types.ts, endpoints.ts
    auth/       AuthContext.tsx
    security/   tokenStore.ts, SanitizedHtml.tsx, polling.ts
    hooks/      usePolling.ts
    pages/      Login, Register, Inboxes, InboxDetail, MessageDetail, Billing, Payments
    components/ NavBar, Protected, InboxList, MessageList, LedgerTable, ErrorBanner, Spinner
    lib/        packages.ts (pricing catalog)
    styles.css
```

## Polling schedule (plan section 8)

Mail waiting uses a fixed schedule of **elapsed seconds from the start of waiting**:

```
POLL_SCHEDULE_SECONDS = [0, 3, 8, 15, 25, 40, 60, 90, 120]   // 9 polls, last at 120s
```

Guarantees implemented in `src/security/polling.ts` (`InboxPoller`) and wired to React
via `src/hooks/usePolling.ts`:

- Each next tick is scheduled with `setTimeout` from the schedule — **never** a fixed
  `setInterval`.
- Only **one** refresh is in flight at a time; the previous request is aborted with an
  `AbortController` before a new one starts.
- Polling stops immediately when: a new/matching message arrives, the user leaves the
  inbox (unmount) or logs out, or the 120s schedule is exhausted.
- When the tab is hidden (`document.hidden`) polling **pauses**; on becoming visible it
  does **not** replay missed ticks — the schedule pointer jumps forward so at most one
  refresh is (re)scheduled.
- Server backpressure is respected: a `429`, a `Retry-After` header, or
  `next_poll_after_seconds` delays the next poll by at least that many seconds.
- Each poll calls `POST /v1/inboxes/{id}/refresh` which **never charges**.

## Security posture (plan section 13)

- **Sandboxed message rendering.** Message bodies (`html_sanitized`) are rendered ONLY by
  `src/security/SanitizedHtml.tsx` inside an `<iframe sandbox="" srcDoc=…>` — no
  `allow-scripts`, no `allow-same-origin`, plus an inner restrictive CSP. The app never
  uses `dangerouslySetInnerHTML`.
- **Strict CSP.** `index.html` ships a strict `Content-Security-Policy` `<meta>`
  (`default-src 'self'`, `script-src 'self'`, `frame-src 'none'`, etc.). The only
  documented relaxation is `style-src 'unsafe-inline'` for Vite/inline styles; it does not
  permit script execution. See the comment in `index.html`.
- **Tokens.** `access_token` / `refresh_token` are held in memory only
  (`src/security/tokenStore.ts`) — never persisted to storage/cookies and never logged.
  On a `401` the client calls `POST /v1/auth/refresh` once with the refresh token, stores
  the rotated pair, and retries the original request once; if refresh fails the tokens are
  cleared and the user is redirected to `/login`.
- `Authorization: Bearer <token>` is attached to every `/v1` call **except** `/v1/auth/*`.

## Billing & payments

- `BillingPage` shows the wallet balance and a cursor-paginated ledger.
- `PaymentsPage` lists the package table (Starter 19,000đ→150, Popular 29,000đ→350,
  Pro 49,000đ→800, plus pay-as-you-go 200đ = 1 read), creates a QR payment, submits manual
  proof, and polls payment status until a terminal state.
- Reading a message may cost 200đ; a reopen returns `billing.charged = false`
  ("Reopened (no charge)"). A `402` prompts the user to top up.
