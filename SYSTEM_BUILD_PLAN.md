# Kế hoạch xây dựng hệ thống hộp thư tạm và tính phí đọc thư

## 1. Mục tiêu, phạm vi và nguyên tắc sử dụng hợp pháp

### 1.1 Mục tiêu

Xây dựng dịch vụ web cho phép người dùng đã xác thực:

- Tạo inbox tạm từ các miền được upstream hỗ trợ.
- Xem danh sách thư và đọc chi tiết thư theo yêu cầu.
- Trích xuất OTP/link xác minh để phục vụ kiểm thử, QA, phát triển và các luồng mà người dùng có quyền thực hiện.
- Thanh toán theo lượt đọc thành công hoặc theo gói lượt đọc.
- Hoạt động ổn định khi upstream chậm/lỗi, không nhân số request ngoài kiểm soát và không thu phí sai.

### 1.2 Phạm vi hợp pháp

Chỉ dùng hệ thống cho tài khoản, ứng dụng, địa chỉ nhận thư và quy trình mà người dùng sở hữu hoặc được ủy quyền hợp lệ. Cấm spam, chiếm đoạt tài khoản, vượt kiểm soát truy cập, né rate limit, lạm dụng khuyến mại, thu thập dữ liệu trái phép hoặc vi phạm điều khoản của nhà cung cấp upstream. Cần có Điều khoản sử dụng, Chính sách riêng tư, cơ chế báo cáo lạm dụng, khóa tài khoản và bảo toàn bằng chứng ở mức tối thiểu cần thiết.

### 1.3 Ngoài phạm vi

- Không tự động đăng ký/chiếm quyền tài khoản bên thứ ba.
- Không cam kết inbox hay thư tồn tại vĩnh viễn.
- Không cung cấp API để lấy cookie/key/payload thô của upstream.
- Không chạy job chờ đồng bộ dài trong HTTP request.

## 2. Mô hình giá và quy tắc tính phí

### 2.1 Bảng giá

| Hình thức | Giá/quyền lợi |
|---|---:|
| Trả theo lượt | 200đ cho mỗi lần đọc thư **thành công** |
| Starter | 19.000đ / 150 lượt đọc thành công |
| Popular | 29.000đ / 350 lượt đọc thành công |
| Pro | 49.000đ / 800 lượt đọc thành công |

Mọi gói được tạo inbox không giới hạn theo **fair-use**. “Không giới hạn” không có nghĩa là không rate limit: giới hạn theo tài khoản/IP/device và sức khỏe upstream vẫn được áp dụng để chống abuse và bảo vệ hệ thống.

### 2.2 Định nghĩa lượt đọc thành công

Chỉ charge khi endpoint đọc chi tiết trả về một message hợp lệ, đúng inbox, có `mid`, upstream trả thành công và backend commit được bản ghi sử dụng. Không charge cho:

- Refresh danh sách inbox.
- Poll khi chưa có thư.
- Mở lại cùng một thư đã được charge trước đó cho cùng quyền sở hữu.
- Cache hit của thư đã đọc.
- Timeout, lỗi mạng, upstream 4xx/5xx, response sai định dạng hoặc body/detail rỗng không hợp lệ.
- Client retry cùng idempotency key.
- Transaction billing thất bại/rollback.

Đơn vị quota của gói là “lượt đọc thành công”. Với trả theo lượt, tạo ledger debit 200đ. Không trừ quota và tiền đồng thời.

### 2.3 Dedupe billing bắt buộc

Khóa nghiệp vụ ổn định:

`provider + domain_type + internal_inbox_id + mid`

Tạo unique constraint theo khóa trên (có thể thêm `billable_owner_id` nếu quyền đọc được tính riêng theo chủ sở hữu). **Không dùng payload làm billing dedupe key** vì payload có thể thay đổi, hết hạn, chứa dữ liệu nhạy cảm hoặc được cấp lại cho cùng inbox.

## 3. Đối chiếu chính xác logic Python hiện có

Nguồn đối chiếu: `smailpro_logic_full.py`. File này chỉ là tài liệu tham chiếu; không sửa trực tiếp trong kế hoạch này.

### 3.1 `create()`

- Gọi `GET https://smailpro.com/app/create` với query `username`, `type`, `domain`, `server`.
- Xác thực bằng cookie; timeout request hiện tại 30 giây.
- Retry blocking tối đa mặc định 5 lần, sleep 2 giây giữa các lần.
- Chấp nhận nhiều dạng response và chuẩn hóa thành `{address, key, timestamp}`.
- Rủi ro: một web request có thể giữ worker hơn 2 phút trong trường hợp xấu; retry đồng loạt tạo thundering herd; log hiện tại có thể lộ địa chỉ.

Thiết kế mới: route create chỉ thực hiện một ngân sách thời gian hữu hạn; retry ngắn có jitter và tổng deadline; nếu cần retry dài thì chuyển job nền. Lưu `key` mã hóa, không trả cho browser.

### 3.2 `get_inbox()`

Mỗi lần đọc danh sách có hai upstream hop tuần tự:

1. `POST https://smailpro.com/app/inbox` với `address`, `timestamp`, `key` để lấy `payload`.
2. Chọn endpoint Sonjj theo domain (`temp_outlook`, `temp_gmail`, `temp_yahoo`, `temp_mailru`, `temp_icloud`, mặc định `temp_other`), rồi `GET /v1/{domain_type}/inbox?payload=...` để lấy danh sách.

Response được chuẩn hóa thành `mid`, `subject`, `sender`, `date`, `snippet`. Rủi ro hiện tại: log body, payload, URL chứa payload và raw message. Trong production phải loại bỏ toàn bộ log này.

### 3.3 `get_message_detail()`

- Gọi `GET https://api.sonjj.com/v1/{domain_type}/message?payload=...&mid=...`.
- Chuẩn hóa subject/sender/to/date và các biến thể `message`, `body`, `htmlBody`, `content`, `textBody`.
- Cần payload hợp lệ và `mid`; upstream có thể trả response không nhất quán.
- Backend phải xác minh `mid` thuộc inbox của user trước khi gọi detail và trước billing commit.

### 3.4 Polling hiện tại và request amplification

`wait_for_message()` và `wait_for_code()` đang loop đồng bộ mỗi 3 giây đến 120 giây. Mỗi poll inbox tạo tối đa 2 upstream request; `wait_for_code()` còn gọi detail cho từng message. Với 41 mốc từ 0 đến 120 giây, một client có thể tạo khoảng 82 upstream request chỉ để kiểm tra inbox, chưa tính detail. N client/tab/retry sẽ nhân tải tuyến tính; refresh đồng thời làm tăng đột biến.

**Tuyệt đối không chạy `wait_for_message`, `wait_for_code`, `full_flow` (hoặc quick wrapper tương ứng) bên trong web request.** HTTP API phải stateless, trả nhanh; lịch polling nằm ở client và mỗi lần chỉ gọi một endpoint backend có cache/single-flight/rate limit.

## 4. Kiến trúc mục tiêu

```text
Browser
  -> Cloudflare Pages (SPA)
  -> HTTPS FastAPI trên Koyeb
       -> Neon PostgreSQL (nguồn dữ liệu chuẩn, billing ledger)
       -> RAM TTL cache + single-flight (một replica)
       -> Upstash Redis khi chạy multi-replica
       -> SmailPro / Sonjj upstream
       -> Payment gateway/webhook
```

### 4.1 Cloudflare Pages

- Host SPA, asset immutable/hash, CSP nghiêm ngặt.
- Chỉ giữ access token ngắn hạn trong memory hoặc dùng HttpOnly Secure SameSite cookie do backend cấp.
- Polling theo lịch cố định và dừng khi tab hidden/quá hạn/inbox bị đóng.

### 4.2 FastAPI trên Koyeb

- Async endpoints; dùng async HTTP client với connection pool, connect/read/total timeout.
- Dependency auth, ownership, rate limit và request ID dùng chung.
- Không giữ trạng thái quan trọng chỉ trong process.
- Health: `/health/live`; readiness kiểm tra DB và cấu hình, không bắt buộc upstream sống.

### 4.3 Neon PostgreSQL

Nguồn dữ liệu chuẩn cho user, inbox, message metadata, payment, package, quota và immutable billing ledger. Dùng pooled connection, transaction ngắn, migration có version.

### 4.4 Cache và single-flight

- Một replica: RAM TTL cache có giới hạn kích thước + single-flight theo khóa logic.
- Multi-replica: chuyển cache/lock/rate-limit dùng chung sang Upstash Redis; DB vẫn là nguồn chuẩn.
- Không dựa vào distributed lock để bảo đảm billing; billing an toàn bằng unique constraint + transaction DB.

## 5. Cấu trúc source đề xuất

```text
frontend/
  src/api/ src/pages/ src/components/ src/stores/ src/security/
backend/
  app/main.py
  app/api/routes/{auth,inboxes,messages,billing,payments,admin}.py
  app/core/{config,security,errors,logging,rate_limit}.py
  app/domain/{models,services,policies}.py
  app/integrations/{smailpro,sonjj,payments}.py
  app/repositories/
  app/cache/{memory,redis,singleflight}.py
  app/db/{session,migrations}.py
  tests/{unit,integration,contract,e2e,load}/
  alembic/
infra/
  koyeb/ cloudflare/ scripts/
docs/
```

Tách adapter upstream khỏi service nghiệp vụ; không import trực tiếp module synchronous hiện tại vào route production. Có thể tái sử dụng mapping/normalizer sau khi loại bỏ log nhạy cảm và bọc async adapter.

## 6. API contract v1

Mọi response có `request_id`; lỗi theo envelope `{error:{code,message,retryable},request_id}`. Không trả cookie, key hay payload.

### 6.1 Inbox

- `POST /v1/inboxes`
  - Body: `{domain, username_mode?}`.
  - `201`: `{id,address,domain_type,status,created_at,expires_at}`.
  - Hỗ trợ `Idempotency-Key`; cùng key/user/body trả cùng inbox.
- `GET /v1/inboxes?cursor=&limit=`: danh sách inbox thuộc user.
- `GET /v1/inboxes/{inbox_id}`: metadata inbox.
- `POST /v1/inboxes/{inbox_id}/refresh`
  - `200`: `{messages,next_poll_after_seconds,refreshed_at}`.
  - Không charge. Backend cache/single-flight; có thể trả `202` nếu refresh đang chạy.
- `DELETE /v1/inboxes/{inbox_id}`: soft delete/đóng inbox.

### 6.2 Message

- `GET /v1/inboxes/{inbox_id}/messages`: list metadata cache, không charge.
- `GET /v1/inboxes/{inbox_id}/messages/{mid}`:
  - `200`: detail đã sanitize + `{billing:{charged,amount,source}}`.
  - Charge chỉ khi detail upstream hợp lệ và transaction commit.
  - Mở lại trả cache/DB với `charged:false`.
- Không nhận payload từ client; backend lấy secret đã mã hóa theo inbox.

### 6.3 Billing/payment

- `GET /v1/billing/balance`, `GET /v1/billing/ledger?cursor=`.
- `POST /v1/payments/qr`: tạo yêu cầu QR `{package_code|amount}` và reference duy nhất.
- `POST /v1/payments/{id}/manual-proof`: nộp bằng chứng; trạng thái `pending_review`.
- `POST /v1/webhooks/payments/{provider}`: xác thực chữ ký, timestamp, replay protection; idempotent theo provider event ID.
- `GET /v1/payments/{id}`: trạng thái `pending|pending_review|paid|rejected|expired|refunded`.

Status quan trọng: 401 chưa xác thực, 403 không sở hữu, 404 không tồn tại, 409 idempotency/conflict, 402 hết quota/số dư, 429 rate limit, 502 upstream invalid, 503 upstream unavailable, 504 upstream timeout.

## 7. Schema, index và transaction billing

### 7.1 Bảng chính

- `users(id, status, created_at, ...)`.
- `plans(code, price_vnd, included_reads, fair_use_policy_version, active)`.
- `subscriptions(id, user_id, plan_code, reads_total, reads_used, starts_at, expires_at, status)`.
- `wallets(user_id, balance_vnd, version)` nếu hỗ trợ pay-as-you-go.
- `inboxes(id, user_id, provider, domain_type, address_hash, address_encrypted, key_encrypted, timestamp, status, created_at, expires_at, deleted_at)`.
- `messages(id, inbox_id, mid, subject_sanitized, sender_sanitized, received_at, discovered_at)`.
- `message_details(message_id, body_encrypted_or_sanitized, fetched_at, expires_at)` nếu quyết định lưu detail.
- `billing_reads(id, user_id, inbox_id, provider, domain_type, mid, subscription_id, amount_vnd, source, created_at)`.
- `ledger_entries(id, user_id, type, amount_vnd, reference_type, reference_id, created_at)`.
- `payments(id, user_id, provider, provider_ref, package_code, amount_vnd, status, created_at, paid_at)`.
- `webhook_events(provider, event_id, payload_hash, status, received_at, processed_at)`; chỉ lưu hash/metadata tối thiểu, không lưu body nhạy cảm nếu không cần.

### 7.2 Constraints/index

- Unique `inboxes(user_id, id)` ngầm qua PK/ownership query; cân nhắc unique address trong vòng đời active.
- Unique `messages(inbox_id, mid)`.
- Unique billing dedupe trên `(provider, domain_type, inbox_id, mid, user_id)`.
- Unique `payments(provider, provider_ref)` và `webhook_events(provider,event_id)`.
- Index `(user_id,status,created_at desc)`, `(inbox_id,received_at desc)`, `(user_id,created_at desc)` cho ledger, `(expires_at)` cho cleanup.
- CHECK tiền/quota không âm; foreign key đầy đủ.

### 7.3 Transaction đọc và charge

1. Kiểm tra auth/ownership/rate limit.
2. Nếu `billing_reads` đã tồn tại, trả detail cache/DB, không charge.
3. Single-flight fetch detail theo `inbox_id+mid`; validate response và quan hệ message.
4. Bắt đầu DB transaction.
5. `INSERT billing_reads ... ON CONFLICT DO NOTHING RETURNING id`.
6. Chỉ khi insert thành công: khóa subscription/wallet row (`FOR UPDATE`), kiểm tra quota/số dư, trừ đúng một nguồn, ghi ledger.
7. Nếu thiếu quota/số dư hoặc bất kỳ bước nào lỗi: rollback toàn bộ; không ghi charge.
8. Commit rồi mới trả `charged:true`. Concurrent duplicate sẽ thấy conflict và trả `charged:false`.

Nếu muốn kiểm tra quota trước fetch để tránh upstream tốn phí, thực hiện precheck không khóa; kiểm tra quyết định vẫn phải lặp lại trong transaction.

## 8. Polling client và kiểm soát request amplification

Lịch chính xác tính từ lúc bắt đầu chờ: **0, 3, 8, 15, 25, 40, 60, 90, 120 giây**. Đây là 9 poll, thay vì khoảng 41 poll mỗi 3 giây.

- Mỗi thời điểm chỉ một request refresh/inbox.
- Dừng ngay khi có message phù hợp, user đóng inbox, logout hoặc đến 120 giây.
- Khi tab hidden: pause; khi visible lại không “bù” hàng loạt, chỉ thực hiện tối đa một refresh nếu còn hiệu lực.
- Abort request cũ trước request mới; không retry chồng.
- Tôn trọng `Retry-After`/`next_poll_after_seconds`, 429 và circuit breaker.
- Nhiều tab phối hợp bằng BroadcastChannel/local lease; server vẫn là lớp bảo vệ cuối.

Request amplification mới tối đa khoảng 18 upstream hop/inbox nếu mọi list cache đều miss; cache payload/list và single-flight phải giảm thêm các request trùng.

## 9. Chiến lược cache

- **Payload cache**: key `payload:{inbox_id}:{credential_version}`, TTL ngắn theo quan sát upstream (khởi điểm 15–30 giây), không log/không gửi client, mã hóa nếu đưa vào Redis.
- **List cache**: key `list:{inbox_id}`, TTL 3–8 giây; stale-while-revalidate ngắn; negative cache inbox trống 2–3 giây.
- **Detail cache**: key `detail:{inbox_id}:{mid}`, TTL dài hơn nhưng không vượt retention; cache chỉ sau validate; sanitize HTML trước trả client.
- Single-flight cùng khóa để chỉ một coroutine/replica gọi upstream.
- Cache miss không được hiểu là chưa charge; luôn kiểm tra DB billing dedupe.
- Payload không phải định danh message và **không bao giờ là billing key**.

## 10. Concurrency, semaphore và rate limit

- Semaphore global trên mỗi replica cho upstream; semaphore riêng theo provider/domain_type để lỗi một miền không chiếm toàn bộ capacity.
- Hàng đợi chờ semaphore có timeout ngắn; quá tải trả 503/429 có `Retry-After`, không chờ vô hạn.
- Rate limit token bucket theo user + IP + route: create thấp hơn list; detail theo quota; webhook theo provider/IP nhưng không làm mất event hợp lệ.
- Fair-use gồm số inbox/phút, inbox active, refresh/phút, concurrency và daily anomaly threshold; cấu hình theo plan nhưng không hứa bỏ giới hạn.
- Circuit breaker theo upstream endpoint; retry chỉ với lỗi retryable, exponential backoff + jitter, không retry 4xx nghiệp vụ.

## 11. Active/standby cookies

- Lưu cookie set trong secret manager/Koyeb secret, mã hóa, không trong repo/DB plaintext.
- Mỗi provider có `active` và `standby`; health score dựa trên lỗi xác thực, timeout, tỷ lệ thành công.
- Chỉ failover khi active được xác định hỏng; cooldown để tránh flap; không thử cả hai trong mọi request.
- Cookie version gắn với inbox/credential version để payload cache không trộn.
- Rotation có audit metadata (ai/khi nào/version), không lưu giá trị secret.
- **Không log cookie, key, payload, request/response body hay OTP** ở bất kỳ cấp log nào.

## 12. Thanh toán QR: manual trước, webhook sau

### 12.1 Giai đoạn manual

- Backend sinh payment reference duy nhất và QR đúng số tiền/nội dung.
- User gửi bằng chứng; admin đối soát và chuyển trạng thái bằng thao tác có audit.
- Credit/quota chỉ cấp trong transaction khi chuyển `paid`; thao tác lặp không cấp hai lần.
- Không tự động tin ảnh hoặc text do client gửi.

### 12.2 Giai đoạn webhook

- Xác thực chữ ký trên raw bytes, timestamp window, source/provider secret và event ID.
- Ghi nhận event idempotent; worker xử lý và reconcile amount/reference/account.
- Trả ACK nhanh; retry processing nội bộ; dead-letter/review cho event lệch.
- Job reconciliation định kỳ so sánh payment, ledger và báo cáo nhà cung cấp.

## 13. Security, retention và anti-abuse

- TLS mọi nơi; secrets qua secret manager; least privilege DB; rotate định kỳ.
- Auth token ngắn hạn, refresh rotation, HttpOnly/Secure/SameSite cookie, CSRF protection nếu cookie auth.
- CORS allowlist chính xác domain Pages; CSP; chống clickjacking; validate/normalize input.
- Sanitize HTML email, render trong sandboxed iframe; vô hiệu script, form, remote tracking image mặc định và link nguy hiểm.
- Mã hóa key/address/detail nhạy cảm; hash để tra cứu khi cần. Không đưa upstream secret vào analytics.
- Retention mặc định đề xuất: payload RAM/Redis vài chục giây; detail tối thiểu cần dùng và xóa trong 24 giờ hoặc sớm hơn; metadata billing theo nghĩa vụ kế toán/pháp lý; user có quyền xóa theo chính sách.
- Cleanup job idempotent; backup cũng tuân retention.
- Anti-abuse: velocity/device/IP signals, disposable-account loops, shared cookie anomaly, CAPTCHA/risk challenge ở create, denylist có review, giới hạn active inbox.
- Log/audit không chứa nội dung thư, OTP, body, cookie, key hoặc payload.

## 14. Logging, taxonomy lỗi, metrics và alert

### 14.1 Structured logging

Chỉ log: timestamp, level, service/version, request_id, route template, user_id dạng hash, inbox_id nội bộ, provider/domain_type, status, latency, cache outcome, retry count, error code. Redaction middleware phải drop query nhạy cảm và body trước logger/APM.

### 14.2 Error taxonomy

- `AUTH_*`: chưa đăng nhập, token hết hạn, ownership.
- `VALIDATION_*`: domain/ID/body không hợp lệ.
- `UPSTREAM_AUTH`, `UPSTREAM_TIMEOUT`, `UPSTREAM_RATE_LIMIT`, `UPSTREAM_BAD_RESPONSE`, `UPSTREAM_UNAVAILABLE`.
- `CACHE_*`, `DB_*`, `BILLING_INSUFFICIENT`, `BILLING_CONFLICT`, `PAYMENT_*`.
- `ABUSE_BLOCKED`, `INTERNAL_ERROR`.

Mỗi lỗi có HTTP mapping, retryable flag và message an toàn; không trả stack trace/raw upstream.

### 14.3 Metrics/KPI kỹ thuật

- Request rate/error/latency p50/p95/p99 theo route.
- Upstream call count per user action và theo endpoint.
- Cache hit ratio payload/list/detail; single-flight coalesced count.
- Create success, inbox refresh success, detail valid success.
- Billing attempts/insert/conflict/rollback, double-charge count.
- Polls/inbox, active inbox/user, 429/abuse blocks.
- Payment conversion, pending age, webhook duplicate/failure.
- DB pool saturation, semaphore wait, Redis/DB errors.

### 14.4 Alerts

Cảnh báo theo SLO và burn rate: tăng 5xx/upstream timeout, p95 vượt ngưỡng, create/detail success giảm, billing mismatch hoặc double charge > 0, webhook backlog, DB pool/semaphore saturation, cache hit giảm mạnh, active cookie auth failure. Alert phải kèm runbook link và dashboard, không chứa dữ liệu nhạy cảm.

## 15. Testing

- **Unit**: domain mapping, response normalizer, OTP extractor (không log fixture), cache key, polling scheduler, billing policy.
- **Contract**: fixture cho mọi dạng create/list/detail response, malformed JSON, empty payload, missing mid, 401/429/5xx.
- **Integration DB**: migration, unique constraints, transaction rollback, concurrent detail requests chỉ charge một lần, hết quota không charge.
- **Cache/single-flight**: 100 request đồng thời tạo đúng một upstream call/key; TTL, eviction, Redis outage fallback.
- **API/security**: ownership, IDOR, CSRF/CORS/CSP, HTML sanitization, secret redaction, rate limit.
- **Payment**: manual approval lặp, webhook signature/replay/duplicate/out-of-order/refund/reconciliation.
- **E2E**: create -> poll lịch chuẩn -> list -> detail -> reopen không charge; upstream timeout; cookie failover.
- **Load/soak**: amplification budget, semaphore, DB pool, multi-replica với Upstash.
- **Failure injection**: Neon/Redis/upstream chậm hoặc mất, deploy restart giữa transaction.
- CI phải quét secret, dependency, lint/type-check, migration dry-run và kiểm tra log fixture không chứa cookie/key/payload/body/OTP.

## 16. Triển khai, migration, rollback và backup

### 16.1 Trình tự theo dependency, không theo mốc ngày

1. Chốt legal/fair-use/retention và contract billing.
2. Định nghĩa OpenAPI, error taxonomy, schema và migration.
3. Dựng Neon, role least privilege, backup/PITR và test restore.
4. Xây adapter async + contract tests + redaction.
5. Xây repository/service, billing transaction và concurrency tests.
6. Thêm RAM cache/single-flight/rate limit/semaphore.
7. Xây API/auth/ownership và payment manual.
8. Xây SPA Pages, polling scheduler và multi-tab coordination.
9. Deploy staging Koyeb, E2E/load/security test.
10. Canary production, quan sát KPI rồi tăng traffic.
11. Khi có nhiều replica, bật Upstash cho cache/lock/rate limit dùng chung và test partition.
12. Sau khi manual payment ổn định, triển khai webhook + reconciliation.

### 16.2 Migration

- Alembic migration forward-compatible, expand/contract; không drop/rename cột cùng release với code phụ thuộc.
- Index lớn tạo theo cách giảm lock phù hợp Neon/PostgreSQL.
- Backfill theo batch, resumable và có metric.
- Billing constraint phải tồn tại trước khi bật charge production.

### 16.3 Rollback

- Giữ release trước và feature flags cho billing, webhook, cache Redis, cookie failover.
- Rollback app trước; migration destructive chỉ chạy sau giai đoạn tương thích.
- Khi billing có dấu hiệu sai: tắt charge bằng kill switch, vẫn cho mở lại dữ liệu đã charge, đối soát và hoàn tiền/quota bằng ledger bù trừ; không sửa/xóa ledger cũ.

### 16.4 Backup

- Neon PITR/backup theo gói; export mã hóa cho billing/payment nếu cần.
- Test restore định kỳ vào môi trường cô lập; ghi RPO/RTO được duyệt.
- Không backup payload/cookie/body lâu hơn retention; secret backup tách biệt và kiểm soát truy cập.

## 17. Checklist triển khai theo dependency

- [ ] Legal, ToS, privacy, fair-use, retention được duyệt.
- [ ] OpenAPI và billing semantics được ký duyệt.
- [ ] Schema, unique billing key và migration đã test concurrency.
- [ ] Redaction test chứng minh không log cookie/key/payload/body/OTP.
- [ ] Async upstream adapter có deadline, jitter, circuit breaker.
- [ ] API không gọi `wait_for_message`, `wait_for_code`, `full_flow`.
- [ ] Cache list/detail/payload và single-flight hoạt động.
- [ ] Polling đúng `0,3,8,15,25,40,60,90,120`.
- [ ] Semaphore, fair-use và rate limit có dashboard.
- [ ] Active/standby cookie rotation/failover đã test.
- [ ] Payment manual có audit/idempotency; webhook có replay protection.
- [ ] Security/E2E/load/failure tests đạt.
- [ ] Migration dry-run, backup restore và rollback rehearsal đạt.
- [ ] Alerts/runbook/on-call owner sẵn sàng trước canary.

## 18. Bug runbook

### 18.1 User bị charge hai lần

1. Bật billing kill switch nếu còn phát sinh.
2. Tìm bằng request ID và khóa `(provider,domain_type,inbox_id,mid,user_id)`, không tìm bằng payload.
3. Kiểm tra unique constraint, transaction isolation, ledger và deploy version.
4. Bù quota/tiền bằng reversal ledger có reference; không xóa lịch sử.
5. Thêm regression concurrency test, sửa rồi canary.

### 18.2 Có thư nhưng list trống

Kiểm tra domain mapping, active credential version, payload/list cache, response normalizer, circuit breaker và upstream status. Purge cache theo `inbox_id`, không in payload. Thử một request có trace an toàn; nếu mapping sai, sửa adapter + fixture contract.

### 18.3 Detail lỗi hoặc body rỗng

Xác minh ownership/mid, payload còn hạn, endpoint domain type, response variant và sanitizer. Không charge nếu chưa commit detail hợp lệ. Nếu đã charge nhưng detail không thể cung cấp do lỗi hệ thống, tạo reversal theo policy.

### 18.4 Tải upstream tăng đột biến

Kiểm tra polls/inbox, cache hit, single-flight, multi-tab, retry count và semaphore wait. Hạ concurrency, tăng TTL ngắn, bật circuit breaker/rate limit; xác minh client không polling mỗi 3 giây liên tục.

### 18.5 Cookie active hỏng

Đánh dấu unhealthy theo threshold, failover standby một lần, invalidate cache theo credential version, cảnh báo operator. Không log cookie/key. Nếu cả hai hỏng, trả `UPSTREAM_AUTH`/503 và dừng retry storm.

### 18.6 Webhook không cộng quota

Kiểm tra signature/timestamp/event ID, payment reference/amount, event backlog và transaction. Replay event idempotently; không tạo payment mới thủ công. Nếu đã cộng nhưng UI chưa thấy, kiểm tra cache balance và invalidate.

### 18.7 DB/Redis lỗi

DB lỗi: readiness fail, ngừng charge/detail cần transaction; không fallback billing vào RAM. Redis lỗi: single replica có thể fallback RAM với rate limit bảo thủ; multi-replica giảm tải/disable create nếu không đảm bảo chống amplification.

## 19. Acceptance criteria và KPI

### 19.1 Acceptance criteria chức năng

- Tạo inbox, list, detail chạy đúng domain mapping đã đối chiếu.
- Refresh/list/poll/lỗi/mở lại không charge.
- Hai request detail đồng thời cho cùng message chỉ tạo một billing read.
- Trả theo lượt debit đúng 200đ; Starter/Popular/Pro cấp đúng 150/350/800 lượt với giá 19k/29k/49k.
- Tạo inbox không giới hạn theo fair-use và giới hạn được công bố.
- Client poll đúng 9 mốc và dừng đúng điều kiện.
- API không trả hoặc log cookie/key/payload/body/OTP.
- Payment manual và webhook idempotent; reconciliation không lệch ledger.

### 19.2 KPI/SLO ban đầu cần đo rồi chốt ngưỡng

- Double-charge: 0.
- Secret/OTP/body/payload leakage trong log: 0.
- Upstream calls cho một phiên poll 120 giây: không quá 18 khi cache miss hoàn toàn; mục tiêu thấp hơn nhờ cache/single-flight.
- API availability và p95 theo SLO được duyệt, tách latency cache hit và upstream call.
- Detail success/charge commit consistency: 100% — không có charge nếu detail không hợp lệ.
- Cache hit, create success, payment reconciliation và abuse false-positive có baseline/dashboard trước khi mở rộng.

## 20. Blockers cần giải quyết trước production

- Xác nhận quyền sử dụng và điều khoản của SmailPro/Sonjj cho mô hình dịch vụ này.
- Xác nhận format/TTL thực tế của payload, giới hạn upstream và danh sách domain hỗ trợ.
- Chọn payment provider, chuẩn chữ ký webhook và quy trình hoàn tiền.
- Chốt thời hạn gói, quota carry-over, refund và định nghĩa chủ sở hữu billing khi chia sẻ inbox.
- Chốt retention/body storage và yêu cầu pháp lý/kế toán.
- Koyeb replica/concurrency, Neon connection/PITR và Upstash region/cost.
- Có bộ cookie active/standby hợp lệ, quy trình rotation và người chịu trách nhiệm.
- Có domain Cloudflare Pages, CORS/cookie strategy và auth provider.
- Có SLO/RPO/RTO, on-call owner, ngân sách cảnh báo và quy trình xử lý abuse.

## 21. Các bất biến bắt buộc khi review code

1. Không chạy `wait_for_message`, `wait_for_code`, `full_flow` trong web request.
2. Không log cookie, key, payload, request/response body hoặc OTP.
3. Không dùng payload làm billing dedupe key.
4. Không charge refresh, mở lại, cache hit đã charge hoặc bất kỳ lỗi nào.
5. Billing luôn dựa trên unique constraint và transaction PostgreSQL, không dựa vào cache/lock.
6. Browser không bao giờ nhận upstream cookie/key/payload.
7. Không bypass fair-use, semaphore hoặc rate limit vì plan trả phí.
