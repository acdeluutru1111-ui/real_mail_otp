# Auto-Cookie Acquisition with Integrity Token and Dual Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `CookieManager` to acquire Ghost CMS `integrityToken`, send magic link with OTC support, parse both sign-in link and 6-digit OTC code from Gmail, authenticate with automatic fallback to OTC verification, and forward session cookies to extract SmailPro credentials.

**Architecture:** Single `httpx.AsyncClient` lifecycle during `_execute_cookie_flow` preserving cookies and headers across Sonjj, MySonjj, and SmailPro. IMAP polling returns both sign-in link and OTC code. Dual-branch authentication tries Direct Sign-in link first, falling back to `/members/api/verify-otc/` using fresh integrity token and `otc_ref`.

**Tech Stack:** Python 3.12, `httpx`, `asyncio`, `imaplib`, `pytest`, `respx`.

## Global Constraints

- Never log or expose secrets (tokens, passwords, URLs, OTPs, cookies) in logs, exceptions, or telemetry.
- Telemetry events must strictly conform to allowed stages (`magic_link_request`, `imap_connect`, `imap_auth`, `imap_poll`, `signin`, `session`, `sso`, `smailpro_cookie`) and allowed reason codes.
- Maintain single-flight locking via `asyncio.Lock()` and generation awareness.
- Persistence is memory-only (`cookie_persistence=none`) by default.
- All network requests use bounded timeouts (15.0s HTTP, configurable IMAP timeout).

---

### Task 1: Add Integrity Token Acquisition and Enhanced Magic Link Request

**Files:**
- Modify: `backend/app/integrations/cookie_manager.py`
- Test: `backend/tests/test_auto_cookie.py`

**Interfaces:**
- Consumes: `client: httpx.AsyncClient`, `self._gmail_email`
- Produces: 
  - `_get_integrity_token(client: httpx.AsyncClient) -> str`: Calls `GET https://sonjj.com/members/api/integrity-token/` and returns token string.
  - `_request_magic_link(client: httpx.AsyncClient, integrity_token: str) -> Optional[str]`: Sends POST to `/members/api/send-magic-link/` with `integrityToken`, `emailType="signin"`, `requestSrc="portal"`, `autoRedirect=True`, `includeOTC=True`, `urlHistory=[...]`. Returns `otc_ref` if present in response.

- [ ] **Step 1: Write the failing tests for integrity token and magic link request with OTC support**

Add unit tests in `backend/tests/test_auto_cookie.py`:
- `test_get_integrity_token_success_and_failure`
- `test_request_magic_link_with_integrity_and_otc_payload`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/test_auto_cookie.py -k "integrity_token or magic_link_with_integrity"`
Expected: FAIL (AttributeError / methods not found or updated)

- [ ] **Step 3: Implement `_get_integrity_token` and update `_request_magic_link` in `CookieManager`**

Implement `_get_integrity_token` and update `_request_magic_link` with telemetry, error handling, and payload format.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/tests/test_auto_cookie.py -k "integrity_token or magic_link_with_integrity"`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add backend/app/integrations/cookie_manager.py backend/tests/test_auto_cookie.py
git commit -m "feat(cookie): implement integrity token and enhanced magic link request"
```

---

### Task 2: Update IMAP Email Parser for Dual Sign-in Link and OTC Code Extraction

**Files:**
- Modify: `backend/app/integrations/cookie_manager.py`
- Test: `backend/tests/test_auto_cookie.py`

**Interfaces:**
- Consumes: IMAP message from `imap.gmail.com` with `FROM "sonjj.com"`.
- Produces:
  - `_read_gmail_auth_sync(max_wait: float, poll_interval: float) -> Tuple[Optional[str], Optional[str]]`: Returns `(signin_url, otc_code)`.
  - `_read_gmail_auth_async(max_wait: float, poll_interval: float) -> Tuple[Optional[str], Optional[str]]`: Async wrapper around `_read_gmail_auth_sync`.

- [ ] **Step 1: Write the failing tests for dual extraction (signin link & OTC code)**

Add tests in `backend/tests/test_auto_cookie.py`:
- `test_imap_extracts_both_signin_url_and_otc_code`
- `test_imap_extracts_otc_code_only_when_url_missing`
- `test_imap_extracts_url_only_when_otc_missing`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/test_auto_cookie.py -k "imap_extracts"`
Expected: FAIL

- [ ] **Step 3: Implement `_read_gmail_auth_sync` and `_read_gmail_auth_async` in `CookieManager`**

Extract `otc_code` via regex `r'\b(\d{6})\b'` from Subject and body, alongside `signin_url`. Ensure compatibility and update legacy `_read_signin_link_sync` / `_read_signin_link_async` to use the new method.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/tests/test_auto_cookie.py -k "imap_extracts"`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add backend/app/integrations/cookie_manager.py backend/tests/test_auto_cookie.py
git commit -m "feat(cookie): add dual sign-in url and otc code extraction from imap"
```

---

### Task 3: Implement Dual Authentication (Sign-in Link Primary with OTC Verification Fallback)

**Files:**
- Modify: `backend/app/integrations/cookie_manager.py`
- Test: `backend/tests/test_auto_cookie.py`

**Interfaces:**
- Consumes: `client: httpx.AsyncClient`, `signin_url: Optional[str]`, `otc_code: Optional[str]`, `otc_ref: Optional[str]`
- Produces:
  - `_authenticate(client: httpx.AsyncClient, signin_url: Optional[str], otc_code: Optional[str], otc_ref: Optional[str]) -> None`: Attempts Branch A (signin URL), falls back to Branch B (POST `/members/api/verify-otc/` with fresh integrity token). Raises `CookieRefreshError(CookieRefreshStage.SIGNIN, CookieRefreshReason.MAGIC_LINK_INVALID)` if both fail.

- [ ] **Step 1: Write the failing tests for dual authentication flow and fallback**

Add tests in `backend/tests/test_auto_cookie.py`:
- `test_authenticate_branch_a_signin_url_success`
- `test_authenticate_branch_b_otc_fallback_success`
- `test_authenticate_fails_when_both_branches_fail`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/test_auto_cookie.py -k "test_authenticate_"`
Expected: FAIL

- [ ] **Step 3: Implement `_authenticate` and verify-otc logic in `CookieManager`**

Implement `_authenticate` handling Branch A, fallback to Branch B with fresh integrity token fetch, and proper telemetry recording.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/tests/test_auto_cookie.py -k "test_authenticate_"`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add backend/app/integrations/cookie_manager.py backend/tests/test_auto_cookie.py
git commit -m "feat(cookie): implement dual authentication with otc verification fallback"
```

---

### Task 4: Unify Single-Client Cookie Flow and End-to-End Test Suite

**Files:**
- Modify: `backend/app/integrations/cookie_manager.py`
- Test: `backend/tests/test_auto_cookie.py`

**Interfaces:**
- Consumes: `_execute_cookie_flow(max_wait: float, poll_interval: float)`
- Produces: Full end-to-end flow executing Stage 1 through Stage 6 on a single `httpx.AsyncClient` session.

- [ ] **Step 1: Write end-to-end mocked tests for both direct link and OTC fallback paths**

Add comprehensive `respx` tests in `backend/tests/test_auto_cookie.py`:
- `test_full_cookie_flow_e2e_direct_signin`
- `test_full_cookie_flow_e2e_otc_fallback`
- `test_full_cookie_flow_integrity_token_rejected`

- [ ] **Step 2: Run tests to verify they fail or need orchestration**

Run: `pytest backend/tests/test_auto_cookie.py -k "full_cookie_flow"`
Expected: FAIL

- [ ] **Step 3: Refactor `_execute_cookie_flow` and extraction methods in `CookieManager`**

Refactor `_execute_cookie_flow` to run the 6-stage lifecycle using a unified `httpx.AsyncClient`. Refactor `_extract_cookies_from_signin` / `_extract_cookies_from_session` accordingly.

- [ ] **Step 4: Run entire test suite to verify 100% pass**

Run: `pytest` in `backend`
Expected: PASS (all tests pass)

- [ ] **Step 5: Commit changes**

```bash
git add backend/app/integrations/cookie_manager.py backend/tests/test_auto_cookie.py
git commit -m "feat(cookie): unify execute_cookie_flow with full integrity and otc lifecycle"
```
