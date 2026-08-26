# Kế hoạch xây dựng hệ thống hộp thư tạm và tính phí đọc thư — Bản tinh gọn v2

> Bản này thay thế mục tiêu triển khai của `SYSTEM_BUILD_PLAN.md`. Nguyên tắc: giữ toàn bộ *bất biến an toàn* của bản gốc, cắt hạ tầng phức tạp sang Phase 2, gộp billing về **một ví credit** và **không lưu nội dung thư** để giảm mạnh số bảng và lượng code mà vẫn đủ tính năng.

## 0. Bảy bất biến bắt buộc (giữ nguyên, không thương lượng)

1. Không chạy `wait_for_message`, `wait_for_code`, `full_flow` (hoặc quick wrapper) bên trong web request. HTTP API phải stateless, trả nhanh; polling nằm ở client.
2. Không log cookie, key, payload, request/response body hoặc OTP ở bất kỳ cấp nào.
3. Không dùng payload làm billing dedupe key.
4. Không charge cho refresh, poll, mở lại thư đã charge, cache hit, hay bất kỳ lỗi nào.
5. Billing luôn dựa trên unique constraint + transaction PostgreSQL, không dựa vào cache/lock.
6. Browser không bao giờ nhận upstream cookie/key/payload.
7. Không bypass fair-use/rate limit vì user trả phí.

## 1. Mục tiêu và phạm vi

Dịch vụ web cho người dùng đã xác thực: tạo inbox tạm, xem danh sách thư, đọc chi tiết thư theo yêu cầu, trích OTP/link xác minh, thanh toán theo credit. Hoạt động ổn định khi upstream chậm/lỗi và không thu phí sai.

Chỉ dùng cho tài khoản/địa chỉ/quy trình mà người dùng sở hữu hoặc được ủy quyền. Cấm spam, chiếm đoạt tài khoản, né rate limit, thu thập trái phép, vi phạm điều khoản upstream. Cần ToS, Privacy Policy, cơ chế báo cáo lạm dụng và khóa tài khoản tối thiểu.

Ngoài phạm vi: không tự đăng ký/chiếm tài khoản bên thứ ba; không cam kết thư tồn tại vĩnh viễn; không lộ cookie/key/payload thô cho client; không chạy job chờ dài trong HTTP request.

## 2. Mô hình giá — MỘT ví credit duy nhất

Thay cho hệ thống song song "subscription quota" + "wallet pay-as-you-go", v2 quy mọi thứ về **một ví credit** cho mỗi user.

- 1 lượt đọc thư thành công = trừ **200đ** khỏi `wallet.balance_vnd`.
- Các gói chỉ là **nạp credit có khuyến mãi** (định nghĩa trong config, không cần bảng `plans`/`subscriptions`):

| Gói | Nạp | Credit nhận | Giá thực / lượt |
|---|---:|---:|---:|
| Trả theo lượt | tùy | 200đ = 1 lượt | 200đ |
| Starter | 19.000đ | 150 lượt | ~127đ |
| Popular | 29.000đ | 350 lượt | ~83đ |
| Pro | 49.000đ | 800 lượt | ~61đ |

Lợi ích: chỉ còn **một nguồn trừ tiền** → dedupe, đối soát và runbook đơn giản hơn nhiều.

### 2.1 Định nghĩa "lượt đọc thành công"

Chỉ charge khi endpoint đọc chi tiết trả về message hợp lệ, đúng inbox, có `mid`, upstream thành công VÀ backend commit được bản ghi. KHÔNG charge cho: refresh danh sách; poll khi chưa có thư; mở lại thư đã charge; cache hit; timeout/lỗi mạng/upstream 4xx-5xx/response sai định dạng/body rỗng; client retry cùng idempotency key; transaction billing rollback.

### 2.2 Dedupe billing bắt buộc

Khóa nghiệp vụ ổn định: `provider + domain_type + inbox_id + mid + user_id`. Tạo unique constraint theo khóa này. **Không dùng payload làm khóa.**

## 3. Đối chiếu logic Python hiện có (`smailpro_logic_full.py`)

File Python chỉ là tài liệu tham chiếu; không import trực tiếp module synchronous vào route production. Có thể tái sử dụng phần mapping domain và normalizer response sau khi bọc async và loại bỏ toàn bộ log nhạy cảm.

- `create()`: `GET smailpro.com/app/create` (query `username,type,domain,server`), auth bằng cookie, chuẩn hóa về `{address, key, timestamp}`. Rủi ro gốc: retry blocking tối đa 5 lần × sleep 2s có thể giữ worker lâu → thiết kế mới dùng ngân sách thời gian hữu hạn + retry ngắn có jitter; nếu cần retry dài thì chuyển job nền. Lưu `key` mã hóa, không trả browser.
- `get_inbox()`: 2 hop tuần tự — (A) `POST smailpro.com/app/inbox` lấy `payload`; (B) chọn endpoint Sonjj theo domain rồi `GET /v1/{domain_type}/inbox?payload=...`. Chuẩn hóa `mid,subject,sender,date,snippet`. Bỏ toàn bộ log body/payload/URL.
- `get_message_detail()`: `GET /v1/{domain_type}/message?payload=...&mid=...`. Backend phải xác minh `mid` thuộc inbox của user trước khi gọi và trước billing commit.
- `wait_*` / `full_flow`: TUYỆT ĐỐI không chạy trong web request (xem bất biến 1).

## 4. Kiến trúc mục tiêu (v1: một replica)

```text
Browser (SPA)
  -> Cloudflare Pages (asset immutable, CSP nghiêm)
  -> FastAPI trên Koyeb (1 replica)
       -> Neon PostgreSQL   (nguồn chuẩn: user, ví, ledger, billing, inbox, message meta)
       -> RAM TTL cache + asyncio single-flight
       -> SmailPro / Sonjj upstream   (1 cookie set trong secret)
       -> Thanh toán QR thủ công (admin duyệt)
```

- Async endpoints; async HTTP client có connect/read/total timeout và connection pool.
- Dependency dùng chung: auth, ownership, rate limit, request_id.
- Health: `/health/live`; readiness kiểm tra DB + config (không bắt buộc upstream sống).
- **Không cần Redis/Upstash ở v1**: 1 replica dùng cache RAM + `asyncio.Lock` single-flight + rate limit in-process. Postgres vẫn là nguồn chuẩn nên billing an toàn.

## 5. Cấu trúc source đề xuất (tinh gọn)

```text
frontend/
  src/api/ src/pages/ src/components/ src/security/
backend/
  app/main.py
  app/api/routes/{auth,inboxes,messages,billing,payments,admin}.py
  app/core/{config,security,errors,logging,rate_limit}.py
  app/domain/{models,services,policies}.py
  app/integrations/{smailpro,sonjj}.py     # adapter async, không log nhạy cảm
  app/repositories/
  app/cache/{memory,singleflight}.py        # RAM only ở v1
  app/db/{session,migrations}.py
  tests/{unit,contract,integration,e2e}/
  alembic/
infra/{koyeb,cloudflare,scripts}/
docs/
```

## 6. API contract v1

Mọi response có `request_id`; lỗi theo envelope `{error:{code,message,retryable},request_id}`. Không bao giờ trả cookie/key/payload.

### 6.1 Inbox
- `POST /v1/inboxes` — body `{domain}`; `201 {id,address,domain_type,status,created_at,expires_at}`. Hỗ trợ `Idempotency-Key` (cùng key/user → cùng inbox).
- `GET /v1/inboxes?cursor=&limit=` — inbox của user.
- `GET /v1/inboxes/{id}` — metadata.
- `POST /v1/inboxes/{id}/refresh` — `200 {messages,next_poll_after_seconds,refreshed_at}`. **Không charge**; cache + single-flight; có thể trả `202` nếu đang refresh.
- `DELETE /v1/inboxes/{id}` — soft delete.

### 6.2 Message
- `GET /v1/inboxes/{id}/messages` — list metadata từ cache, **không charge**.
- `GET /v1/inboxes/{id}/messages/{mid}` — `200` detail đã sanitize + `{billing:{charged,amount,source}}`. Charge chỉ khi detail upstream hợp lệ và transaction commit. Mở lại → trả cache với `charged:false`. Không nhận payload từ client.

### 6.3 Billing / Payment (v1: QR thủ công)
- `GET /v1/billing/balance`, `GET /v1/billing/ledger?cursor=`.
- `POST /v1/payments/qr` — tạo QR đúng số tiền + reference duy nhất `{package_code|amount}`.
- `POST /v1/payments/{id}/manual-proof` — nộp bằng chứng; trạng thái `pending_review`.
- `GET /v1/payments/{id}` — `pending|pending_review|paid|rejected|expired`.

Status quan trọng: 401 chưa auth, 403 không sở hữu, 404 không tồn tại, 409 idempotency/conflict, 402 hết số dư, 429 rate limit, 502 upstream invalid, 503 upstream unavailable, 504 upstream timeout.

## 7. Schema tinh gọn (7 bảng, giảm từ ~11)

- `users(id, status, created_at, ...)`
- `wallets(user_id PK, balance_vnd, version)` — **nguồn tiền duy nhất**.
- `ledger_entries(id, user_id, type, amount_vnd, reference_type, reference_id, created_at)` — immutable, mọi cộng/trừ đều ghi ở đây.
- `inboxes(id, user_id, provider, domain_type, address_hash, address_encrypted, key_encrypted, timestamp, status, created_at, expires_at, deleted_at)`
- `messages(id, inbox_id, mid, subject_sanitized, sender_sanitized, received_at, discovered_at)` — chỉ metadata.
- `billing_reads(id, user_id, inbox_id, provider, domain_type, mid, amount_vnd, source, created_at)` — bản ghi đã charge.
- `payments(id, user_id, provider, provider_ref, package_code, amount_vnd, status, created_at, paid_at)`

**Đã bỏ so với bản gốc:** `plans`, `subscriptions` (gộp vào ví + config gói), `message_details` (không lưu body), `webhook_events` (đến Phase 2).

### 7.1 Constraints / index
- Unique `messages(inbox_id, mid)`.
- Unique billing dedupe `(provider, domain_type, inbox_id, mid, user_id)`.
- Unique `payments(provider, provider_ref)`.
- Index `(user_id,status,created_at desc)`, `(inbox_id,received_at desc)`, `(user_id,created_at desc)` cho ledger, `(expires_at)` cho cleanup.
- CHECK `balance_vnd >= 0`; foreign key đầy đủ.

### 7.2 Transaction đọc và charge
1. Kiểm tra auth/ownership/rate limit.
2. Nếu `billing_reads` đã tồn tại theo khóa dedupe → trả detail từ cache, `charged:false`.
3. Single-flight fetch detail theo `inbox_id+mid`; validate response + quan hệ message.
4. Mở DB transaction.
5. `INSERT billing_reads ... ON CONFLICT DO NOTHING RETURNING id`.
6. Chỉ khi insert thành công: `SELECT ... FOR UPDATE` wallet row, kiểm tra `balance_vnd >= 200`, trừ 200đ, ghi `ledger_entries`.
7. Thiếu số dư hoặc bất kỳ lỗi nào → rollback toàn bộ, không charge.
8. Commit rồi mới trả `charged:true`. Concurrent duplicate thấy conflict → `charged:false`.

## 8. Polling client (giữ nguyên, chống request amplification)

Lịch tính từ lúc bắt đầu chờ: **0, 3, 8, 15, 25, 40, 60, 90, 120 giây** (9 poll thay vì ~41).
- Mỗi thời điểm chỉ một request refresh.
- Dừng ngay khi có message phù hợp / user đóng inbox / logout / đến 120s.
- Tab hidden → pause; visible lại không "bù" hàng loạt, tối đa 1 refresh.
- Abort request cũ trước request mới; tôn trọng `Retry-After` / `next_poll_after_seconds` / 429.
- Multi-tab coordination (BroadcastChannel) là tùy chọn Phase 2; server vẫn là lớp bảo vệ cuối.

## 9. Cache (RAM, v1)
- **List cache**: `list:{inbox_id}`, TTL 3–8s; negative cache inbox trống 2–3s.
- **Payload cache**: `payload:{inbox_id}:{credential_version}`, TTL 15–30s; không log, không gửi client.
- **Detail cache**: `detail:{inbox_id}:{mid}`, TTL vài phút; chỉ cache sau validate; sanitize HTML trước khi trả.
- Single-flight cùng khóa để chỉ một coroutine gọi upstream.
- Cache miss KHÔNG có nghĩa là chưa charge — luôn kiểm tra `billing_reads` trong DB.

## 10. Concurrency & rate limit (in-process, v1)
- Một global semaphore giới hạn số upstream call đồng thời/replica.
- Rate limit token bucket theo user + IP + route: create thấp hơn list; detail giới hạn theo số dư.
- Quá tải → 503/429 kèm `Retry-After`, không chờ vô hạn.
- Fair-use: số inbox/phút, inbox active tối đa, refresh/phút. Circuit breaker và semaphore theo domain là Phase 2.

## 11. Cookie (v1: một bộ, xoay tay)
- Lưu 1 cookie set trong Koyeb secret, mã hóa, không để trong repo/DB plaintext.
- `credential_version` gắn với inbox để payload cache không trộn.
- Nếu cookie hỏng → trả `UPSTREAM_AUTH`/503 + cảnh báo operator; thay tay. Rotation có audit metadata (ai/khi nào/version), không lưu giá trị secret.
- Active/standby + health scoring + failover tự động là **Phase 2**.

## 12. Thanh toán (v1: QR thủ công)
- Backend sinh reference duy nhất + QR đúng số tiền/nội dung.
- User nộp bằng chứng; admin đối soát, chuyển `paid` bằng thao tác có audit.
- Cấp credit chỉ trong transaction khi chuyển `paid`; thao tác lặp không cấp hai lần.
- Không tự tin ảnh/text client gửi.
- **Webhook + reconciliation tự động là Phase 2** (xác thực chữ ký trên raw bytes, timestamp window, event ID idempotent, dead-letter, job đối soát định kỳ).

## 13. Security & retention
- TLS mọi nơi; secrets qua secret manager; least-privilege DB; rotate định kỳ.
- Auth token ngắn hạn, refresh rotation; HttpOnly/Secure/SameSite cookie; CSRF protection nếu dùng cookie auth.
- CORS allowlist đúng domain Pages; CSP; chống clickjacking; validate/normalize input.
- Sanitize HTML email, render trong sandboxed iframe; vô hiệu script/form/remote tracking image mặc định.
- Mã hóa key/address; hash để tra cứu. **Không lưu body thư** (chỉ cache RAM ngắn), tránh bài toán retention/encryption-at-rest cho nội dung.
- Metadata billing giữ theo nghĩa vụ kế toán; cleanup job idempotent.
- Anti-abuse: velocity/IP/device signals, giới hạn inbox active, CAPTCHA ở create nếu cần.

## 14. Logging, lỗi, metrics
- Structured log chỉ chứa: timestamp, level, service/version, request_id, route template, user_id (hash), inbox_id nội bộ, provider/domain_type, status, latency, cache outcome, retry count, error code. Redaction middleware drop query/body nhạy cảm trước logger.
- Error taxonomy: `AUTH_*`, `VALIDATION_*`, `UPSTREAM_AUTH/TIMEOUT/RATE_LIMIT/BAD_RESPONSE/UNAVAILABLE`, `CACHE_*`, `DB_*`, `BILLING_INSUFFICIENT`, `BILLING_CONFLICT`, `PAYMENT_*`, `ABUSE_BLOCKED`, `INTERNAL_ERROR`. Mỗi lỗi có HTTP mapping + retryable flag + message an toàn.
- Metrics/KPI: request rate/error/latency p50/p95/p99 theo route; upstream call per user action; cache hit ratio; create/detail success; billing insert/conflict/rollback + **double-charge count**; polls/inbox; 429/abuse; payment conversion; DB pool saturation.
- Alert theo SLO: tăng 5xx/upstream timeout, p95 vượt ngưỡng, **double charge > 0**, DB pool saturation, cookie auth failure. Alert kèm runbook, không chứa dữ liệu nhạy cảm.

## 15. Testing
- **Unit**: domain mapping, response normalizer, OTP extractor, cache key, polling scheduler, billing policy.
- **Contract**: fixture mọi dạng create/list/detail, malformed JSON, empty payload, missing mid, 401/429/5xx.
- **Integration DB**: migration, unique constraints, transaction rollback, concurrent detail chỉ charge một lần, hết số dư không charge.
- **Cache/single-flight**: N request đồng thời → đúng một upstream call/key.
- **API/security**: ownership/IDOR, CSRF/CORS/CSP, HTML sanitization, secret redaction, rate limit.
- **Payment**: manual approval lặp không cấp hai lần.
- **E2E**: create -> poll lịch chuẩn -> list -> detail -> reopen không charge; upstream timeout.
- CI quét secret, dependency, lint/type-check, migration dry-run, kiểm tra log fixture không chứa cookie/key/payload/body/OTP.

## 16. Lộ trình theo dependency

### Phase 1 (MVP — làm trước)
1. Chốt legal/ToS/retention và semantics billing (ví credit).
2. Định nghĩa OpenAPI, error taxonomy, schema 7 bảng + migration.
3. Dựng Neon, role least-privilege, backup/PITR, test restore.
4. Adapter async SmailPro/Sonjj + contract tests + redaction.
5. Repository/service, transaction đọc-charge, concurrency tests.
6. RAM cache + single-flight + rate limit + semaphore in-process.
7. API + auth/ownership + payment QR thủ công.
8. SPA Pages + polling scheduler `0,3,8,15,25,40,60,90,120`.
9. Deploy staging Koyeb (1 replica), E2E/security test.
10. Canary production, quan sát KPI rồi tăng traffic.

### Phase 2 (khi thực sự cần)
- Upstash Redis cho cache/lock/rate limit khi chạy multi-replica.
- Webhook thanh toán + job reconciliation.
- Active/standby cookie + health scoring + failover tự động.
- Circuit breaker + semaphore theo domain; multi-tab coordination.

## 17. Migration / rollback / backup
- Alembic forward-compatible, expand/contract; không drop/rename cột cùng release với code phụ thuộc.
- Billing unique constraint phải tồn tại **trước khi** bật charge production.
- Feature flag cho billing (kill switch), payment, cache. Rollback app trước; migration destructive chỉ sau giai đoạn tương thích.
- Khi billing sai: tắt charge bằng kill switch, vẫn cho mở lại dữ liệu đã charge, hoàn tiền bằng **ledger bù trừ** — không sửa/xóa ledger cũ.
- Neon PITR/backup; test restore định kỳ; không backup payload/cookie/body lâu hơn retention.

## 18. Bug runbook (rút gọn)
- **Charge hai lần**: bật kill switch → tìm bằng request_id + khóa `(provider,domain_type,inbox_id,mid,user_id)` (không tìm bằng payload) → kiểm unique constraint/isolation/ledger → bù bằng reversal ledger → thêm regression test.
- **Có thư nhưng list trống**: kiểm domain mapping, credential_version, list/payload cache, normalizer, upstream status. Purge cache theo `inbox_id`, không in payload.
- **Detail lỗi / body rỗng**: xác minh ownership/mid, payload còn hạn, endpoint domain type, sanitizer. Không charge nếu chưa commit detail hợp lệ.
- **Tải upstream tăng đột biến**: kiểm polls/inbox, cache hit, single-flight, retry, semaphore wait. Hạ concurrency, tăng TTL ngắn, bật rate limit; xác minh client không poll mỗi 3s.
- **Cookie hỏng**: trả `UPSTREAM_AUTH`/503, dừng retry storm, cảnh báo operator thay cookie. Không log cookie/key.

## 19. Acceptance criteria
- Tạo inbox / list / detail đúng domain mapping đã đối chiếu.
- Refresh/list/poll/lỗi/mở lại **không charge**.
- Hai request detail đồng thời cùng message → **đúng một** billing read.
- Trừ đúng 200đ/lượt; nạp gói cấp đúng 150/350/800 credit với giá 19k/29k/49k.
- Client poll đúng 9 mốc và dừng đúng điều kiện.
- API không trả/không log cookie/key/payload/body/OTP.
- Payment manual idempotent, không cấp credit hai lần.

## 20. Blockers cần chốt trước production
- Quyền sử dụng / điều khoản SmailPro & Sonjj cho mô hình dịch vụ này.
- Format/TTL thực tế của payload, giới hạn upstream, danh sách domain hỗ trợ (v1 nên bắt đầu chỉ với domain đã test được, mặc định outlook/temp_outlook).
- Payment provider và quy trình hoàn tiền (v1 thủ công).
- Retention/nghĩa vụ kế toán cho metadata billing.
- Có bộ cookie hợp lệ + người chịu trách nhiệm rotation.
- Domain Cloudflare Pages, CORS/cookie strategy, auth provider.
- SLO/RPO/RTO, on-call owner, quy trình xử lý abuse.