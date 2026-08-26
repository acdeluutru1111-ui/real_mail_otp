# Contract fixtures — upstream response variants

Static fixtures fed to `httpx.MockTransport` in `test_adapters_contract.py`.
They intentionally contain **fake** secret-shaped strings (e.g. `key`,
`payload`) so the redaction-guard test can assert none of them ever reach logs.

| File | Represents |
| --- | --- |
| `create_top_level.json` | SmailPro `create` — address/key/timestamp at top level |
| `create_nested_data.json` | SmailPro `create` — nested under `data` |
| `inbox_payload_list.json` | SmailPro inbox — payload in a list-wrapped response |
| `inbox_payload_dict.json` | SmailPro inbox — payload as a bare dict |
| `inbox_empty.json` | SmailPro inbox — empty payload (no mail yet) |
| `sonjj_list_messages_key.json` | Sonjj list under `messages` key (mixed field variants) |
| `sonjj_list_data_key.json` | Sonjj list under `data` key |
| `sonjj_list_bare.json` | Sonjj list as a bare array |
| `detail_message.json` | Detail body via `message` |
| `detail_body.json` | Detail body via `body` |
| `detail_htmlBody.json` | Detail body via `htmlBody` |
| `detail_content.json` | Detail body via `content` |
| `detail_textBody.json` | Detail body via `textBody` only |
| `detail_empty.json` | Empty `{}` detail — expect `UPSTREAM_BAD_RESPONSE` |
| `detail_missing_mid.json` | Detail without `mid` (adapter uses passed mid) |
| `malformed.txt` | Non-JSON body — expect `UPSTREAM_BAD_RESPONSE` |
| `error_401.json` | 401 body — expect `UPSTREAM_AUTH` |
| `error_429.json` | 429 body — expect `UPSTREAM_RATE_LIMIT` |
| `error_500.json` | 500 body — expect `UPSTREAM_BAD_RESPONSE` |
