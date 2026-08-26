# PROJECT_OVERVIEW — Canonical handoff cho `real_mail_otp`

> **Vai trò tài liệu:** nguồn handoff kỹ thuật canonical cho các phiên AI/developer sau. Ưu tiên bằng chứng theo thứ tự: **runtime source hiện tại > migration/ràng buộc DB > test > authored OpenAPI > README/build plan > `reference/`**.
>
> **Nhãn:** **IMPLEMENTED** = có code/runtime artifact; **PLANNED** = ý định chưa có đủ artifact; **STALE** = mô tả không còn đúng source; **RISK** = lỗi/drift cần xử lý. “Có code” không đồng nghĩa production-ready.
>
> **Secret hygiene:** tài liệu này chỉ liệt kê *tên* biến và loại dữ liệu; không chép giá trị `.env`, cookie, token, OTP, body thư, magic link, API key hay credential.

## 1. Tóm tắt điều hành

Dự án là dịch vụ web hộp thư tạm có xác thực và tính phí khi đọc chi tiết thư. Browser chạy React SPA; FastAPI gọi SmailPro để tạo inbox/lấy payload, gọi Sonjj để list/detail, lưu dữ liệu chuẩn và sổ tiền trong PostgreSQL, dùng cache RAM + single-flight trong một replica.

### 1.1 Trạng thái đã xác minh

- **IMPLEMENTED:** FastAPI + React/Vite; 23 operation HTTP runtime; PostgreSQL/Alembic head `0005_credential_version`; 9 bảng sau migration.
- **IMPLEMENTED:** auth email/password, JWT access/refresh, DB-backed refresh rotation; inbox create/list/get/refresh/delete; list/detail message; ví/ledger; QR thủ công/proof/approve/reversal; auto-cookie qua các route admin có xác thực.
- **IMPLEMENTED:** cache RAM, negative cache, single-flight, rate limit in-process, global upstream semaphore, request ID, structured error envelope cho `AppError`, readiness `SELECT 1`.
- **IMPLEMENTED auto-cookie:** `CookieManager` singleton được khởi tạo lazy; bootstrap cookie khi cần; refresh generation-aware single-flight; SmailPro replay đúng **một lần** chỉ sau `UPSTREAM_AUTH`; mặc định chỉ giữ cookie trong memory, không ghi plaintext; tùy chọn `legacy-read-only` chỉ đọc cache cũ để migration.
- **VERIFIED (latest local validation):** full suite **47 passed**; focused auto-cookie suite **21 passed**. Frontend validation gần nhất được ghi nhận: `npm run typecheck && npm run build` **passed**; `alembic heads`: **`0005_credential_version (head)`**.
- **LIVE LIMITATION:** live smoke không tạo inbox; dừng fail-safe tại stage `magic_link_request`, reason code `upstream_rejected`; vì vậy live cookie acquisition **chưa được xác nhận**.
- **IMPLEMENTED artifact:** authored spec `backend/openapi/openapi.yaml`; FastAPI còn sinh `/openapi.json` độc lập.
- **STALE/EMPTY:** `postman/collections/`, `postman/environments/`, `postman/specs/` trống; local workspace chỉ liên kết/nhìn thấy authored OpenAPI, chưa có collection/environment regression.
- **RISK:** chưa có infra/CI/deploy/monitoring/backup artifacts thực trong repo; target trong plan là Koyeb + Neon + Cloudflare Pages.

### 1.2 P0/P1 phải biết trước khi sửa hoặc deploy

1. **P0 RISK — package credit lệch đơn vị:** `PACKAGES` trả `credits=150/350/800`, nhưng `wallet.balance_vnd` và mỗi lần đọc dùng VND (`200`). Approval cộng trực tiếp 150/350/800 vào `balance_vnd`, khiến gói không cấp 150/350/800 lượt mà chỉ cấp 0/1/4 lượt theo phép trừ 200. Phải chốt mô hình: hoặc cấp `reads * READ_PRICE_VND`, hoặc đổi toàn bộ ví sang đơn vị lượt; không vá riêng UI.
2. **P0 RISK — refresh token dùng làm access:** `get_current_user_id()` gọi `decode_token()` nhưng không bắt `claims.type == "access"`; refresh JWT hợp lệ có thể gửi trong Bearer để truy cập route bảo vệ.
3. **P0 RISK — unknown-jti fallback:** `/v1/auth/refresh` chấp nhận JWT refresh ký hợp lệ nhưng không có `jti` trong DB như “legacy”, rồi mint family mới. Điều này phá revocation/rotation assurance.
4. **P0 RISK — billing kill switch không hiệu lực:** `is_billing_enabled()` tồn tại trong config và được import, nhưng đường `MessageService.read_message_detail()` không gọi helper; `BILLING_CHARGE_ENABLED=false` vẫn có thể charge.
5. **P1 RISK — auto-cookie còn giới hạn vận hành:** các leak/expiry/concurrency cũ đã được harden: route chỉ trả metadata, mặc định memory-only, expiry dùng `timedelta`, IMAP không monkeypatch socket và telemetry chỉ dùng allowlist. Tuy vậy refresh vẫn phụ thuộc upstream + Gmail IMAP, lock chỉ trong một process, legacy plaintext cache có thể còn trên máy cũ, admin operations chưa rate-limit, và live cookie acquisition chưa được xác nhận.
6. **P1 RISK — durable create idempotency chưa wire:** bảng/repository `idempotency_keys` đã có nhưng `InboxService` vẫn dùng dict RAM, nhả lock trước upstream create; restart/multi-replica/concurrent request có thể tạo trùng.
7. **P0 RISK — frontend refresh race:** mỗi request 401 tự refresh độc lập; nhiều 401 đồng thời có thể dùng cùng refresh token, một lần rotate và các lần còn lại bị xem là replay, revoke cả family và logout phiên hợp lệ.
8. **P1 RISK — ORM/migration drift:** migration đổi FK kế toán sang `RESTRICT` và tạo partial unique index credit/payment; ORM vẫn khai báo `CASCADE` và không khai báo partial index. Autogenerate có thể đề xuất phá invariant.
9. **P1 RISK — validation/OpenAPI drift:** authored spec, FastAPI/Pydantic mặc định và runtime error không hoàn toàn đồng nhất; FastAPI validation thường trả 422 mặc định thay vì envelope ứng dụng; nullable/required/status/header/path semantics còn lệch.
10. **P1 RISK — reject payment chưa implement:** enum có `rejected`, UI/docs có thể kỳ vọng trạng thái, nhưng không có service/admin endpoint transition reject.

## 2. Mục tiêu, phạm vi và invariants

### 2.1 Mục tiêu

- User đăng ký/đăng nhập, tạo inbox tạm, poll/list thư, đọc detail đã sanitize và trích OTP.
- Chỉ charge đúng một lần cho detail hợp lệ đã commit; cung cấp ledger và top-up QR thủ công.
- Che giấu hoàn toàn upstream cookie/key/payload khỏi browser và log.
- Giữ request ngắn/stateless; polling dài ở client.

### 2.2 Trong/ngoài phạm vi

**IMPLEMENTED scope:** một provider logic SmailPro/Sonjj, một ví, QR/manual approval, một replica/cache RAM, credential auth.

**PLANNED/không có artifact:** multi-replica Redis, webhook payment/reconciliation, circuit breaker theo domain, active/standby cookie, production monitoring/alerts, cleanup scheduler, full CI/CD, legal/ToS artifacts.

**Ngoài phạm vi an toàn:** spam, chiếm tài khoản, né rate limit, truy cập mailbox không được ủy quyền, tự động hóa `wait_*`/`full_flow` trong web request.

### 2.3 Invariants không được phá

1. Không chạy wait/quick/full-flow dài trong HTTP request.
2. Không log/trả cookie, key, payload, body raw, OTP, token, magic link hay credential.
3. Billing dedupe key là `(provider, domain_type, inbox_id, mid, user_id)`, tuyệt đối không dùng payload.
4. Không charge refresh/list/poll/cache hit/reopen/error/timeout/invalid response.
5. Billing correctness dựa vào transaction + unique constraint PostgreSQL, không dựa cache/lock RAM.
6. Browser không nhận upstream secret.
7. User trả phí vẫn chịu fair-use/rate limit.
8. Ledger append-only; sửa tiền bằng reversal, không update/delete lịch sử.
9. Commit billing read, wallet debit và ledger debit cùng transaction; lỗi phải rollback tất cả.
10. Không deploy charge trước khi xác nhận đơn vị tiền/package và kill switch thực sự chặn đường charge.

## 3. Kiến trúc và dependency flow

```text
Browser / React SPA
  -> fetch client + in-memory token store
  -> FastAPI /v1
       -> auth/rate-limit/request-id dependencies
       -> domain services
          -> repositories -> AsyncSession -> PostgreSQL
          -> TTL cache RAM + asyncio single-flight
          -> SmailProAdapter -> SmailPro (create, payload)
               -> lazy CookieManager (memory-only by default)
               -> on UPSTREAM_AUTH: generation-aware refresh + exactly one replay
          -> SonjjAdapter -> Sonjj (list, detail)
          -> CookieManager -> magic link -> Gmail IMAP readonly UID/BODY.PEEK
                           -> fresh-link filter -> session/SSO -> cookie pair
```

### 3.1 Luồng dependency request

- `app.main:create_app()` đọc cached `Settings`, cấu hình logging/CORS/error handler, mount `api_router`.
- Route nhận Pydantic body/query/header, inject user/rate-limit/session.
- Service điều phối repository/cache/upstream; route không chứa business transaction chính.
- `get_session()` yield một `AsyncSession`; commit khi dependency kết thúc, rollback khi exception. Một số service tự commit/rollback sớm; commit cuối dependency sau đó là no-op nhưng làm transaction boundary khó suy luận.
- PostgreSQL là source of truth cho ownership, metadata, billing, wallet, payment, refresh token. RAM chỉ tối ưu hiệu năng.

## 4. Cây file có ý nghĩa

```text
PROJECT_OVERVIEW.md                 # tài liệu canonical này
run-demo.bat                       # launcher Windows; migrate rồi mở backend/frontend
SYSTEM_BUILD_PLAN*.md              # PLANNED/STALE, không ưu tiên hơn source
backend/
  app/main.py                      # app factory, middleware, health, startup/shutdown
  app/api/deps.py                  # Bearer auth, user status, admin, IP, rate limit
  app/api/routes/api_v1.py         # mount tất cả router dưới /v1
  app/api/routes/{auth,inboxes,messages,billing,payments,admin}.py
  app/core/config.py               # Settings/env, feature helpers
  app/core/security.py             # JWT, bcrypt, Fernet, address hash
  app/core/{errors,logging,context,rate_limit}.py
  app/db/{models,session}.py        # ORM 9 bảng + async engine/session
  app/domain/{models,policies,services}.py
  app/repositories/*.py            # SQL/data access; idempotency repo hiện chưa wire
  app/cache/{memory,singleflight}.py
  app/integrations/
    http_client.py                 # shared httpx client/semaphore/retry helpers
    smailpro.py                    # create + payload hop
    sonjj.py                       # list/detail hop
    cookie_manager.py              # lazy auto-cookie; generation single-flight; safe telemetry
    {domains,normalizers,adapters}.py
  _live_cookie_smoke.py            # live smoke chỉ in stage/status/reason/elapsed metadata
  alembic/versions/0001..0005      # schema chain authoritative
  openapi/openapi.yaml             # authored contract, có drift
  tests/contract/                  # adapter contract tests + fixtures
  tests/test_auto_cookie.py        # focused mocked acceptance tests cho auto-cookie
  requirements.txt                 # có aiofiles cho legacy read-only migration
.gitignore                         # ignore backend/.env và legacy .cookie_cache.json
frontend/
  src/router.tsx                   # SPA routes
  src/api/{client,endpoints,types}.ts
  src/auth/AuthContext.tsx
  src/security/{tokenStore,polling,SanitizedHtml}.tsx|ts
  src/hooks/usePolling.ts
  src/pages/*.tsx                  # Login/Register/Inboxes/Detail/Billing/Payments
  src/components/*.tsx
  src/lib/packages.ts              # UI pricing; cùng bug đơn vị ở backend concept
  package.json/package-lock.json
  dist/                            # build artifact local; không phải deploy config
postman/
  collections/ environments/ specs/  # hiện trống
  globals/workspace.globals.yaml      # workspace artifact; không coi là API suite
reference/
  smailpro_logic_full.py
  auto_2_cookie_sonji_logic.py     # STALE/reference-only; có thể log nhạy cảm
```

## 5. Tech stack và version

### Backend

- Python **3.11+** theo README; version runtime thực cần xác minh trên máy/deploy.
- FastAPI `>=0.109,<0.112`; Uvicorn `>=0.27,<0.30`.
- Pydantic v2 `>=2.5,<3`; pydantic-settings `>=2.1,<3`.
- SQLAlchemy async `>=2.0.25,<2.1`; asyncpg `>=0.29,<0.30`; Alembic `>=1.13,<1.14`.
- httpx `>=0.26,<0.28`; python-jose `>=3.3,<4`; cryptography `>=41,<43`.
- passlib bcrypt; `bcrypt==4.0.1` cố định do tương thích.
- pytest/pytest-asyncio/anyio/respx.

### Frontend

- React/React DOM `^18.3.1`; React Router `^6.26.2`.
- TypeScript `^5.6.2`; Vite `^5.4.8`; plugin React `^4.3.2`.
- Fetch native; không có state/query library ngoài React context/hooks.

### Data/deploy target

- PostgreSQL có `pgcrypto`; plan nhắm Neon.
- Plan nhắm FastAPI một replica trên Koyeb và SPA trên Cloudflare Pages.
- **RISK:** repo không có Dockerfile, Koyeb/Pages config, IaC, CI workflow hoặc production manifest; `dist/` chỉ chứng minh local build từng chạy.

## 6. Setup, run, test, build, migration

### 6.1 Backend local

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Điền secret cục bộ; không commit/không paste vào chat hoặc log.
alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Yêu cầu PostgreSQL thật với async DSN; launcher tuyên bố không hỗ trợ SQLite. Docs runtime: `/docs`; live schema: `/openapi.json`; health: `/health/live`, `/health/ready`.

### 6.2 Frontend local

```powershell
cd frontend
npm install
# Tạo/cấu hình .env cục bộ với VITE_API_BASE_URL, không commit.
npm run dev
npm run typecheck
npm run build
npm run preview
```

Mặc định Vite port 5173. Nếu base URL rỗng, client dùng same-origin; local tách port cần cấu hình backend URL hoặc proxy (không thấy proxy được mô tả là canonical).

### 6.3 Launcher Windows

`run-demo.bat` kiểm Python/Node/npm, cài Python package vào system Python nếu thiếu, copy `.env.example`, migrate, cài npm nếu chưa có, **kill process giữ port 8000/5173**, rồi mở hai cửa sổ. **RISK:** script có tác động mạnh lên process/port và không dùng venv; dùng thủ công trong môi trường dev, không dùng production.

### 6.4 Migration

```powershell
cd backend
alembic current
alembic heads
alembic upgrade head
alembic downgrade -1        # chỉ khi đã đánh giá data/compatibility
alembic revision --autogenerate -m "..."  # KHÔNG tin output mù vì ORM drift
```

Head đã biết: `0005_credential_version`. Trước migration mới phải diff ORM với DB thật và giữ partial index/FK RESTRICT.

### 6.5 Test/build đã biết

```powershell
cd backend
python -m pytest -q
python -m pytest -v
python -m pytest tests/contract/test_adapters_contract.py

cd ..\frontend
npm run typecheck
npm run build
```

- Latest backend validation: full suite **47 passed**; focused `tests/test_auto_cookie.py` **21 passed**.
- Focused suite chứng minh lazy bootstrap, generation-aware single-flight, expiry qua ngày bằng `timedelta`, không ghi plaintext mặc định, IMAP readonly + UID + `BODY.PEEK[]` + fresh-link filter, telemetry allowlist, metadata-only admin response, validated bounds, không còn dev no-auth routes, và replay đúng một lần chỉ sau auth failure.
- Frontend validation gần nhất được ghi nhận: `npm run typecheck && npm run build` **passed**; `alembic heads` trả `0005_credential_version (head)`.
- **LIMITATION:** live smoke fail-safe tại `magic_link_request` / `upstream_rejected`, không tạo inbox; live cookie acquisition chưa được xác nhận.
- **RISK:** chưa thấy PostgreSQL integration/concurrency đầy đủ hoặc frontend component/integration/E2E; không có coverage command/config/report, nên pass count không suy ra % coverage.

## 7. Toàn bộ environment variables

> Không chép giá trị `.env`. Dùng secret manager trên deploy; `.env` chỉ local.

| Biến | Secret | Vai trò / mặc định source |
|---|---:|---|
| `SERVICE_NAME` | không | tên service; mặc định `real-mail-otp-backend` |
| `SERVICE_VERSION` | không | version log/OpenAPI; mặc định `0.1.0` |
| `ENVIRONMENT` | không | development/staging/production; ảnh hưởng dev endpoint/fail-safe helper |
| `DEBUG` | không | debug + SQL echo; phải false production |
| `DATABASE_URL` | **có** | async PostgreSQL DSN; bắt buộc runtime hữu ích |
| `JWT_SECRET` | **có** | ký JWT HS256 |
| `JWT_ALGORITHM` | cấu hình nhạy | khai báo algorithm nhưng `security.py` vẫn hardcode HS256; drift |
| `JWT_ACCESS_TTL_SECONDS` | không | access TTL; mặc định 900 |
| `JWT_REFRESH_TTL_SECONDS` | không | refresh TTL; mặc định 1209600 |
| `ENCRYPTION_KEY` | **có** | dẫn xuất Fernet key cho address/key inbox |
| `SMAILPRO_COOKIES` | **có** | JSON cookie set upstream; không log/preview/persist plaintext |
| `CORS_ORIGINS` | không | CSV hoặc JSON array allowlist |
| `RATE_LIMIT_CREATE_PER_MINUTE` | không | create/auth-create class |
| `RATE_LIMIT_LIST_PER_MINUTE` | không | list class |
| `RATE_LIMIT_DETAIL_PER_MINUTE` | không | detail class |
| `RATE_LIMIT_REFRESH_PER_MINUTE` | không | refresh class |
| `MAX_ACTIVE_INBOXES_PER_USER` | không | fair-use quota |
| `ADMIN_USER_IDS` | nhạy vừa | CSV UUID admin; quyền config-based, không role table |
| `UPSTREAM_MAX_CONCURRENCY` | không | semaphore/pool per replica |
| `TRUSTED_PROXIES` | không | CSV/JSON CIDR/IP được tin cho XFF |
| `UPSTREAM_TIMEOUT` | không | timeout/budget cơ sở |
| `UPSTREAM_CONNECT_TIMEOUT` | không | connect/write/pool timeout |
| `UPSTREAM_READ_TIMEOUT` | không | read timeout |
| `UPSTREAM_MAX_RETRIES` | không | retry count |
| `CACHE_LIST_TTL` | không | list cache TTL |
| `CACHE_LIST_NEGATIVE_TTL` | không | empty-list negative TTL |
| `CACHE_PAYLOAD_TTL` | không | payload TTL helper; hiện payload cache chưa wire đầy đủ |
| `CACHE_DETAIL_TTL` | không | sanitized detail TTL |
| `READ_PRICE_VND` | không | giá detail; mặc định 200 |
| `GMAIL_EMAIL` | **có/PII** | mailbox cho magic-link cookie refresh; chỉ ghi tên biến, không ghi giá trị |
| `GMAIL_APP_PASSWORD` | **có** | app password IMAP; chỉ ghi tên biến, không ghi giá trị |
| `COOKIE_AUTO_REFRESH_ENABLED` | không | bật lazy bootstrap/auth recovery |
| `COOKIE_BOOTSTRAP_ON_STARTUP` | không | hiện mặc định false; runtime dùng lazy bootstrap |
| `COOKIE_PERSISTENCE` | không | `none` mặc định hoặc `legacy-read-only`; không có writer plaintext |
| `COOKIE_TTL_HOURS` | không | TTL cookie, validated `(0,168]`; expiry dùng `timedelta` |
| `COOKIE_REFRESH_COOLDOWN_SECONDS` | không | cooldown validated `[0,3600]` |
| `COOKIE_REFRESH_MAX_WAIT_SECONDS` | không | wait bound validated `(0,600]` |
| `COOKIE_REFRESH_POLL_INTERVAL_SECONDS` | không | poll bound `(0,60]` và không vượt max wait |
| `COOKIE_IMAP_TIMEOUT_SECONDS` | không | IMAP timeout validated `(0,60]` |
| `BILLING_CHARGE_ENABLED` | không | kill switch được khai báo nhưng **không được gọi ở charge path** |
| `PAYMENT_APPROVAL_ENABLED` | không | admin approve kill switch; có gọi |
| `VITE_API_BASE_URL` | không | frontend backend origin, bỏ trailing slash |

**Env status:** `backend/.env.example` đã liệt kê các tên biến Gmail/auto-cookie với placeholder, không ghi giá trị thật. Source `Settings` là runtime truth cho bounds/default. `.gitignore` loại `backend/.env` và legacy `backend/.cookie_cache.json`; frontend env local cũng không được đưa vào overview ngoài tên biến.

## 8. Startup, lifecycle, session và transaction

### 8.1 Startup/shutdown

- Import `app.main` tạo app ngay; router import fail thì boot fail-fast.
- Startup: nếu có `DATABASE_URL`, khởi tạo engine/sessionmaker lazy; không chạy migration tự động.
- Shutdown: đóng shared httpx client và dispose SQLAlchemy engine.
- Engine: pool size 5, overflow 10, pre-ping, recycle 1800s.
- Readiness: yêu cầu DB URL/JWT secret và `SELECT 1` trong 2s; không kiểm migration head hoặc upstream.

### 8.2 Session/transaction

- Mặc định request = một AsyncSession; dependency commit success/rollback exception.
- Detail charge tự commit trước khi trả và tự rollback khi lỗi.
- Payment create/proof/approve/reverse cũng commit trong service.
- **RISK:** mixed transaction ownership (dependency + service) dễ tạo commit sớm; khi refactor phải định nghĩa transaction boundary rõ.

### 8.3 Auth trace

1. Register normalize email, giới hạn password 72 byte, unique precheck, bcrypt, tạo user + wallet 0, mint token pair và lưu hash refresh.
2. Login không phân biệt email sai/password sai; kiểm user active.
3. Access dependency parse Bearer, decode signature/expiry, lấy `sub`; `get_current_user` load DB và kiểm active.
4. **P0:** access dependency không kiểm JWT `type`.
5. Refresh kiểm `type=refresh`, lấy `jti/fid`, lookup DB, detect revoked reuse rồi revoke family, kiểm user, revoke old, mint new cùng family.
6. **P0:** record không tồn tại được fallback mint family mới; phải fail closed sau migration/cutover.
7. Logout chỉ revoke refresh jti; access token tiếp tục sống tới expiry. Frontend logout hiện chỉ clear local tokens, không gọi backend logout.

## 9. Database canonical: 9 bảng, relationships, constraints, indexes

### 9.1 Migration chain

1. `0001_initial`: 7 bảng, enums, pgcrypto, unique/index/check/FK cơ bản.
2. `0002_add_user_auth`: thêm `users.email/password_hash`, backfill placeholder, unique email.
3. `0003_billing_and_idempotency`: partial unique ledger credit/payment, payment check/snapshot/audit, FK kế toán RESTRICT, bảng idempotency.
4. `0004_refresh_tokens_and_proofs`: refresh token table và proof columns.
5. `0005_credential_version`: thêm inbox credential version.

### 9.2 Bảng và quan hệ

1. `users`: UUID PK; email unique/index/not-null; password hash; status enum; created. Parent của wallet/inbox/ledger/billing/payment/idempotency/refresh.
2. `wallets`: `user_id` PK/FK user; `balance_vnd >= 0`; version. Một ví/user.
3. `ledger_entries`: UUID; user FK; type credit/debit/reversal; signed `amount_vnd`; reference; created. Index user + created desc. Migration FK user **RESTRICT**.
4. `inboxes`: owner FK; provider/domain; address hash; address/key encrypted; timestamp; credential_version; lifecycle timestamps/status. Index owner/status/created và expiry.
5. `messages`: inbox FK; mid; sanitized subject/sender; received/discovered. Unique `(inbox_id,mid)`; index inbox/received. Không lưu body DB.
6. `billing_reads`: user/inbox FK; provider/domain/mid/amount/source. Unique dedupe `(provider,domain_type,inbox_id,mid,user_id)`.
7. `payments`: user FK; provider/ref unique; package/amount; credited snapshot/admin/reason; proof fields; status/times. Check amount > 0. Migration FK user **RESTRICT**.
8. `idempotency_keys`: user/op/key unique; fingerprint; status; resource/summary; expiry index. **Chưa được InboxService dùng.**
9. `refresh_tokens`: user FK; jti unique/index; token hash; family index; revoked/created/expires; user-created và expiry indexes.

### 9.3 Constraint/index drift nguy hiểm

- Migration có partial unique `uq_ledger_credit_per_payment` trên `(reference_type,reference_id)` khi payment credit; ORM `LedgerEntry.__table_args__` không có.
- Migration đổi `ledger_entries.user_id` và `payments.user_id` sang RESTRICT; ORM vẫn `ondelete="CASCADE"`.
- Reversal idempotency chỉ check-then-insert, không có unique DB cho `reversal_payment`; concurrent reversal vẫn có race dù payment row lock giảm rủi ro trong cùng payment.
- `approved_by` là UUID nhưng không FK tới users/admin.
- Không có DB CHECK đảm bảo sign ledger theo type hoặc `credited_vnd > 0`.

## 10. Catalog 25 runtime operations

### Quy ước chung

- Auth route bảo vệ dùng `Authorization: Bearer` trừ health/auth/dev cookie endpoints.
- `AppError`: `{error:{code,message,retryable},request_id}` + `X-Request-ID`; 429 có `Retry-After`.
- **RISK:** FastAPI/Pydantic `RequestValidationError` chưa có custom handler nên có thể trả shape mặc định.
- Không gửi/ghi mẫu token, cookie, OTP, body.

### Health (2)

| Method/path | Auth | Chính |
|---|---|---|
| `GET /health/live` | no | 200 `{status:"live"}` |
| `GET /health/ready` | no | 200 ready khi config+DB OK; 503 với checks an toàn |

### Auth (4)

| Method/path | Body | Response/status chính |
|---|---|---|
| `POST /v1/auth/register` | email,password | 201 token pair; 409 duplicate; 422; 429 |
| `POST /v1/auth/login` | email,password | 200 token pair; 401 bad credentials; 403 inactive; 429 |
| `POST /v1/auth/refresh` | refresh_token | 200 rotated pair; 401 invalid/replay; 403 inactive; **unknown-jti fallback risk** |
| `POST /v1/auth/logout` | refresh_token | 200 idempotent message; invalid token cũng success |

### Inbox (5)

| Method/path | Body/query | Response/status chính |
|---|---|---|
| `POST /v1/inboxes` | `{domain}`; optional `Idempotency-Key` max 100 | 201 Inbox; 401/409/422/429/502/503/504; idempotency hiện RAM |
| `GET /v1/inboxes` | cursor, limit 1..100 | 200 `{items,next_cursor}` |
| `GET /v1/inboxes/{inbox_id}` | path | 200 Inbox; missing/not-owned 404 |
| `POST /v1/inboxes/{inbox_id}/refresh` | none | 200 `{messages,next_poll_after_seconds,refreshed_at}`; không charge; route không thực trả 202 hiện tại |
| `DELETE /v1/inboxes/{inbox_id}` | none | 204 soft-delete; ownership 404 |

### Message (2)

| Method/path | Query/body | Response/status chính |
|---|---|---|
| `GET /v1/inboxes/{id}/messages` | cursor nhận nhưng service không dùng; limit 1..100 | 200 `{items}`; no charge |
| `GET /v1/inboxes/{id}/messages/{mid}` | none | 200 detail sanitized + billing; 402 thiếu tiền; upstream/rate/auth errors; unknown mid hiện 422 thay vì 404 |

### Billing (2)

| Method/path | Response |
|---|---|
| `GET /v1/billing/balance` | `{balance_vnd}` |
| `GET /v1/billing/ledger` | `{items,next_cursor}`, limit 1..100 |

**RISK:** billing routes phụ thuộc `get_current_user_id`, không load/check trạng thái user như `get_current_user`; suspended user có access JWT còn hạn có thể gọi.

### Payment user (3)

| Method/path | Body | Response/status chính |
|---|---|---|
| `POST /v1/payments/qr` | package_code hoặc positive amount_vnd | 201 Payment + `qr_content`; QR chỉ string nội bộ, chưa phải VietQR chuẩn |
| `POST /v1/payments/{id}/manual-proof` | optional note<=1000, reference<=500 | 200, status pending_review; chỉ metadata text, không upload ảnh |
| `GET /v1/payments/{id}` | none | 200 Payment; not-owned/malformed 404 |

### Admin/payment/cookie (7)

| Method/path | Auth/body | Ghi chú |
|---|---|---|
| `POST /v1/admin/payments/{id}/approve` | admin; optional reason | lock payment+wallet, credit+ledger+paid; payment kill switch có hiệu lực; **package unit bug** |
| `POST /v1/admin/payments/{id}/reverse` | admin; required reason | ledger reversal, debit wallet; không đổi payment status |
| `POST /v1/admin/refresh-cookies` | admin; force/max_wait/poll_interval | magic-link/IMAP flow; **P0 cookie risk** |
| `GET /v1/admin/cookies/status` | admin | **RISK:** trả masked previews, vẫn là secret exposure |
| `DELETE /v1/admin/cookies/clear` | admin | clear memory/file cache |
| `POST /v1/admin/dev/refresh-cookies` | no auth nhưng chỉ ENV=development | **RISK:** dev config sai sẽ expose high-impact operation |
| `GET /v1/admin/dev/cookies/status` | no auth nhưng chỉ ENV=development | **RISK:** trả preview không auth trong dev |

**Không tồn tại:** payment reject endpoint/transition; admin list review queue; revoke-all sessions; cleanup endpoints/jobs.

## 11. Truy vết các flow nghiệp vụ

### 11.1 Create inbox

Route -> active user/rate limit -> `InboxService` -> RAM idempotency lookup -> quota -> SmailPro create -> derive domain type từ address trả về -> hash + Fernet encrypt address/key -> insert inbox -> RAM mapping -> response decrypt address. **RISK:** durable repository không dùng; hai concurrent request có thể cùng gọi upstream.

### 11.2 Refresh/list message

- Refresh ownership -> list cache/negative cache -> single-flight theo `list:{inbox}` -> decrypt credential -> SmailPro POST lấy opaque payload -> Sonjj list -> normalize/sanitize metadata -> upsert `messages` -> trả list.
- List message chỉ đọc metadata DB, không gọi upstream và không charge.
- `credential_version`/`payload_key` helper tồn tại nhưng refresh hiện không cache payload riêng; config `CACHE_PAYLOAD_TTL` chưa tạo hiệu quả như plan.

### 11.3 Read detail/cache/single-flight/charge

1. Ownership + kiểm `mid` đã được discover trong `messages`.
2. Query `billing_reads` trước cache: cache miss không suy ra chưa charge.
3. Detail cache key `(inbox,mid)`; miss thì single-flight fetch upstream, validate, sanitize HTML/text, extract OTP/link in-memory, cache detail sanitized.
4. Insert billing read `ON CONFLICT DO NOTHING`.
5. Nếu insert thắng: lock wallet `FOR UPDATE`, kiểm đủ, debit, append ledger, commit rồi trả `charged=true`.
6. Nếu conflict/reopen: không debit, trả `charged=false`.
7. Exception rollback insert/debit/ledger.
8. **P0:** helper billing kill switch không được dùng; cache hit trước lần charge vẫn đi qua charge path (đúng), nhưng “no charge on cache hit” trong plan diễn đạt mâu thuẫn với first successful read từ cached detail—validator phải chốt semantics.

### 11.4 Billing/ledger/package

- Wallet tên và UI biểu diễn VND; read debit `READ_PRICE_VND`.
- Pay-as-you-go cộng amount VND, phù hợp mô hình VND.
- Package hiện trả số *lượt* nhưng cộng như VND. Fix phải có migration/compatibility cho payment cũ và snapshot `credited_vnd`.
- Ledger signed: credit dương, debit/reversal âm. Không sửa entry cũ.

### 11.5 QR/proof/approve/reversal/reject

- QR create tạo `provider_ref` unique và string `VIETQR|amount=...|memo=...`; **không phải chuẩn ngân hàng đã xác minh**.
- Proof lưu note/reference/time, status pending_review; không có file/object checksum/malware scan.
- Approve lock payment, chỉ pending_review->paid, lock/create wallet, append credit, snapshot/audit, commit; partial unique DB ngăn duplicate payment credit.
- Reverse chỉ payment paid; tìm reversal hiện có, đảm bảo wallet đủ, debit và append reversal. Không thay payment state và không có dedicated audit table.
- **RISK:** reject hoàn toàn absent dù enum/status contract đề cập.

### 11.6 Upstream và cookie refresh

- SmailPro create `GET /app/create`; payload `POST /app/inbox` với credential decrypted trong memory.
- Sonjj endpoint được map theo domain; list/detail gửi payload query param, normalize và không chủ ý log URL/body.
- Shared HTTP client trust_env=false, bounded pool/semaphore, timeout; retry helper tồn tại nhưng adapter chính phần lớn gọi `request()` hoặc custom create retry, nên “retry toàn bộ” không nên giả định.
- Cookie manager ưu tiên memory/file rồi env; auto-flow: magic link -> Gmail IMAP -> Sonjj session -> SSO -> SmailPro cookies.
- **P0:** plaintext `.cookie_cache.json`, cookie previews, response text/URLs trong logs, exception interpolation, invalid expiry arithmetic. Tắt/không expose flow trước production cho tới khi sửa và test leak.

## 12. Frontend canonical

### 12.1 Routes/components

- Public: `/login`, `/register`.
- Protected: `/inboxes`, `/inboxes/:id`, `/inboxes/:id/messages/:mid`, `/billing`, `/payments`.
- `/` và unknown redirect `/inboxes`; `Protected` redirect login khi token store trống.
- Pages dùng components InboxList, MessageList, LedgerTable, ErrorBanner, Spinner, NavBar; message HTML qua `SanitizedHtml` sandboxed iframe.

### 12.2 Token lifecycle/client

- Access/refresh chỉ ở module memory; reload tab logout; không localStorage/cookie.
- Client gắn Bearer trừ `/v1/auth/*`, parse error envelope và Retry-After.
- 401 protected -> refresh một lần -> retry request một lần -> fail thì clear tokens.
- **P0:** không có shared refresh promise/mutex; concurrent 401 gây token-family replay/revoke.
- AuthContext logout chỉ clear local, không gọi `/auth/logout`; server refresh token còn valid.

### 12.3 Polling

- Schedule canonical: `0,3,8,15,25,40,60,90,120` giây.
- `setTimeout`, một request in-flight, AbortController, pause hidden, không catch-up, tôn trọng 429/Retry-After/next poll, dừng khi có message/error/exhausted/unmount.
- **RISK:** logout không trực tiếp stop mọi poller toàn app; thường protected route unmount sẽ dừng, nhưng cần integration test.
- Payment page có polling riêng cần validator đọc để kiểm interval/backpressure/cleanup.

## 13. Cache, rate limit, retry, error, security, logging

### Cache

- RAM TTL max 10k, lazy expiry, positive + negative store.
- List TTL mặc định 5s; negative 3s; detail 180s.
- Detail chỉ cache sau validate/sanitize.
- Single-flight in-process; không bảo vệ multi-replica.
- **RISK:** payload cache helper/version có nhưng service chưa dùng.

### Rate limit

- Token bucket in-process theo user+IP+route; auth dùng anon+IP.
- XFF chỉ tin khi direct peer nằm trong `TRUSTED_PROXIES`.
- 429 AppError có Retry-After.
- **RISK:** reset khi restart, không shared replica; route get inbox/delete/admin không phải tất cả đều rate-limited; admin cookie ops không rate limit.

### Retry/timeout

- Shared httpx phase timeouts + semaphore.
- Có `request_with_retry` exponential jitter/deadline, nhưng call sites cần kiểm từng adapter; không mặc định coi mọi request dùng nó.
- Không retry auth/bad JSON; custom SmailPro create có finite attempts.

### Error/validation

- Taxonomy: auth, not-found, validation, upstream, cache, DB, billing, payment, abuse, internal.
- Unhandled 500 được mask.
- **RISK:** cookie admin dùng `HTTPException` và interpolate exception text; bypass common envelope và có thể leak.
- **RISK:** Pydantic validation mặc định drift envelope; một số malformed cursor UUID có thể còn ValueError -> 500.

### Security

- Password bcrypt + 72-byte guard; JWT HS256; Fernet derived từ SHA-256 passphrase; address SHA-256 deterministic.
- CORS allowlist; CSP/sandbox frontend; no token persistence.
- **RISK:** refresh-as-access, unknown-jti, cookie leak/plaintext, dev admin cookie endpoints, no CSRF concern hiện tại vì Bearer in memory nhưng XSS vẫn đánh cắp memory token.
- Không thấy secret scanning/rotation/audit infrastructure artifact.

### Logging

- Mục tiêu structured safe metadata/request ID; upstream client tránh URL/body.
- **RISK:** CookieManager có nhiều f-string chứa upstream response text, redirect URL, exception text; vi phạm invariant cần sửa trước bật flow.
- `reference/` có thể log nhạy cảm; không import vào production.

## 14. OpenAPI và Postman

- `backend/openapi/openapi.yaml` là authored OpenAPI 3.1, có auth/inbox/message/billing/payment/approve/reverse.
- FastAPI tự sinh `/openapi.json`; không có CI semantic diff giữa hai spec.
- Authored spec nói mọi response có request_id nhưng nhiều success body không có; runtime chủ yếu dùng `X-Request-ID` header. Cần chốt header-vs-body canonical.
- Spec chưa mô tả cookie admin/dev operations; 202 refresh được mô tả nhưng runtime route luôn await và trả 200/error.
- Nullable/required MessageMeta/Detail có thể lệch DTO thực (`None` cho subject/sender/date/html).
- Postman collection/environment/spec folders trống: **PLANNED** tạo collection từ contract sau khi chốt drift, thêm environment chỉ chứa base URL/non-secret; token dùng session/vault, không shared value.

## 15. Implemented vs planned/stale

### IMPLEMENTED đáng giữ

- Async layers rõ; DB-backed billing dedupe; wallet lock; ledger; payment lock/partial index; refresh token table/rotation; request ID/error taxonomy; readiness DB; list/detail cache/single-flight; sanitizer/poll schedule; metadata-only DB; shutdown cleanup.

### PLANNED/chưa chứng minh

- Production deploy/IaC/CI; Redis/multi-replica; full observability; cleanup; legal/abuse operations; webhook/reconciliation; backup/PITR restore test; generated SDK/client; complete Postman suite; comprehensive tests.

### STALE

- Plan v2 mô tả 7 bảng; runtime migration hiện 9.
- Fix plan nói durable idempotency đã áp dụng, nhưng chỉ schema/repository có; service chưa wire.
- Fix plan từng nói kill switch đã sửa, nhưng billing helper không được gọi.
- README nói “1 read=200 VND” đúng, nhưng package “150 reads” không đúng runtime accounting.
- Comments trong `services.py` gọi durable idempotency Phase 2 dù migration/repo đã có.

## 16. Risk register ưu tiên và bất biến khi sửa

### P0 — block production

1. Package unit bug: thêm test 19k/29k/49k cấp đúng số lượt; migrate snapshot/ledger có chủ đích; không sửa lịch sử trực tiếp.
2. Enforce `type=access` ở access dependency; test refresh token không gọi được protected route.
3. Remove unknown-jti fallback sau cutover; signed-but-untracked refresh phải 401; test replay/concurrent refresh.
4. Wire billing kill switch trước dedupe insert; định nghĩa detail behavior khi off; test flag false không tạo billing/ledger/debit.
5. Rewrite/disable CookieManager unsafe surfaces: không preview, không plaintext file, timedelta expiry, redacted errors/logs, no global socket mutation.
6. Frontend single-flight refresh promise/queue; retry tất cả waiter bằng token mới; logout/revoke đúng semantics.

### P1

- Wire `IdempotencyRepository` atomic claim/complete/fail và fingerprint; xử lý crash-after-upstream.
- Đồng bộ ORM với migrations; regression test `alembic check`/metadata diff có allowlist.
- Chốt OpenAPI/runtime; custom validation handler; add cookie ops hoặc bỏ khỏi public contract.
- Implement reject transition có audit/authorization/idempotency, hoặc loại bỏ kỳ vọng khỏi UI/spec.
- Add PostgreSQL concurrency tests cho charge/approve/reverse/idempotency/refresh.
- Billing/payment routes phải load active user, không chỉ decode subject.

### P2

- Pagination thật cho messages; expiry/cleanup jobs; admin review queue; revocation-all; payment provider chuẩn; metrics/alerts; deployment artifacts; Postman regression.

### Bất biến khi sửa

- Không đổi dedupe sang payload; không charge trước validate; không dựa lock RAM cho tiền.
- Không drop partial unique/FK RESTRICT vì ORM autogenerate.
- Không log test fixture secret/raw body.
- Không chuyển token sang localStorage để “fix reload”.
- Không cho multi-replica trước khi thay cache/rate-limit/idempotency assumptions.
- Reversal là entry mới; không mutate ledger cũ.

## 17. Playbook: làm việc gì thì đọc file nào

| Công việc | Đọc trước |
|---|---|
| Auth/access/refresh | `core/security.py`, `api/deps.py`, `routes/auth.py`, `refresh_token_repo.py`, migration 0004, frontend client/tokenStore |
| Billing detail | `domain/services.py:MessageService`, policies, billing/wallet/ledger repos, models, migrations 0001/0003 |
| Package/payment | domain models/policies/services, payment/ledger/wallet repos, routes payments/admin, `frontend/src/lib/packages.ts` |
| Inbox idempotency | InboxService, idempotency repo/model, migration 0003, create route |
| Upstream list/detail | integrations smailpro/sonjj/http_client/domains/normalizers + contract fixtures |
| Cookie refresh | cookie_manager + admin routes + config; không dùng reference làm runtime |
| DB migration | toàn bộ `alembic/versions`, ORM models/session; inspect DB thật; đặc biệt partial index/FK |
| API contract | runtime routes/DTO/errors trước, rồi authored OpenAPI, frontend types/endpoints |
| Frontend auth race | api/client, tokenStore, AuthContext, Protected, backend refresh semantics |
| Polling | security/polling, usePolling, InboxDetailPage, refresh endpoint |
| Deploy | main/config/session/http_client, env inventory, run scripts; hiện phải tạo artifact mới sau review |
| Security review | logging/errors/security/deps/cookie manager/SanitizedHtml/index CSP + secret scan |

## 18. Glossary

- **Access token:** JWT ngắn hạn dùng Bearer; phải có `type=access`.
- **Refresh family:** chuỗi refresh token rotate cùng `fid`; replay phải revoke family.
- **jti:** ID duy nhất của refresh JWT, map record DB.
- **mid:** upstream message ID, chỉ hợp lệ sau khi discover trong inbox sở hữu.
- **payload:** opaque upstream credential để Sonjj đọc; secret, không phải billing key.
- **credential_version:** version inbox credential dùng để invalidate payload cache; hiện wiring chưa đầy đủ.
- **single-flight:** gom concurrent call cùng key thành một upstream call trong process.
- **billing read:** record một lần charge theo dedupe tuple.
- **ledger:** sổ append-only tiền vào/ra/reversal.
- **proof:** note/reference manual do user gửi; không phải bằng chứng ngân hàng tự xác minh.
- **authored OpenAPI:** file YAML thủ công, khác live FastAPI schema.

## 19. Checklist phiên tiếp theo

### Trước khi code

- [ ] Đọc §1.2 và risk liên quan.
- [ ] Xác nhận runtime source/migration mới hơn tài liệu này hay không.
- [ ] Không đọc/paste `.env` values; dùng tên biến/secret manager.
- [ ] Chạy test/build baseline; ghi rõ command và DB type.
- [ ] Nếu migration: inspect partial index và FK action trước autogenerate.

### Trước khi merge

- [ ] Test invariant tiền/concurrency trên PostgreSQL thật.
- [ ] Test refresh token type/unknown-jti/replay/concurrent 401.
- [ ] Diff live OpenAPI, authored YAML, frontend types.
- [ ] Scan log/response không chứa secret/body/OTP.
- [ ] Xác minh kill switch bằng state DB trước/sau.
- [ ] Không sửa source ngoài phạm vi yêu cầu.

### Trước production

- [ ] Fix toàn bộ P0.
- [ ] Chốt đơn vị ví/package và migrate dữ liệu.
- [ ] Có CI, deploy artifact, backup/restore, monitoring/alert/runbook.
- [ ] Tắt hoặc harden cookie auto-refresh/dev endpoints.
- [ ] Postman regression collection/environment không chứa secret.
- [ ] Security/legal/upstream authorization review.

## 20. Validator cần kiểm tra tiếp

1. Chạy DB integration trên PostgreSQL sạch từ `0001 -> 0005`, inspect đúng 9 bảng/FK/index/check.
2. Chứng minh package bug bằng test approval + số detail đọc được, không dùng production data.
3. Chứng minh refresh JWT truy cập protected route và unknown-jti fallback bằng test an toàn.
4. Set billing flag false rồi xác nhận hiện vẫn charge; sau fix phải không đổi wallet/ledger/billing_reads.
5. Fuzz/error test CookieManager với giờ cuối ngày, file permission, log capture và response redaction.
6. Concurrent browser/API test nhiều 401; xác nhận family bị revoke hiện tại và regression sau fix.
7. Semantic diff `backend/openapi/openapi.yaml` với `/openapi.json` và response fixtures.
8. Coverage report theo layer; bổ sung API/DB/concurrency/frontend/E2E thay vì chỉ adapter contract.
9. Secret scan repo, đặc biệt ignored files/cache/log/reference; không in match value trong báo cáo.
10. Xác minh deployment target thực tế vì repo hiện không chứa production artifacts.

---

**Canonical rule cuối:** nếu tài liệu này mâu thuẫn với source runtime hoặc migration mới hơn, cập nhật tài liệu sau khi xác minh bằng test; không hợp thức hóa behavior nguy hiểm chỉ vì nó đang chạy.