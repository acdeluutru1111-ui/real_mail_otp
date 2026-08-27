# Hướng dẫn triển khai cách sửa 1: truyền `mid` bằng query parameter

> **Phạm vi:** chỉ mô tả cách sửa `mid` từ URL path segment sang query parameter. Tài liệu này không thay đổi mã ứng dụng, không đụng `.env`, không refactor ngoài luồng đọc chi tiết thư.

## 1. Mục tiêu và hợp đồng đích

`mid` là định danh opaque do upstream cung cấp. Ứng dụng phải giữ nguyên chuỗi logic từ danh sách thư đến repository, billing, cache và upstream; không được tự tách hoặc diễn giải các ký tự `/`, `?`, `#`, `%` hay Unicode như cấu trúc URL.

Hợp đồng đích:

| Tầng | Trước | Sau |
|---|---|---|
| SPA | `/inboxes/:id/messages/:mid` | `/inboxes/:id/messages?mid=<encoded>` |
| API | `GET /v1/inboxes/{id}/messages/{mid}` | `GET /v1/inboxes/{id}/messages/detail?mid=<encoded>` |
| FastAPI | `mid` là path parameter | `mid` là query parameter bắt buộc |
| Service/repository/upstream | nhận `mid: str` | giữ nguyên, không đổi chữ ký |

Dùng `/messages/detail` thay vì chính `/messages` để không xung đột với endpoint list hiện có `GET /v1/inboxes/{id}/messages`.

Ví dụ với giá trị logic:

```text
mid = a/b?c#d%25-điện-thư
```

URL trên dây có dạng tương đương:

```text
/inboxes/<id>/messages?mid=a%2Fb%3Fc%23d%2525-%C4%91i%E1%BB%87n-th%C6%B0
/v1/inboxes/<id>/messages/detail?mid=a%2Fb%3Fc%23d%2525-%C4%91i%E1%BB%87n-th%C6%B0
```

`URLSearchParams` mã hóa khi dựng URL và giải mã đúng một lần khi đọc. Không gọi thêm `encodeURIComponent`, `decodeURIComponent`, không `.trim()`, không thay `/`, và không chuẩn hóa Unicode.

## 2. Cơ chế lỗi hiện tại

Luồng hiện tại là:

```text
MessageMeta.mid
  -> MessageList nội suy vào /messages/${m.mid}
  -> React Router đọc :mid bằng useParams()
  -> getMessageDetail nội suy vào API path
  -> FastAPI đọc /{mid}
  -> MessageService -> repository/cache/billing/Sonjj
```

Hai lần nội suy trực tiếp biến dữ liệu opaque thành cấu trúc path:

- `/` tạo thêm segment và có thể làm SPA/API không match route.
- `?` bắt đầu query string; phần sau không còn thuộc `mid` path.
- `#` bắt đầu fragment ở browser và không được gửi lên server.
- `%` có thể tạo escape không hợp lệ hoặc bị giải mã ngoài ý muốn.
- Unicode phụ thuộc việc mã hóa/giải mã URL ngầm ở nhiều tầng.

Hậu quả là backend không nhận đúng chuỗi đã có trong DB. `MessageRepository.get_by_inbox_and_mid()` không tìm thấy, hoặc billing/cache/upstream dùng một `mid` khác.

## 3. Luồng dữ liệu sau khi sửa

```text
MessageMeta.mid (chuỗi gốc)
  -> new URLSearchParams({ mid })
  -> Link: /inboxes/:id/messages?mid=...
  -> MessageDetailPage: searchParams.getAll('mid')
  -> getMessageDetail(inboxId, mid)
  -> apiRequest({ path: .../messages/detail, query: { mid } })
  -> client.ts / URL.searchParams.set('mid', mid)
  -> FastAPI Query nhận chuỗi đã decode đúng một lần
  -> MessageService.read_message_detail(..., mid)
  -> get_by_inbox_and_mid / detail_key / billing dedupe / Sonjj params
```

Bất biến cần kiểm tra tại từng ranh giới:

```text
mid_from_list === mid_from_spa_query
              === mid_passed_to_endpoint
              === mid_received_by_route
              === mid_passed_to_service/repository/upstream
              === response.mid
```

## 4. Ma trận ảnh hưởng theo thư mục/tệp

### Thay đổi bắt buộc

| Tệp | Ký hiệu bị ảnh hưởng | Loại thay đổi |
|---|---|---|
| `frontend/src/router.tsx` | route của `MessageDetailPage` | bỏ `:mid` khỏi path SPA |
| `frontend/src/components/MessageList.tsx` | `MessageList` | dựng query bằng `URLSearchParams` |
| `frontend/src/pages/MessageDetailPage.tsx` | `MessageDetailPage`, `load` | đọc/validate query thay vì `useParams().mid` |
| `frontend/src/api/endpoints.ts` | `getMessageDetail` | dùng path `/messages/detail` và `query: { mid }` |
| `backend/app/api/routes/messages.py` | `read_message_detail` | chuyển `/{mid}` thành `/detail`, dùng FastAPI `Query` |
| `backend/openapi/openapi.yaml` | operation đọc detail | đổi path và `mid.in` thành `query` |
| `backend/tests/test_message_mid_query.py` | tệp test mới | hồi quy parsing/forwarding/status |

### Không cần đổi mã, nhưng phải kiểm tra hồi quy

| Tệp | Lý do không đổi |
|---|---|
| `frontend/src/api/client.ts` | `RequestOptions.query` và `buildUrl()` đã dùng `URL` + `url.searchParams.set`; đây là cơ chế đúng để mã hóa query. |
| `frontend/src/api/types.ts` | `MessageMeta.mid` và `MessageDetail.mid` đã là `string`; không thay DTO. |
| `backend/app/domain/models.py` | `MessageMetaDTO.mid` và `MessageDetailDTO.mid` đã là `str`. |
| `backend/app/domain/services.py` | `MessageService.read_message_detail`, `_get_detail`, `_detail_to_dto` đã truyền `mid: str` xuyên suốt. |
| `backend/app/repositories/message_repo.py` | `get_by_inbox_and_mid(inbox_id, mid)` so sánh equality, không xử lý URL. |
| `backend/app/integrations/sonjj.py` | `get_message_detail()` đã gửi `mid` qua `httpx params={...}`, đúng với dữ liệu opaque. |
| `backend/app/cache/memory.py` | `detail_key(inbox_id, mid)` dùng chính chuỗi nhận được; không phải URL. |
| `backend/app/api/routes/api_v1.py` | vẫn mount `messages.router` dưới `/v1`; không đổi. |

### Tùy chọn

- Cập nhật các tài liệu tổng quan đang ghi endpoint cũ: `PROJECT_OVERVIEW.md`, `SYSTEM_BUILD_PLAN.md`, `SYSTEM_BUILD_PLAN_v2.md`. Việc này không ảnh hưởng runtime nhưng tránh tài liệu drift.
- Thêm Vitest/React Testing Library để tự động hóa test frontend. Hiện `frontend/package.json` chỉ có `dev`, `build`, `preview`, `typecheck` và chưa có test runner.
- Giữ route cũ trong một cửa sổ migration ngắn. Chỉ làm nếu có client ngoài SPA; xem mục 10.

## 5. Thay đổi frontend theo từng tệp

### 5.1 `frontend/src/router.tsx` — route SPA

**Trách nhiệm hiện tại:** `AppRouter()` khai báo route bảo vệ và mount `MessageDetailPage` tại `/inboxes/:id/messages/:mid`.

**Lỗi:** `:mid` buộc React Router xem `mid` như path segment. Một `mid` chứa `/` không còn là một parameter; `?` và `#` có nghĩa URL riêng.

**Trách nhiệm mới:** path chỉ định tài nguyên màn hình; `id` vẫn là path parameter UUID, còn opaque `mid` nằm trong query.

Thay đúng route:

```tsx
<Route
  path="/inboxes/:id/messages"
  element={
    <Protected>
      <MessageDetailPage />
    </Protected>
  }
/>
```

- Input route: `id: string` từ `useParams()`.
- Input query: `mid` do page tự đọc.
- Output: render `MessageDetailPage` trong `Protected` như hiện tại.
- Route `/inboxes/:id` vẫn giữ nguyên; React Router v6 match theo path đầy đủ nên `/messages` không xung đột.

### 5.2 `frontend/src/components/MessageList.tsx` — tạo liên kết

**Trách nhiệm hiện tại:** component trình bày `MessageMeta[]`; mỗi `<li>` dùng `m.mid` làm key và tạo `<Link>` đến chi tiết.

**Lỗi:** ``to={`/inboxes/${inboxId}/messages/${m.mid}`}`` nội suy opaque value vào path.

**Trách nhiệm mới:** dựng query bằng `URLSearchParams`; tuyệt đối không tự nối `?mid=${m.mid}`.

Thay phần tạo `to` bằng một trong hai mẫu tập trung sau. Mẫu object của React Router rõ ràng nhất:

```tsx
{messages.map((m) => {
  const search = new URLSearchParams({ mid: m.mid }).toString();

  return (
    <li key={m.mid} className="message-list-item">
      <Link
        to={{
          pathname: `/inboxes/${inboxId}/messages`,
          search: `?${search}`,
        }}
      >
        {/* Giữ nguyên nội dung hiện tại */}
      </Link>
    </li>
  );
})}
```

`URLSearchParams` nhận `m.mid` nguyên bản và tạo chuỗi mã hóa an toàn. Không thay key, props hoặc nội dung list.

> `inboxId` là UUID nội bộ hiện vẫn ở path. Sửa này chỉ áp dụng cho `mid`.

### 5.3 `frontend/src/pages/MessageDetailPage.tsx` — đọc query

**Trách nhiệm hiện tại:** lấy `{ id, mid }` từ `useParams()`, gọi `getMessageDetail(id, mid)`, hiển thị loading/error/402/detail và link quay lại inbox.

**Lỗi:** `mid` đã bị router/path semantics làm thay đổi trước khi `load()` nhận được.

**Trách nhiệm mới:** chỉ lấy `id` từ path; đọc toàn bộ giá trị `mid` từ query; không gọi API nếu thiếu, rỗng hoặc lặp.

Đổi import:

```tsx
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
```

Đọc query và đặt policy canonical:

```tsx
const { id = '' } = useParams();
const [searchParams] = useSearchParams();
const midValues = searchParams.getAll('mid');
const mid = midValues.length === 1 ? midValues[0] : '';
const invalidMid = midValues.length !== 1 || mid.length === 0;
```

Lưu ý:

- `getAll()` được dùng thay vì chỉ `get()` để phát hiện duplicate.
- Không `.trim()`: khoảng trắng có thể là một phần định danh opaque.
- `?mid=` là rỗng và không hợp lệ.
- `?mid=a&mid=b` là mơ hồ và không hợp lệ, không chọn “first” hoặc “last”.
- `%2F`, `%3F`, `%23`, `%25` và Unicode được React Router/`URLSearchParams` trả về thành đúng chuỗi logic.

Điều chỉnh `load` để chặn request lỗi:

```tsx
const load = useCallback(async () => {
  if (!id || invalidMid) {
    setLoading(false);
    setDetail(null);
    setError(new Error('Missing or invalid message id.'));
    return;
  }

  setLoading(true);
  setError(null);
  try {
    const res = await getMessageDetail(id, mid);
    setDetail(res);
  } catch (err) {
    setError(err);
  } finally {
    setLoading(false);
  }
}, [id, mid, invalidMid]);
```

Giữ nguyên:

- logic `ApiError.status === 402` và nút `/payments`;
- link quay lại ``/inboxes/${id}``;
- render `MessageDetail`, billing và `SanitizedHtml`;
- dependency của `load` phải thay đổi khi query `mid` thay đổi để back/forward navigation tải đúng thư.

**Hành vi lỗi UI:** missing/empty/duplicate không phát request; page dừng spinner và hiển thị lỗi local qua `ErrorBanner`. Không chuyển lỗi này thành 402/404.

### 5.4 `frontend/src/api/endpoints.ts` — API endpoint typed

**Trách nhiệm hiện tại:** `getMessageDetail(inboxId, mid)` trả `Promise<MessageDetail>` và nội suy `mid` vào path.

**Trách nhiệm mới:** giữ nguyên chữ ký/hợp đồng TypeScript, chỉ đổi cách vận chuyển:

```ts
export function getMessageDetail(
  inboxId: string,
  mid: string,
): Promise<MessageDetail> {
  return apiRequest<MessageDetail>({
    method: 'GET',
    path: `/v1/inboxes/${inboxId}/messages/detail`,
    query: { mid },
  });
}
```

- Input: `inboxId: string`, `mid: string` nguyên bản.
- Output: vẫn `Promise<MessageDetail>`.
- `client.ts:buildUrl()` sẽ gọi `url.searchParams.set('mid', String(mid))`.
- Không dùng path có `mid`; không encode trước khi truyền vào `query`.

### 5.5 `frontend/src/api/client.ts` và `types.ts` — kiểm tra, không sửa bắt buộc

`client.ts` hiện đã có:

```ts
query?: Record<string, string | number | undefined>;
// ...
url.searchParams.set(k, String(v));
```

Đây là convention cần tái sử dụng. Có một chi tiết: `buildUrl()` bỏ qua `v === ''`, vì vậy nếu một caller bỏ qua validation và truyền `mid: ''`, request sẽ thành thiếu `mid`, backend trả 422. Không nên sửa hành vi dùng chung chỉ vì endpoint này.

`types.ts` giữ nguyên `mid: string` trong `MessageMeta` và `MessageDetail`; URL encoding không thuộc domain type.

## 6. Thay đổi backend theo từng tệp

### 6.1 `backend/app/api/routes/messages.py` — route và FastAPI Query

**Trách nhiệm hiện tại:** router có prefix `/inboxes/{inbox_id}/messages`; `read_message_detail()` nhận `mid` từ `@router.get('/{mid}')`, inject rate limit/user/session, rồi gọi `MessageService`.

**Lỗi:** ASGI routing đã diễn giải path trước khi domain service nhận `mid`; slash/reserved characters không còn được đảm bảo nguyên vẹn.

**Trách nhiệm mới:** route tĩnh `/detail`, `mid` là query bắt buộc, không rỗng; phát hiện duplicate trước khi gọi service.

Mẫu triển khai nhất quán với FastAPI/Pydantic hiện tại:

```py
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request


@router.get("/detail", response_model=MessageDetailDTO)
async def read_message_detail(
    request: Request,
    inbox_id: str,
    mid: Annotated[str, Query(min_length=1)],
    _rl=Depends(rate_limit("detail")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageDetailDTO:
    mid_values = request.query_params.getlist("mid")
    if len(mid_values) != 1:
        raise ValidationErrorError("Exactly one mid query parameter is required.")

    svc = MessageService(session)
    return await svc.read_message_detail(str(user.id), inbox_id, mid)
```

Bổ sung import `ValidationErrorError` từ `app.core.errors` nếu áp dụng policy reject duplicate như trên.

**Validation và status:**

| Trường hợp | Kỳ vọng |
|---|---|
| thiếu `mid` | FastAPI trả `422` trước khi gọi service |
| `mid=` | `Query(min_length=1)` trả `422` |
| hai hoặc nhiều `mid` | check `getlist()` trả application `VALIDATION_ERROR`, status `422` |
| một `mid` hợp lệ | gọi service đúng một lần với chuỗi đã decode |
| inbox sai/không thuộc user | service trả `NOT_FOUND`, status `404` như hiện tại |
| `mid` không thuộc inbox | service giữ hành vi `VALIDATION_ERROR`, status `422` |
| thiếu tiền | `BILLING_INSUFFICIENT`, status `402` |
| billing conflict | `409` |
| upstream | giữ `429/502/503/504` theo taxonomy hiện tại |

FastAPI mặc định cho lỗi request validation có body 422 chuẩn của FastAPI, trong khi duplicate check dùng application envelope. Đây là drift đã tồn tại; không mở rộng scope sang custom validation handler. OpenAPI chỉ cần cam kết status 422, không cam kết đổi toàn bộ envelope trong sửa này.

**Thứ tự route:** khai báo `@router.get('/detail')` thay cho `/{mid}`. Không giữ dynamic detail route cạnh route mới nếu quyết định cutover dứt điểm.

### 6.2 DTO, service, repository — không đổi chữ ký

#### `backend/app/domain/models.py`

- `MessageMetaDTO.mid: str` và `MessageDetailDTO.mid: str` tiếp tục biểu diễn giá trị domain, không phải encoded URL.
- Không thêm model request body; đây là GET query.

#### `backend/app/domain/services.py`

`MessageService.read_message_detail(user_id, inbox_id, mid)` hiện:

1. xác minh ownership;
2. gọi `message_repo.get_by_inbox_and_mid(inbox.id, mid)`;
3. dùng `mid` trong billing dedupe;
4. gọi `_get_detail(inbox, mid)`;
5. dùng `detail_key(inbox_id, mid)` và single-flight;
6. gọi `SonjjAdapter.get_message_detail(payload, mid, address)`;
7. đối chiếu `raw.mid` và tạo `MessageDetailDTO`.

Không encode/decode trong service. Thay đổi transport kết thúc tại route. Mọi equality/dedupe/cache phải dùng chuỗi logic đã decode đúng một lần.

#### `backend/app/repositories/message_repo.py`

`get_by_inbox_and_mid(inbox_id: UUID, mid: str)` đã query:

```py
Message.inbox_id == inbox_id, Message.mid == mid
```

Không sửa. Test phải chứng minh route chuyển đúng giá trị đến service/repository; không biến `%2F` thành literal `"%2F"`, cũng không biến `%25` hai lần.

#### `backend/app/integrations/sonjj.py`

Adapter đã dùng:

```py
params={"payload": payload, "mid": mid}
```

Đây là cùng convention query-parameter đúng. Không encode trước; `httpx` đảm nhiệm wire encoding.

## 7. OpenAPI

Trong `backend/openapi/openapi.yaml`, xóa path:

```yaml
/v1/inboxes/{id}/messages/{mid}:
```

và thay bằng:

```yaml
/v1/inboxes/{id}/messages/detail:
  get:
    tags: [messages]
    summary: Get message detail with billing (charge only on valid, committed read)
    parameters:
      - $ref: "#/components/parameters/InboxId"
      - name: mid
        in: query
        required: true
        description: Opaque upstream message identifier; transported as one query value without application-level normalization.
        schema:
          type: string
          minLength: 1
    responses:
      "200":
        description: Sanitized message detail + billing info
        content:
          application/json:
            schema: { $ref: "#/components/schemas/MessageDetail" }
      "401": { $ref: "#/components/responses/Unauthorized" }
      "402": { $ref: "#/components/responses/InsufficientBalance" }
      "403": { $ref: "#/components/responses/Forbidden" }
      "404": { $ref: "#/components/responses/NotFound" }
      "409": { $ref: "#/components/responses/Conflict" }
      "422": { $ref: "#/components/responses/ValidationError" }
      "429": { $ref: "#/components/responses/RateLimited" }
      "502": { $ref: "#/components/responses/UpstreamInvalid" }
      "503": { $ref: "#/components/responses/UpstreamUnavailable" }
      "504": { $ref: "#/components/responses/UpstreamTimeout" }
```

Giữ schema `MessageMeta.mid` và `MessageDetail.mid` là `string`. Không mô tả một regex giả định vì `mid` là opaque. Thêm 422 vì missing/empty/duplicate là input không hợp lệ.

Sau triển khai, so sánh authored spec với `/openapi.json`; dự án hiện duy trì hai nguồn OpenAPI và có nguy cơ drift.

## 8. Kế hoạch test hồi quy

### 8.1 Ma trận dữ liệu bắt buộc

Dùng chuỗi logic, không dùng chuỗi đã percent-encode làm fixture đầu vào:

| Ca | `mid` logic | Điều cần chứng minh |
|---|---|---|
| slash | `a/b` | một query value; backend nhận `a/b` |
| question | `a?b` | `?` không mở query thứ hai |
| fragment | `a#b` | `#b` được gửi đến server, không thành browser fragment |
| percent | `a%b` hoặc `a%2Fb` | `%` và literal `%2F` không bị double decode |
| kết hợp | `a/b?c#d%25` | equality end-to-end |
| Unicode | `thư-điện-tử-你好` | round-trip UTF-8 đúng nguyên chuỗi |
| missing | không có key | 422 API; UI không gọi API |
| empty | `mid=` | 422 API; UI không gọi API |
| duplicate | `mid=a&mid=b` | 422; không chọn first/last; service không chạy |

Có thể thêm khoảng trắng đầu/cuối để bảo vệ quy tắc “không trim”.

### 8.2 Backend route tests — bắt buộc

Tạo `backend/tests/test_message_mid_query.py`. Dùng app test của FastAPI, override auth/session và monkeypatch `MessageService` để test ranh giới HTTP mà không chạm DB/upstream/charge thật.

Pseudocode trọng tâm:

```py
import pytest
from fastapi.testclient import TestClient

@pytest.mark.parametrize(
    "mid",
    ["a/b", "a?b", "a#b", "a%b", "a%2Fb", "thư-điện-tử-你好", "a/b?c#d%25"],
)
def test_detail_query_preserves_mid_exactly(client, service_spy, mid):
    response = client.get(
        f"/v1/inboxes/{INBOX_ID}/messages/detail",
        params={"mid": mid},
    )
    assert response.status_code == 200
    assert service_spy.received_mid == mid
    assert response.json()["mid"] == mid


def test_missing_mid_is_422_and_service_not_called(client, service_spy): ...
def test_empty_mid_is_422_and_service_not_called(client, service_spy): ...
def test_duplicate_mid_is_422_and_service_not_called(client, service_spy):
    response = client.get(
        f"/v1/inboxes/{INBOX_ID}/messages/detail",
        params=[("mid", "a"), ("mid", "b")],
    )
    assert response.status_code == 422
    assert not service_spy.called
```

Dùng `params=` của HTTP client để test giá trị logic. Chỉ một test riêng có thể dùng URL raw để chứng minh `%252F` round-trip thành literal `%2F`:

```py
# wire `%252F` -> query value logic `%2F`, không phải `/`
client.get(f".../messages/detail?mid=a%252Fb")
assert received_mid == "a%2Fb"
```

Bổ sung route table regression:

```py
paths = {route.path for route in messages.router.routes}
assert "/inboxes/{inbox_id}/messages/detail" in paths
assert "/inboxes/{inbox_id}/messages/{mid}" not in paths
```

Nếu fixture app dùng global exception handler, assert duplicate có `error.code == 'VALIDATION_ERROR'`. Với missing/empty do FastAPI tạo, chỉ assert 422 và không gọi service để tránh mở rộng scope envelope.

### 8.3 Service/repository tests — bắt buộc ở mức forwarding

Không cần viết lại toàn bộ billing suite cho thay đổi transport. Thêm test nhỏ bằng fake repository hoặc spy:

```py
mid = "a/b?c#d%25-điện-thư"
await service.read_message_detail(USER_ID, INBOX_ID, mid)
assert message_repo.requested_mid == mid
assert billing_repo.requested_mid == mid
assert sonjj.requested_mid == mid
```

Nếu setup service quá nặng, route spy + adapter contract test là tối thiểu bắt buộc. Không gọi upstream thật và không để test tạo charge thật.

Mở rộng `backend/tests/contract/test_adapters_contract.py` bằng test `SonjjAdapter.get_message_detail()` ghi nhận `req.url.params.get('mid')` và assert bằng chuỗi logic với reserved chars/Unicode. File này đã dùng `httpx.MockTransport`, phù hợp convention hiện tại.

### 8.4 Frontend tests

#### Bắt buộc: kiểm tra build và kiểm tra chức năng thủ công

Vì hiện không có test runner frontend, thực hiện ít nhất:

1. Render list có từng `mid` trong ma trận.
2. Mở link và kiểm tra address bar có `/messages?mid=...`, không có `mid` trong path.
3. Trong Network, request là `/messages/detail?mid=...`.
4. Backend spy/log test an toàn xác nhận giá trị logic; không log dữ liệu thư hoặc secret.
5. Back/forward giữa hai query `mid` khác nhau phải tải đúng thư.
6. Truy cập missing/empty/duplicate: không có request detail và UI hết spinner, có lỗi.

#### Khuyến nghị: thêm test runner (tùy chọn về hạ tầng, bắt buộc nếu CI yêu cầu tự động hóa frontend)

Nếu thêm Vitest + React Testing Library, bổ sung script `test` trong `frontend/package.json` và test:

- `MessageList` tạo link; parse `href` bằng `new URL(..., location.origin)`, rồi assert `url.searchParams.get('mid') === originalMid` và `url.pathname` không chứa `mid`.
- `MessageDetailPage` trong `MemoryRouter` đọc query, mock `getMessageDetail`, assert tham số chính xác.
- missing/empty/duplicate không gọi mock endpoint.
- thay đổi `initialEntries`/navigation giữa hai query làm fetch lại.

Không assert trực tiếp exact percent-encoding nếu browser có encoding tương đương; assert round-trip qua `URL.searchParams` và pathname.

### 8.5 OpenAPI regression

Parse/spec-lint nếu công cụ sẵn có, hoặc ít nhất search:

```text
Có:  /v1/inboxes/{id}/messages/detail
Không còn: /v1/inboxes/{id}/messages/{mid}
mid.in == query
mid.required == true
mid.schema.minLength == 1
422 được khai báo
```

## 9. Thứ tự triển khai an toàn

1. **Chốt compatibility:** xác nhận không có client ngoài SPA cần endpoint path cũ.
2. Thêm backend tests fail trước cho query, reserved chars, Unicode, missing/empty/duplicate.
3. Đổi FastAPI route sang `/detail` + `Query(min_length=1)` + duplicate rejection.
4. Cập nhật authored OpenAPI và kiểm tra `/openapi.json` runtime.
5. Đổi `getMessageDetail()` sang `query: { mid }`.
6. Đổi SPA router, sau đó `MessageList` tạo query bằng `URLSearchParams`.
7. Đổi `MessageDetailPage` sang `useSearchParams`, chặn input không canonical.
8. Chạy backend suite, frontend typecheck/build và test thủ công/automated.
9. Search toàn repo để bảo đảm không còn route/runtime interpolation cũ.
10. Deploy backend và frontend cùng release nếu cutover dứt điểm; kiểm tra 404/422/402 và billing reopen.

## 10. Compatibility và migration

### Quyết định khuyến nghị: cutover dứt điểm

Đây là dự án v1 và source hiện chỉ có một caller frontend cho `getMessageDetail`. Khuyến nghị xóa route cũ, cập nhật frontend/backend cùng release. Lợi ích:

- không duy trì hai hợp đồng;
- không khuyến khích tiếp tục đưa opaque value vào path;
- route table và OpenAPI rõ ràng;
- không cần migration DB/data vì giá trị `mid` lưu trữ không đổi.

### Nếu bắt buộc có client cũ

Tùy chọn giữ `/{mid}` trong một release chỉ cho `mid` path-safe, đánh dấu deprecated trong OpenAPI và đo usage bằng metadata không nhạy cảm. Không redirect bằng cách ghép raw `mid` vào URL. Route mới là canonical; frontend luôn dùng query. Sau cửa sổ migration, xóa route cũ.

Không thể làm route cũ hỗ trợ đáng tin cậy `#` vì fragment không bao giờ đến server; vì vậy compatibility route không phải cách sửa reserved characters.

### Duplicate query

Quyết định canonical là **reject 422**. Không dùng “last wins” mặc định vì có thể làm UI, proxy và backend chọn khác nhau, gây lookup/billing cho sai thư.

## 11. Lệnh xác minh trên Windows

Chạy từ `D:\real_mail_otp`. Các lệnh dưới đây không cần sửa `.env` cho typecheck/unit tests đã mock; full app/integration có thể cần cấu hình local hiện hữu.

### Backend

```bat
cd /d D:\real_mail_otp\backend
python -m pytest tests\test_message_mid_query.py -q
python -m pytest tests\contract\test_adapters_contract.py -q
python -m pytest -q
```

Kiểm tra route runtime (khi backend đang chạy theo port launcher hiện tại là 8099):

```bat
python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8099/openapi.json')); print('/v1/inboxes/{inbox_id}/messages/detail' in d['paths'])"
```

Không dùng lệnh chứa token thật trong command history. Kiểm tra authenticated endpoint qua UI hoặc client kiểm thử an toàn.

### Frontend

```bat
cd /d D:\real_mail_otp\frontend
npm run typecheck
npm run build
```

Nếu đã thêm script test:

```bat
npm test -- --run
```

### Rà soát tham chiếu bằng PowerShell

```powershell
Set-Location D:\real_mail_otp
Get-ChildItem frontend\src,backend\app,backend\openapi -Recurse -File |
  Select-String -SimpleMatch 'messages/:mid','messages/{mid}','messages/${mid}'

Get-ChildItem frontend\src -Recurse -File |
  Select-String -Pattern 'URLSearchParams|useSearchParams|query:\s*\{\s*mid'
```

Kết quả mong đợi: không còn route/interpolation runtime cũ; có `URLSearchParams`, `useSearchParams`, và `query: { mid }` tại đúng các tệp nêu trên. Các build plan lịch sử có thể vẫn chứa endpoint cũ nếu chưa thực hiện thay đổi tài liệu tùy chọn.

## 12. Tiêu chí nghiệm thu

- [ ] SPA canonical là `/inboxes/:id/messages?mid=...`; `mid` không nằm trong path.
- [ ] API canonical là `GET /v1/inboxes/{id}/messages/detail?mid=...`.
- [ ] Link được dựng bằng `URLSearchParams`, API query được dựng qua `RequestOptions.query`/`URL.searchParams`.
- [ ] `MessageDetailPage` dùng `useSearchParams()`/`getAll('mid')`, không dùng `useParams().mid`.
- [ ] Backend dùng FastAPI `Query(min_length=1)` và reject duplicate.
- [ ] `/`, `?`, `#`, `%`, literal `%2F`, Unicode và chuỗi kết hợp round-trip đúng nguyên giá trị logic.
- [ ] Missing, empty và duplicate đều không chạy service; API trả 422; UI không treo spinner.
- [ ] `MessageService`, repository, cache key, billing dedupe và Sonjj nhận cùng `mid`.
- [ ] Không thay đổi DTO/schema response ngoài vị trí parameter.
- [ ] 200/401/402/404/409/422/429/502/503/504 giữ semantics hiện có.
- [ ] Reopen cùng `mid` không double-charge; `billing.charged` vẫn false theo logic hiện tại.
- [ ] Authored OpenAPI không còn path detail cũ và khai báo `mid` query bắt buộc, `minLength: 1`, response 422.
- [ ] `python -m pytest -q`, `npm run typecheck`, `npm run build` pass.
- [ ] Không có secret, `.env`, raw message body, payload, cookie/key/token bị thêm vào code/test/log/tài liệu.
- [ ] Không có refactor không liên quan.

## 13. Rollback

Nếu release gặp lỗi:

1. rollback frontend và backend cùng phiên bản để tránh SPA mới gọi API cũ hoặc ngược lại;
2. khôi phục route SPA `/inboxes/:id/messages/:mid`, endpoint API `/{mid}` và OpenAPI cũ như một đơn vị;
3. không rollback DB vì thay đổi này không có migration/schema/data;
4. cache RAM có key dựa trên chuỗi `mid` và tự hết TTL; không cần data migration;
5. kiểm tra ledger/billing trước khi retry thủ công, tránh suy đoán charge từ HTTP failure;
6. giữ các test reserved-character làm bằng chứng lỗi và tái triển khai sau khi sửa.

Nếu đã chọn compatibility window, rollback nhẹ hơn là tạm giữ cả route cũ và mới, nhưng frontend vẫn nên quay về đúng endpoint tương ứng với backend đang chạy.

## 14. Ranh giới phạm vi

Không thực hiện trong cách sửa này:

- đổi cấu trúc DB hoặc độ dài cột `mid`;
- đổi billing transaction/dedupe;
- đổi cache format;
- đổi Sonjj/SmailPro protocol ngoài việc xác nhận adapter đã dùng query;
- chuẩn hóa/trim/lowercase Unicode;
- thêm custom global FastAPI validation envelope;
- sửa auth, polling, payment, pricing hoặc `.env`;
- refactor API client dùng chung.

Mọi thay đổi ngoài danh sách bắt buộc phải được tách thành công việc riêng.