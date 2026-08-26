# Kế hoạch sửa sau rà soát `SYSTEM_BUILD_PLAN_v2.md`

> **CẬP NHẬT GIAI ĐOẠN 1 (backend) — ĐÃ HOÀN TẤT.** P0-02 (approve_payment atomic FOR UPDATE + idempotent), P1-08 (ledger/payment invariants + FK RESTRICT + snapshot `credited_vnd`), P0-04 (kill switch `BILLING_CHARGE_ENABLED`/`PAYMENT_APPROVAL_ENABLED` fail-safe + reversal idempotent + admin endpoint) và P1-01 (durable inbox idempotency table) đã áp dụng trong `backend/`. Migration mới: `0003_billing_invariants_and_idempotency`. Quyết định canonical: đơn vị ví = VND credit trong `wallet.balance_vnd`; kill switch mặc định fail-safe (production config invalid → tắt charge). Xem §5 Giai đoạn 1.
>
> Phạm vi rà soát: `SYSTEM_BUILD_PLAN_v2.md`, backend, frontend, Postman artifacts, `smailpro_logic_full.py`, migration, OpenAPI, README, cấu hình mẫu và script chạy local. Không ghi secret vào báo cáo.
>
> Kết quả kiểm tra tại thời điểm rà soát: backend `pytest` **26 passed, 5 warnings**; frontend `npm run build` **thành công**. Hai kết quả này chỉ chứng minh bộ test/build hiện tại chạy được, không chứng minh các bất biến billing, contract và production readiness.

## 1. Kết luận điều hành

Code đã có khung FastAPI/React, adapter async, 7 bảng, unique billing dedupe, khóa ví `FOR UPDATE`, cache RAM, single-flight, sanitizer và polling 9 mốc. Tuy nhiên **chưa đủ điều kiện production**. Các blocker chính:

1. Contract giữa OpenAPI, backend DTO và frontend lệch nghiêm trọng; màn hình detail/payment và message list có thể hỏng dù build vẫn xanh.
2. Duyệt payment có race condition có thể cấp credit hai lần; ledger chưa có idempotency constraint cho credit.
3. Idempotency tạo inbox chỉ ở RAM và còn race ngay trong một process.
4. Refresh token “rotation” không có trạng thái/revocation; tài khoản bị khóa vẫn có thể login/refresh token.
5. Kế hoạch yêu cầu mọi response có `request_id`, readiness kiểm DB, kill switch billing/payment/cache, `credential_version`, retry budget, audit và deployment controls nhưng code chưa có.
6. Test hiện chủ yếu là contract adapter; chưa có DB integration/concurrency/API/security/E2E/frontend test theo kế hoạch.
7. `postman/collections`, `postman/environments`, `postman/specs` đang trống, nên chưa có regression collection/environment/spec để kiểm contract.

## 2. Ma trận ưu tiên

| ID | Mức | Chủ đề | Trạng thái |
|---|---|---|---|
| P0-01 | P0 | API contract backend/OpenAPI/frontend lệch | Block production |
| P0-02 | P0 | Race duyệt payment, có thể cấp credit hai lần | ĐÃ SỬA (GĐ1) |
| P0-03 | P0 | Contract “mọi response có request_id” chưa thực hiện | Block contract |
| P0-04 | P0 | Thiếu kill switch và quy trình reversal có thể vận hành | ĐÃ SỬA (GĐ1) |
| P1-01 | P1 | Idempotency tạo inbox không bền và có race | ĐÃ SỬA (GĐ1) |
| P1-02 | P1 | Refresh token rotation/revocation và trạng thái user | Phải sửa trước production |
| P1-03 | P1 | Manual proof không được nhận/lưu/audit | Flow payment chưa hoàn chỉnh |
| P1-04 | P1 | Ownership/error/status không đúng contract | Rủi ro API/IDOR semantics |
| P1-05 | P1 | Readiness không kiểm DB; HTTP client không đóng | Rủi ro deployment |
| P1-06 | P1 | Rate limit/IP/Retry-After/validation chưa chắc chắn | Rủi ro abuse/DoS |
| P1-07 | P1 | Credential version, payload cache, retry budget chưa có | Lệch kế hoạch upstream |
| P1-08 | P1 | Ledger/payment schema thiếu bất biến kế toán | ĐÃ SỬA (GĐ1) |
| P1-09 | P1 | Test coverage không đạt kế hoạch | Không chứng minh invariant |
| P1-10 | P1 | Thiếu CI/deploy/backup/legal/monitoring artifacts | Chưa production-ready |
| P2-01 | P2 | Python tham chiếu log dữ liệu nhạy cảm | Rủi ro dùng nhầm |
| P2-02 | P2 | Pagination/cursor và lifecycle inbox chưa hoàn chỉnh | Sai dữ liệu/cleanup |
| P2-03 | P2 | Cấu hình/dependency/docs còn lệch | Rủi ro tái lập build |
| P2-04 | P2 | Postman artifacts gần như trống | Thiếu contract regression |

---

## 3. Issues chi tiết

### P0-01 — OpenAPI, backend DTO và frontend không cùng một contract

**Hiện trạng / dẫn chứng**

- OpenAPI định nghĩa message list là `{items:[...]}` và detail có `html_sanitized`, `otp_candidates`, `received_at`; xem `backend/openapi/openapi.yaml`, các section `MessageList` và `MessageDetail`.
- Route thực trả trực tiếp `list[MessageMetaDTO]`: `backend/app/api/routes/messages.py:26-39`.
- Backend detail thực trả `body_html`, `body_text`, `otp`, `links`, `date`, và billing source `read`: `backend/app/domain/models.py:51-71`, `backend/app/domain/services.py`, hàm `_detail_to_dto`.
- Frontend lại đọc `html_sanitized`, `otp_candidates`, `received_at` và chỉ chấp nhận source `upstream|cache`: `frontend/src/api/types.ts:45-69`, `frontend/src/pages/MessageDetailPage.tsx:70-102`.
- Backend trả payment field `qr_payload`; OpenAPI/frontend dùng `qr_content`: `backend/app/domain/models.py:91-106`, `backend/app/domain/services.py`, `_payment_to_dto`; `frontend/src/api/types.ts:103-116`.
- OpenAPI chưa mô tả `/v1/auth/*` và `/v1/admin/payments/{id}/approve`, dù code đã expose tại `backend/app/api/routes/auth.py` và `admin.py`.

**Rủi ro**

Frontend có thể nhận `undefined`, render ngày lỗi, không hiện body/OTP/QR, hoặc crash tại runtime; generated client và contract test sai; build TypeScript vẫn xanh vì không kiểm response runtime.

**Cách sửa**

Chọn **một nguồn contract chuẩn** (khuyến nghị OpenAPI), rồi đồng bộ backend response model và frontend types/generated client. Quyết định dứt khoát tên field và envelope list; thêm auth/admin vào OpenAPI; thêm schema nullable đúng thực tế. Không duy trì ba contract thủ công độc lập.

**Tiêu chí nghiệm thu**

- Live `/openapi.json`, authored OpenAPI và response thực không có diff semantic.
- Detail hiển thị body, ngày, OTP và billing source đúng.
- Payment vừa tạo hiển thị QR payload đúng field.
- Message list được frontend parse đúng.

**Test bổ sung**

- Contract test từng endpoint bằng response thực và JSON Schema.
- Frontend integration test với fixtures backend thật cho list/detail/payment.
- CI diff authored OpenAPI với schema FastAPI đã canonicalize.

### P0-02 — Duyệt payment concurrent có thể cấp credit hai lần

**Hiện trạng / dẫn chứng**

`PaymentService.approve_payment` đọc payment không khóa, kiểm `paid`, sau đó mới đổi trạng thái và khóa wallet: `backend/app/domain/services.py`, section `approve_payment`. Hai transaction đồng thời có thể cùng thấy `pending`, lần lượt khóa ví rồi đều cộng credit. Ledger không có unique constraint trên `(reference_type, reference_id, type)`; `backend/app/db/models.py`, bảng `ledger_entries`; migration `backend/alembic/versions/0001_initial.py` cũng không có guard này.

**Rủi ro**

Cấp tiền hai lần cho một giao dịch, sai sổ cái và thất thoát trực tiếp. Check trạng thái trong Python không phải idempotency transaction-safe.

**Cách sửa**

- Lock payment row `SELECT ... FOR UPDATE` trong cùng transaction.
- Chỉ cho transition hợp lệ `pending_review -> paid` (hoặc policy được chốt).
- Thêm unique constraint cho ledger credit theo payment, ví dụ partial unique/reference key ổn định.
- Có thể dùng atomic conditional update `WHERE status = pending_review RETURNING` làm idempotency gate.
- Ghi actor/admin, thời gian, lý do và audit event.

**Tiêu chí nghiệm thu**

N lời gọi approve đồng thời chỉ tạo một ledger credit, ví chỉ tăng một lần, payment cuối là `paid`; gọi lại trả kết quả idempotent.

**Test bổ sung**

Integration PostgreSQL thật với 10–50 transaction concurrent; test rollback sau mark-paid nhưng trước ledger; test retry sau timeout phía client.

### P0-03 — Bất biến “mọi response có request_id” chưa được thực hiện

**Hiện trạng / dẫn chứng**

Kế hoạch §6 yêu cầu mọi response có `request_id`. Error envelope có trường này (`backend/app/core/errors.py`), nhưng success DTO không có; health cũng chỉ trả `status`: `backend/app/main.py:52-74`. Middleware chỉ đủ khả năng đặt context/header, không làm success body phù hợp contract. OpenAPI success schemas cũng không nhất quán có `request_id`.

**Rủi ro**

Không trace được sự cố billing/payment end-to-end; client và runbook dựa trên contract không hoạt động.

**Cách sửa**

Chốt một trong hai: (a) mọi body dùng envelope `{data,request_id}`; hoặc (b) sửa kế hoạch thành mọi response có header `X-Request-ID`, error body có `request_id`. Cập nhật đồng bộ middleware, DTO, OpenAPI và frontend.

**Tiêu chí nghiệm thu**

Mọi status 2xx/4xx/5xx có request ID theo contract đã chốt và cùng giá trị với structured log.

**Test bổ sung**

API test cho health, auth, inbox, billing, payment và unhandled 500; kiểm request ID do client gửi và do server sinh.

### P0-04 — Không có billing/payment/cache kill switch và reversal workflow vận hành được

**Hiện trạng / dẫn chứng**

Kế hoạch §17 yêu cầu feature flag/kill switch. `backend/app/core/config.py` không có flag tương ứng; tìm trong backend không thấy billing kill switch. Ledger có enum `reversal`, nhưng không có service/admin endpoint/runbook executable để hoàn tiền an toàn.

**Rủi ro**

Khi phát hiện charge sai không thể dừng ngay mà vẫn giữ read/reopen; sửa tay DB dễ phá ledger.

**Cách sửa**

Thêm flag fail-safe (mặc định tắt charge nếu cấu hình production không hợp lệ), endpoint/admin command reversal idempotent, quyền tối thiểu và audit. Xác định hành vi detail khi charge bị tắt.

**Tiêu chí nghiệm thu**

Operator tắt charge không cần deploy; refresh/list/reopen vẫn chạy theo policy; reversal tạo ledger bù trừ đúng một lần, không sửa/xóa entry cũ.

**Test bổ sung**

Test flag on/off trong concurrent reads; test reversal lặp; test audit và quyền admin.

### P1-01 — Idempotency tạo inbox chỉ ở RAM và vẫn có race

**Hiện trạng / dẫn chứng**

`_create_inbox_idempotency` là dict RAM; check key rồi nhả lock trước upstream create, chỉ ghi mapping sau khi DB create: `backend/app/domain/services.py:65-80` và `create_inbox`. Hai request cùng key có thể cùng vượt check và tạo hai inbox; restart mất mapping. Điều này mâu thuẫn contract “cùng key/user → cùng inbox”.

**Rủi ro**

Retry do timeout tạo nhiều inbox upstream, vượt quota và tăng tải; response không ổn định.

**Cách sửa**

Tạo bảng/idempotency record bền vững với unique `(user_id, operation, key)`, request fingerprint, trạng thái `in_progress/completed/failed`, response/resource ID và TTL. Claim key trong DB trước upstream; quy định behavior khi payload khác cùng key.

**Tiêu chí nghiệm thu**

Concurrent/restart/retry cùng key chỉ có một inbox; cùng key nhưng domain khác trả 409; key có giới hạn độ dài và TTL.

**Test bổ sung**

DB integration concurrent, crash-after-upstream, retry-after-timeout và conflicting fingerprint.

### P1-02 — Refresh token không thực sự rotation/revocation; status user bị bỏ qua

**Hiện trạng / dẫn chứng**

Refresh token chỉ là JWT stateless, không có `jti`, token family hay storage; refresh cũ vẫn dùng lại được: `backend/app/core/security.py:47-61`, `backend/app/api/routes/auth.py:129-150`. Login không kiểm `user.status`; refresh không load user, nên user suspended/deleted vẫn có thể nhận token mới. Kế hoạch §13 yêu cầu refresh rotation.

**Rủi ro**

Token bị đánh cắp dùng lại trong 14 ngày; khóa tài khoản không có hiệu lực đầy đủ.

**Cách sửa**

Lưu hash refresh token/family; rotate atomically, revoke token cũ, phát hiện reuse và revoke cả family; kiểm user active tại login/refresh; có logout/revoke-all và key rotation strategy.

**Tiêu chí nghiệm thu**

Refresh token cũ thất bại ngay sau rotation; suspended user không login/refresh/access; không lưu token plaintext.

**Test bổ sung**

Replay, concurrent refresh, stolen-token reuse, logout-all, suspended/deleted user và signing-key rotation.

### P1-03 — Manual proof bị bỏ qua hoàn toàn

**Hiện trạng / dẫn chứng**

OpenAPI/frontend gửi `{note,reference}` (`backend/openapi/openapi.yaml`, `frontend/src/api/endpoints.ts`), nhưng route không nhận body và service chỉ đổi status: `backend/app/api/routes/payments.py:41-51`, `backend/app/domain/services.py`, `submit_manual_proof`. Schema payment không có proof/audit table/columns.

**Rủi ro**

Admin không có bằng chứng để đối soát; tuyên bố “nộp bằng chứng” trong kế hoạch không đúng với code; dữ liệu client gửi bị bỏ lặng.

**Cách sửa**

Chốt loại proof an toàn; lưu metadata/object reference, checksum, MIME/size, người nộp và audit; malware scan nếu upload file; không tin text/ảnh client; status transition chặt chẽ.

**Tiêu chí nghiệm thu**

Proof hợp lệ được lưu và admin xem được; proof quá cỡ/sai loại bị từ chối; không chứa secret trong log; final payment không nhận proof mới.

**Test bổ sung**

Validation, ownership, duplicate submit, malicious file/text, object missing và audit.

### P1-04 — Ownership, not-found và status code lệch contract

**Hiện trạng / dẫn chứng**

`_require_owned_inbox/payment` ném `ValidationErrorError` 422 cho missing/not-owned: `backend/app/domain/services.py`. OpenAPI tuyên bố 403/404. UUID/cursor parse bằng `uuid.UUID()` có thể ném `ValueError` thành 500. Route query `limit` không đặt min/max dù OpenAPI quy định 1..100: `inboxes.py`, `messages.py`, `billing.py`.

**Rủi ro**

Client xử lý sai, malformed input gây 500, pagination có thể bị lạm dụng; semantics IDOR không nhất quán.

**Cách sửa**

Dùng typed UUID/Pydantic constraints, error `NOT_FOUND` thống nhất để tránh enumeration hoặc 403 theo policy rõ ràng; giới hạn limit/cursor/mid/domain/idempotency key.

**Tiêu chí nghiệm thu**

Không input client nào gây 500; status thực khớp OpenAPI; ownership không rò existence.

**Test bổ sung**

Malformed UUID/cursor, limit âm/quá lớn, foreign owner, deleted/expired inbox và oversized mid/header.

### P1-05 — Readiness không kiểm DB; lifecycle HTTP client chưa hoàn chỉnh

**Hiện trạng / dẫn chứng**

`/health/ready` chỉ kiểm chuỗi config có rỗng hay không, không query DB: `backend/app/main.py:59-74`, trái §4. `close_client()` có trong `backend/app/integrations/http_client.py` nhưng không được gọi ở shutdown. `httpx.Timeout` là timeout theo phase, không đảm bảo ngân sách total end-to-end như comment/kế hoạch mô tả.

**Rủi ro**

Pod nhận traffic khi DB chết/DSN sai; connection upstream không đóng sạch; request có thể vượt ngân sách mong đợi.

**Cách sửa**

Readiness chạy `SELECT 1` với timeout ngắn và kiểm migration/config; shutdown đóng HTTP client; bọc tổng thời gian bằng deadline/`asyncio.timeout`; readiness không phụ thuộc upstream.

**Tiêu chí nghiệm thu**

DB mất kết nối → readiness 503 nhanh; liveness vẫn 200; shutdown không leak connection; upstream call không vượt total budget.

**Test bổ sung**

DB unavailable/slow, pool exhausted, shutdown lifecycle, connect/read/pool timeout.

### P1-06 — Rate limit và xác định IP còn dễ bypass/không đúng contract

**Hiện trạng / dẫn chứng**

`client_ip` tin trực tiếp `X-Forwarded-For`: `backend/app/api/deps.py:55-59`; client có thể giả nếu proxy không strip. Bucket map không cleanup, có thể tăng vô hạn: `backend/app/core/rate_limit.py`. Khi chặn, code bỏ `retry_after` và error handler không set `Retry-After`, dù kế hoạch/frontend yêu cầu. Key ghép `user+IP+route` không phải hai limit độc lập user và IP.

**Rủi ro**

Bypass hoặc khóa nhầm rate limit, memory growth, polling không backoff đúng.

**Cách sửa**

Chỉ tin proxy chain đã cấu hình; áp bucket độc lập theo user, IP và route; TTL/LRU cleanup; trả `Retry-After`; thêm concurrency admission timeout và active-inbox create race guard.

**Tiêu chí nghiệm thu**

Spoof XFF không đổi client identity ngoài trusted proxy; 429 luôn có `Retry-After`; bộ nhớ bucket bị giới hạn.

**Test bổ sung**

XFF spoof/IPv6/proxy chain, burst, cleanup, nhiều account cùng IP, một user nhiều IP.

### P1-07 — `credential_version`, payload cache và retry budget được kế hoạch nêu nhưng code chưa có

**Hiện trạng / dẫn chứng**

Schema inbox không có `credential_version`: `backend/app/db/models.py` và migration 0001. Tìm backend không có trường này. `CACHE_PAYLOAD_TTL` có trong config nhưng service lấy payload lại cho refresh/detail, không dùng payload cache. `UPSTREAM_MAX_RETRIES` có config nhưng `http_client.request` không retry. Cookie rotation audit/alert cũng chưa có.

**Rủi ro**

Đổi cookie có thể trộn cache/credential lifecycle; upstream call amplification; cấu hình tạo cảm giác đã triển khai nhưng không có tác dụng.

**Cách sửa**

Thêm credential metadata/version không chứa secret, gắn version vào inbox/cache key; triển khai payload cache RAM với TTL và zero-log; retry chỉ cho lỗi an toàn theo deadline, jitter và `Retry-After`; audit rotation.

**Tiêu chí nghiệm thu**

Rotation không dùng nhầm cache cũ; cache giảm hop SmailPro; retry không vượt budget/không tạo storm; config nào tồn tại đều có test chứng minh hiệu lực.

**Test bổ sung**

Rotate giữa hai request, payload expiry, concurrent cache miss, 429/5xx/timeout retry và deadline.

### P1-08 — Schema chưa bảo vệ đầy đủ bất biến kế toán

**Hiện trạng / dẫn chứng**

Ledger “immutable” mới là quy ước code, DB vẫn cho UPDATE/DELETE; FK user dùng `ON DELETE CASCADE`, có thể xóa toàn bộ ledger/payment: `backend/app/db/models.py`, `backend/alembic/versions/0001_initial.py`. Không có unique payment-credit reference; không có CHECK dấu amount theo type; payment amount không CHECK dương; package snapshot/credit granted không được lưu.

**Rủi ro**

Mất lịch sử kế toán, ledger không cân, thay config package làm khó audit giao dịch cũ.

**Cách sửa**

Dùng role/trigger/revoke để append-only; tránh cascade dữ liệu kế toán; thêm constraints và idempotency reference; lưu snapshot `credited_vnd`/package terms tại payment; có reconciliation invariant `wallet = sum(ledger)`.

**Tiêu chí nghiệm thu**

Không thể sửa/xóa ledger bằng app role; mỗi payment chỉ một credit; amount/type hợp lệ; reconciliation bằng 0 sai lệch.

**Test bổ sung**

Constraint tests, delete user, direct update/delete bằng app role, reconciliation và migration rollback.

### P1-09 — Bộ test chưa bao phủ các bất biến quan trọng

**Hiện trạng / dẫn chứng**

Chỉ có `backend/tests/contract/test_adapters_contract.py`; không có thư mục/test thực cho unit, DB integration, concurrency, API/security, payment hay E2E như §15. Pytest chạy 26 pass nhưng có 5 warnings; frontend không có test script trong `frontend/package.json`.

**Rủi ro**

Các lỗi P0 vẫn lọt dù CI xanh; không chứng minh “charge đúng một lần”.

**Cách sửa**

Dựng test pyramid đúng §15, PostgreSQL tạm cho integration, deterministic clock cho polling, mock upstream ở adapter boundary; thêm coverage gates theo invariant thay vì chỉ phần trăm dòng.

**Tiêu chí nghiệm thu**

Có test fail trước/fix sau cho mọi P0/P1; zero warning; frontend component/integration test; E2E create→poll→detail→reopen.

**Test bổ sung**

Đúng danh mục §15, ưu tiên concurrent billing/payment, rollback, IDOR, redaction, sanitizer và polling.

### P1-10 — Thiếu artifacts production: CI, deploy, backup, legal, monitoring

**Hiện trạng / dẫn chứng**

Không thấy `infra/`, CI workflow, Docker/deploy manifests, docs ToS/Privacy/abuse/retention, backup restore evidence, SLO/runbook/alert dashboard. `run-demo.bat` chỉ phục vụ local và tự cài dependency hệ thống. Đây là các mục kế hoạch §13–17 nói phải có trước production.

**Rủi ro**

Không tái lập deployment, không kiểm secret/dependency/migration, không đáp ứng vận hành/pháp lý.

**Cách sửa**

Tạo pipeline lint/type/test/secret/dependency/OpenAPI/migration; image pinned/non-root; Koyeb/Pages config; migration job một lần; backup/PITR restore rehearsal; SLO/alerts/runbooks; hoàn tất legal approval.

**Tiêu chí nghiệm thu**

Staging deploy tự động và rollback được; restore test có biên bản; secret scan sạch; ownership/SLO/RPO/RTO được phê duyệt.

**Test bổ sung**

Migration dry-run, smoke test staging, rollback rehearsal, restore drill và synthetic health/detail-without-charge.

### P2-01 — `smailpro_logic_full.py` chứa logging nhạy cảm, phải cô lập rõ

**Hiện trạng / dẫn chứng**

File tham chiếu log body, payload, URL có query payload, response headers, address và raw response; ví dụ section `get_inbox`, `_fetch_sonjj_messages`, `create`. Điều này vi phạm bất biến nếu file được chạy trong production, dù kế hoạch nói không import trực tiếp.

**Rủi ro**

Developer dùng nhầm module hoặc chạy demo với log thật sẽ lộ cookie-adjacent payload/body/OTP/address.

**Cách sửa**

Đưa vào thư mục reference rõ ràng, banner “không production”, loại bỏ/redact log nhạy cảm hoặc thêm static guard cấm import từ backend. Không lưu cookie file trong repo.

**Tiêu chí nghiệm thu**

Production dependency graph không import file; secret/log scan fixture không thấy payload/body/address/OTP.

**Test bổ sung**

Import-boundary test và log redaction scan.

### P2-02 — Pagination và lifecycle inbox chưa hoàn chỉnh

**Hiện trạng / dẫn chứng**

Cursor chỉ là ID nhưng query lọc chủ yếu `created_at < anchor.created_at`, bỏ các row cùng timestamp; ledger tương tự: `inbox_repo.py`, `ledger_repo.py`. `cursor` của message route không được dùng. `expires_at` thường để `None`; chưa có cleanup job idempotent. Quota count tải tối đa 1000 rows rồi đếm trong Python, không khóa với concurrent create.

**Rủi ro**

Bỏ sót/duplicate page, active inbox không hết hạn, race vượt quota, cleanup/retention không chạy.

**Cách sửa**

Keyset cursor chứa `(created_at,id)` có ký/encode; query tuple ordering; DB count/constraint/advisory policy cho quota; xác định TTL và cleanup job idempotent.

**Tiêu chí nghiệm thu**

Không mất/duplicate khi timestamp trùng; cursor sai trả 422; expired inbox không refresh/detail; cleanup có metric/audit.

**Test bổ sung**

Same-timestamp pages, delete giữa pages, forged cursor, concurrent create và cleanup retry.

### P2-03 — Cấu hình, dependency và tài liệu chưa đồng bộ

**Hiện trạng / dẫn chứng**

- `jwt_algorithm` có config nhưng security hardcode `HS256`: `config.py`, `security.py`.
- Hầu hết Python dependencies không pin version: `backend/requirements.txt`.
- Backend README còn nói nhiều thư mục “filled in later” dù code đã có.
- `run-demo.bat` dùng system Python thay vì `.venv`, tự `pip install`, và mở file cấu hình local; khó tái lập/không phù hợp production.
- Cấu hình mẫu CORS dùng port 3000 trong khi Vite chạy 5173: `backend/.env.example`, `run-demo.bat`.

**Rủi ro**

Drift môi trường, supply-chain/reproducibility kém, onboarding sai.

**Cách sửa**

Lock dependencies với hash, thống nhất algorithm hoặc bỏ config giả, cập nhật README và script theo venv, validate production settings fail-fast, đồng bộ CORS dev.

**Tiêu chí nghiệm thu**

Build sạch từ máy mới bằng lockfile; docs chạy đúng; production không boot với placeholder/secret yếu/CORS sai.

**Test bổ sung**

Clean-room install, config validation matrix và dependency audit.

### P2-04 — Postman workspace chưa có bộ contract/regression dùng được

**Hiện trạng / dẫn chứng**

`postman/collections`, `postman/environments`, `postman/specs` trống; chỉ có `postman/globals/workspace.globals.yaml` với `values: []`.

**Rủi ro**

Không có smoke/regression suite dùng chung; contract drift P0-01 khó phát hiện.

**Cách sửa**

Sau khi chốt OpenAPI, tạo collection theo contract, environment local/staging không chứa secret, examples lỗi/thành công và test invariant; chạy collection trong CI.

**Tiêu chí nghiệm thu**

Collection chạy pass trên staging; không có secret; bao phủ auth, inbox, refresh, detail/reopen, billing, payment và negative cases.

**Test bổ sung**

Schema validation, request-id, no-secret fields, 402/409/429/5xx và concurrent flows ở integration layer.

---

## 4. Những điểm kế hoạch nói đã có nhưng code chưa chứng minh/triển khai

- “Mọi response có request_id”: mới chắc chắn ở error, chưa ở success.
- “Refresh rotation”: mới phát token mới, chưa revoke token cũ.
- “Manual proof”: request body bị bỏ qua, không có persistence/audit.
- “Payment approval idempotent”: chỉ check trạng thái ở application, chưa concurrency-safe.
- “Idempotency-Key cùng user trả cùng inbox”: RAM-only, mất khi restart và race concurrent.
- “Readiness kiểm DB”: chỉ kiểm chuỗi config.
- “Payload cache + credential_version”: config TTL có nhưng luồng chưa dùng và schema thiếu version.
- “Retry ngắn có jitter”: retry setting chưa được dùng.
- “Retry-After”: limiter tính được nhưng response không trả header.
- “Feature flag/kill switch”: chưa có.
- “Metrics/KPI/alert/runbook”: chưa có implementation artifact.
- “Backup/PITR/test restore, deploy Koyeb/Pages, CI scan”: chưa có bằng chứng trong project.
- “Bộ test unit/integration/e2e/security”: hiện chỉ thấy adapter contract tests.

## 5. Lộ trình sửa theo phụ thuộc

### Giai đoạn 0 — Đóng băng production và chốt quyết định

1. Không bật charge/payment approval production.
2. Chốt canonical OpenAPI, success envelope/request-id policy, ownership semantics.
3. Chốt đơn vị ví: `balance_vnd` thực sự là VND credit; package credit là số VND có thể tiêu (150 lượt → 30.000 credit), và snapshot kế toán cần lưu.
4. Chốt legal/upstream permission, retention, payment proof và admin audit.

### Giai đoạn 1 — Bất biến dữ liệu và tiền

1. Migration bổ sung payment approval idempotency/ledger constraints/audit. [DONE 0003]
2. Sửa approve payment atomic + concurrency tests. [DONE]
3. Thêm kill switch và reversal workflow. [DONE]
4. Thêm durable inbox idempotency. [DONE]
5. Rehearsal migration/rollback trên bản sao dữ liệu.

### Giai đoạn 2 — Contract và auth

1. Đồng bộ OpenAPI ↔ backend ↔ frontend.
2. Chuẩn hóa request ID, error/status/validation/pagination.
3. Triển khai refresh token family/revocation và user-status checks.
4. Hoàn thiện proof submission/audit.

### Giai đoạn 3 — Upstream, abuse và lifecycle

1. Credential version + payload cache + deadline/retry policy.
2. Trusted proxy/IP và limiter độc lập, cleanup, `Retry-After`.
3. DB readiness, HTTP client shutdown, inbox expiry/cleanup.
4. Redaction/import guard cho Python tham chiếu.

### Giai đoạn 4 — Verification và deployment

1. DB integration/concurrency/API/security/frontend/E2E tests.
2. Postman contract collection và CI run.
3. CI/CD, dependency lock, secret scan, OpenAPI drift, migration dry-run.
4. Staging, backup restore, load/security test, canary và rollback rehearsal.
5. Chỉ mở production khi checklist dưới đây hoàn tất.

## 6. Checklist hoàn tất

### P0 / dữ liệu tiền

- [ ] Concurrent detail chỉ một billing read/debit/ledger.
- [ ] Concurrent payment approve chỉ một credit/ledger.
- [ ] Ledger/payment DB constraints và app role append-only đạt.
- [ ] Billing kill switch và reversal idempotent đã diễn tập.
- [ ] Durable inbox idempotency qua restart/concurrency.

### Contract / security

- [ ] OpenAPI authored = live schema = frontend runtime types.
- [ ] Mọi response có request ID theo policy đã chốt.
- [ ] Không response/log chứa cookie, key, payload hoặc raw upstream object.
- [ ] Refresh token cũ bị revoke; suspended user bị chặn toàn bộ.
- [ ] Ownership, UUID, cursor, limit, domain, mid và header được validate.
- [ ] Manual proof có validation, persistence, audit và access control.
- [ ] CSP/sandbox/sanitizer được browser security test.

### Upstream / concurrency

- [ ] Credential version và cache invalidation hoạt động.
- [ ] Retry/deadline/jitter không tạo retry storm.
- [ ] Rate limit trả `Retry-After`, không tin XFF ngoài trusted proxy.
- [ ] Readiness kiểm DB thật; shutdown đóng pool/client.
- [ ] Inbox expiry và cleanup idempotent.

### Testing / vận hành

- [ ] Unit, contract, PostgreSQL integration, concurrency, API/security, frontend và E2E pass.
- [ ] Zero pytest warning; frontend có test script.
- [ ] Postman collection/environment/examples không chứa secret và chạy pass.
- [ ] CI secret/dependency/lint/type/OpenAPI/migration checks pass.
- [ ] Staging/canary/rollback, PITR restore, SLO/RPO/RTO và on-call được xác nhận.
- [ ] ToS, Privacy, abuse reporting, upstream permission và retention được duyệt.

## 7. Giả định và điểm chưa thể xác minh

- Không xác minh upstream thật, cookie, TTL/format payload, domain support hay điều khoản SmailPro/Sonjj vì không gửi request thật và không đọc secret.
- Không xác minh DB production/Neon, migration đã áp dụng, role/backup/PITR hay dữ liệu hiện hữu.
- Không xác minh Koyeb/Cloudflare/team settings vì project không có deployment artifacts tương ứng.
- Không đánh giá nội dung các file `.env`; báo cáo chỉ dùng cấu hình mẫu và không ghi secret.
- Không thể xác nhận hành vi browser CSP đầy đủ chỉ bằng build; cần E2E trên browser thật, đặc biệt iframe `srcdoc` dưới CSP `frame-src 'none'`.
- Project không phải Git repository tại root, nên không thể dùng Git để chứng minh diff file. Việc kiểm soát phạm vi dựa trên thao tác file trong phiên rà soát.
- Backend tests pass và frontend build pass không bao gồm production DB/upstream/E2E.

## 8. Bằng chứng kiểm tra đầu ra

- File này là Markdown UTF-8 tại root: `SYSTEM_BUILD_PLAN_v2_FIX_PLAN.md`.
- Không chứa giá trị secret/cookie/token/key/payload thực.
- Không chủ động sửa source backend/frontend/Postman hay file kế hoạch gốc.

---Giai đoạn 3 hoàn thành. Tiếp tục Giai đoạn 4 (Tests + Postman + CI/CD).---