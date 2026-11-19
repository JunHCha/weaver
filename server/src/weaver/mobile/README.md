# 폰 전용 HTTPS API — 전송 계약 v1 (`/mobile/v1`)

이 문서가 **계약의 기준**이다([ADR-040](../../../../docs/domain/decisions/040-tailscale-courier-http-transport.md)의
HTTP 계약 초안은 이 문서를 가리킨다). 요청·응답 예시는 공유 fixture
[`schema/mobile/fixtures/*.json`](../../../../schema/mobile/fixtures/)에 있고, 서버 테스트가 생성·검증한다
(`server/tests/mobile/test_fixtures.py`). Android의 FakeMobileServer는 같은 fixture를 응답으로 쓴다.
계약을 바꾸면 fixture를 다시 만든다.

```sh
WEAVER_UPDATE_FIXTURES=1 uv run --project server pytest server/tests/mobile/test_fixtures.py
uv run --project server pytest server/tests/mobile            # 이후에는 비교만 한다
```

운영 절차(설정·페어링·복귀)는 저장소 루트 README의 "폰 전용 HTTPS API"에 있다. 도메인
`schema_version`(Manifest v1, Receipt v1, ReviewPackage v2, Verdict v2, Action v2, ActionResult v2)과 전송 계약
버전(`capabilities.transport_version`=1)은 독립이다.

## 구성

| 파일 | 역할 |
|---|---|
| `api.py` | FastAPI 앱 팩토리, 라우트, 오류 형식, operation·시간 표기 |
| `auth.py` | Host·Origin → whois → bearer token → node 대조 (순수 ASGI 미들웨어) |
| `whois.py`, `tls.py` | tailscale whois(CLI/LocalAPI, 5초 캐시), 로컬 CA·서버 인증서 |
| `store.py` | `mobile.sqlite3`(0600): devices(token 해시), operations(공통 journal), feed, checkpoint |
| `captures.py`, `reviews.py`, `edits.py`, `read.py`, `feed.py` | 기능별 서비스 |
| `applier.py`, `transport.py` | 데몬 안에서 접수 작업을 적용하는 worker, 기능별 전송 전환 |
| `cli.py` | `weaver mobile serve/pair/devices/revoke/cert/status/reconcile` (Typer) |

## 공통 규칙

- **시간**: 응답의 모든 datetime은 UTC이며 `Z`로 끝난다(`2026-11-05T00:00:00Z`). 순간은 보존되고 현지 오프셋은
  남기지 않는다(일정의 `timezone` 필드는 별도). 날짜(`effective_deadline` 등)는 `YYYY-MM-DD`.
  Wiki `frontmatter` 값은 문서 내용이므로 변환하지 않는다.
- **목록**: 모든 목록 응답은 `{items, next_cursor: string|null}`이고 마지막 페이지의 `next_cursor`는 `null`이다.
  `cursor`는 불투명 문자열이다. 한 번에 주는 목록(`reviews`, `actions`, `tags`, `timeline`, `dictionary`,
  `wiki/pages`)은 항상 `next_cursor: null`이다. **예외: 변경 피드**(`/changes`)는 재개 지점이 필요하므로
  `next_cursor`가 항상 있다(아래).
- **선택 필드**: 값이 없는 선택 필드는 생략할 수도 `null`로 보낼 수도 있고, 둘은 같은 뜻이다(예: Manifest의
  `capture_host`). 폰은 두 형태를 모두 받아들여야 하고, 보낼 때도 어느 쪽이든 서버가 같게 처리한다.
- **오류**: 본문은 `{code, retryable, operation_id?, ...}`뿐이다. 원문·예외 메시지·경로는 없다.
  응답은 `Cache-Control: no-store`.
- **인증**: 모든 요청에 `Authorization: Bearer <token>`. 필요한 scope는 아래 표.

## 오류 분류 (폰의 처리)

| 응답 | 의미 | 폰의 처리 |
|---|---|---|
| 401 `invalid_token`·`token_revoked`, 403 `node_mismatch` | 토큰·기기 결합이 틀림 | **다시 페어링 필요** |
| 403 `scope_denied`·`not_executor` | 이 기기에 그 기능 권한 없음 | 해당 기능만 blocked, 나머지는 계속 |
| 403 `peer_unverified`, 400 `invalid_host`, 403 `origin_not_allowed` | tailscale이 모르는 peer·잘못된 호스트 | 확정 거부. 재시도하지 않고 설정 확인 |
| **503 `whois_unavailable`** (`retryable: true`, `Retry-After: 2`) | tailscaled가 그 순간 답하지 못함 | 재시도(backoff). 재페어링 아님 |
| 409/412/422 | 내용 충돌·stale·검증 실패 | 코드별로 처리, 무한 재시도 금지 |
| 5xx, 연결 끊김, timeout | 결과 불명 | 같은 키로 재시도 |

403은 확정 거부에만 쓴다. whois 일시 실패는 항상 503이다.

## 페어링 QR

서버가 `weaver mobile pair`로 만든다(네트워크로 토큰을 발급하는 엔드포인트는 없다). 형식:

```json
{"v": 1, "server_url": "https://100.106.199.83:8787", "server_id": "srv_…", "device_id": "dev_…",
 "token": "wvr_…", "ca_sha256": "<hex>", "ca_der_b64": "<base64 DER>"}
```

폰은 leaf가 아니라 **CA(`ca_der_b64`, SHA-256이 `ca_sha256`)를 고정**하고 서버 인증서 체인을 그 CA로 검증한다.
호스트 이름 검증은 `server_url`의 IP와 서버 인증서의 SAN(IP)을 대조한다. 그래서 leaf가 갱신되거나
tailnet IP가 바뀌어도 CA가 같으면 다시 페어링하지 않는다. CLI는 QR만 출력하며 payload 텍스트(토큰 포함)는
`--print-json`일 때만 출력한다. 예시: `schema/mobile/fixtures/pairing.json`.

## 기능별 전송 경로

`capabilities.transports`의 `capture`·`review`·`action`이 `files`(Syncthing 폴더)|`http` 중 무엇을 쓰는지
알려 준다. **서버 설정이 단일 기준이고 폰은 이 값을 읽어 따른다**(폰 UI는 상태 표시만, 독자 전환 없음).
`capabilities.features`는 기능 제공 여부다: `capture·review·action·read·dictionary·edits`는 `true`,
`alerts`·`pipeline`은 **`false`** — 서버가 아직 HTTP로 제공하지 않으므로 폰은 해당 엔드포인트를 부르지 않고
그 폴더(`proposals/alert_*`, `proposals/pipeline/`)를 files(Syncthing)로 계속 읽는다. Verdict를 HTTP로
바꿔도 알림·지도는 끊기지 않아야 한다.

## 엔드포인트

scope: `read`, `capture`, `review`, `action`(+실행 폰 1대), `edit`. 표시 없는 경로는 인증된 모든 기기.

### 기본

| 요청 | 응답 |
|---|---|
| `GET /capabilities` | `{server_id, transport_version, schema_versions, transports:{capture,review,action}, features:{…}, limits:{max_payload_bytes, max_json_bytes, max_feed_items, max_feed_bytes}, feed_epoch, device:{device_id, scopes, executor}}` |
| `GET /operations/{id}` | operation(아래). 접수한 기기만 조회, 아니면 404 `operation_not_found` |

### operation

모든 비동기 쓰기(Verdict, ActionResult)와 편집은 같은 모양을 돌려준다.

```json
{"operation_id": "op_…", "state": "queued|applied|rejected|failed", "code": "…",
 "kind": "verdict|action_result|tag_edit|dictionary_edit", "target_id": "…",
 "target_revision": 1, "updated_at": "2026-11-19T00:00:00Z"}
```

- `queued`: 접수됨/적용 대기. `code`가 왜 기다리는지 알려 준다(`apply_waiting`(LLM 게이트), `apply_backoff`,
  `apply_failed` …). 계속 폴링한다.
- `applied`: 도메인 journal이 반영을 확인했다(`code` 없음).
- `rejected`: 도메인이 거절했다. 다시 보내지 말고 상태를 갱신한다(`stale_rev`, `result_conflict`,
  `unknown_action`, `invalid_edit`, `history_thread_readonly` …).
- `failed`: 서버 쪽에서 멈췄다(`apply_stopped`, `payload_lost`). 운영자 조치 후 서버 재시작 때 다시 시도한다.

같은 `client_id`/키로 다시 보내 기존 operation을 돌려받으면 **소유자가 현재 기기로 갱신된다**(재페어링 뒤에도
조회 가능).

### capture (scope `capture`)

| 요청 | 의미 |
|---|---|
| `PUT /captures/{id}/payload` | `Content-Length`·`X-Content-SHA256` 필수. 201 `{capture_id, state:"staged"}`, 같은 내용 200, 이미 보관됐으면 200 `state:"stored"`. 400 `length_mismatch`, 411, 413 `too_large`, 422 `sha256_mismatch`·`invalid_sha256`, 409 `payload_conflict`·`capture_conflict`. **Receipt가 아니다** |
| `PUT /captures/{id}/manifest` | Manifest v1. 201 Receipt v1(archive 보관 뒤), 재전송 200(최초 Receipt). 409 `payload_missing`(retryable), `manifest_conflict`, `capture_conflict`, `payload_mismatch` |
| `GET /captures/{id}/receipt` | 200 Receipt, 없으면 404 `receipt_not_found`. 응답 유실 뒤 본문 재전송 전에 조회 |

한도: payload 64 MiB(`max_payload_bytes`), JSON 1 MiB. 이어 올리기는 없고 항상 전체 본문. 미확정 staging은 7일 뒤 정리.

### 검토 (scope `review`)

| 요청 | 응답 |
|---|---|
| `GET /reviews` | `{items:[{package_id, revision, source_id, title, source_type, stage, etag}], next_cursor:null}` |
| `GET /reviews/{id}/revisions/{rev}` | ReviewPackage v2 + `ETag` |
| `PUT /reviews/{id}/revisions/{rev}/verdict` | Verdict v2 + `If-Match`(패키지 ETag) → **202 operation**(`Location`). 같은 키·내용 재전송은 같은 operation(적용 후에도). 409 `verdict_conflict`, 412 `stale_revision`(`current_revision`)·`etag_mismatch`, 428 `if_match_required`, 404 `package_not_found` |

### Action (scope `action`, 실행 폰 1대 — 아니면 403 `not_executor`)

| 요청 | 응답 |
|---|---|
| `GET /actions` | `{items:[Action v2], next_cursor:null}` seq 순. `transports.action`이 `http`일 때만(아니면 409 `transport_files`) |
| `GET /actions/{id}` | Action v2. 자기 lineage만 조회한다. 없거나 지금 내려 주지 않는 것은 404 `action_not_found` |
| `PUT /actions/{id}/result` | ActionResult v2 → **202 operation**. 409 `result_conflict`, 404 `unknown_action`, 422 `invalid_result`·`id_mismatch` |

### 변경 피드 (인증된 모든 기기, 항목은 scope별로 걸러짐)

`GET /changes?cursor=<epoch>.<seq>&limit=<=100` →
`{feed_epoch, items:[{seq, kind, id, revision, op:"upsert|tombstone", body}], next_cursor, has_more}`.
`kind`: `review`(scope review; body에 etag·stage), `action`(scope action; body 없음, 내용은 `GET /actions`),
`fabric`(scope read; 읽기 모델이 바뀌었다는 힌트, revision=Fabric head seq). 영속 `seq`+`feed_epoch`, 리소스별 최신
항목만 남는 압축 로그이므로 **cursor 없이 부르면 곧 스냅샷**이다. epoch가 다르거나 head보다 앞선 cursor는
410 `cursor_expired` → 처음부터 다시. 페이지를 로컬에 모두 적용한 뒤 `PUT /devices/self/checkpoint`
`{cursor}`로 확인한다(후퇴하지 않고 body의 기기 ID는 무시).

### 읽기 모델 (scope `read`)

웹의 분류 함수(`web.presentation`, `fabric.state.project`, `relations`, `mentionMatch`)를 그대로 쓴다.
**폰은 서버가 계산한 값을 그대로 쓰고 재계산하지 않는다.**

- `GET /read/threads?cursor&limit(≤500, 기본 500)` → `{items:[Thread], next_cursor, head}`. seq 순이고
  `cursor`는 불투명하다. Thread는 도메인 Thread 필드에 다음을 더한다: `presentation`(`fact|history`), `open`(미결 요청),
  `retracted`, `effective_deadline`(`YYYY-MM-DD`|null), `effective_schedule`(객체|null, 폰이 객체를 읽는다),
  `mentions`(사람·조직 태그 중 counterpart가 아닌 것), `source`(표시 라벨: 통화/카카오는 상대 이름, 음성은 "메모",
  공유는 "공유 텍스트", 보관본이 없으면 "알 수 없음"). `counterpart`·`tags`는 사전의 정식 이름과 태그정정을
  반영한 유효 값이다. 값은 요청 시점의 전체 스냅샷으로 계산하고, 데이터베이스가 바뀔 때까지 같은 스냅샷을 쓰므로
  페이지를 넘겨도 다시 계산하지 않는다. 이후 이력 Thread가 생기면 이전 항목을 다시 받아야 값이 갱신된다.
- `GET /read/threads/{id}` → Thread. 404 `thread_not_found`.
- `GET /read/now` → `{today, overdue, due_today, recent}`(각 항목 `{thread, due_kind}`). 마감 지난 미결 요청, 오늘
  마감·오늘 일정, 어제 0시 이후 기록된 사실. 날짜 경계는 설정의 `time.timezone`.
- `GET /read/timeline?subject&mentions&history` → `{items:[{thread, membership:"counterpart|mention|area",
  history}], next_cursor:null}`. 사람·조직은 counterpart 기준(`mentions=true`면 태그로만 언급된 Thread도
  `mention`으로), 영역은 태그 기준. 이력은 `history=true`일 때만, 대상 사실의 소속을 따른다.
- `GET /read/tags`, `GET /read/states?tag=…`: 웹 `/api/tags`, `/api/states`와 같은 내용(`tags`는 `{items,
  next_cursor}`로 감싼다).
- `GET /read/dictionary` → `{items:[{name, kind, aliases, usage_count, archived}], next_cursor:null}`.
- `GET /read/wiki/pages` → `{items:[{id, title, kind, area, updated_at, frontmatter, sha256}], next_cursor:null}`
  (본문 없음). `GET /read/wiki/pages/{id}` → 같은 필드 + `body`, `ETag`=`"<sha256>"`, `If-None-Match` → 304.
  본문은 폰이 열 때 받아 캐시한다. 사람 수정이 아직 반영되지 않은 페이지는 마지막으로 커밋한 버전을 준다.
  404 `page_not_found`.

### 편집 (scope `edit`)

웹 편집 로직(`web.editing`)을 그대로 재사용한다. 멱등 키는 본문 `client_id`(8~64자 `[A-Za-z0-9_-]`).

- `POST /edits/tags` `{client_id, thread_ids, add, remove, kinds}` → 200 operation + `result:{changed, skipped,
  correction_ids, skipped_threads}`. 이력 Thread는 422 `history_thread_readonly`(아무것도 쓰지 않음), 새 태그는 `kinds`
  필요(422 `kind_required`), 404 `thread_not_found`.
- `POST /edits/dictionary/{rename|kind|alias-add|alias-remove|merge}` → 200 operation + `result:{name}`. 422
  `one_change_required`·`kind_choice_required`·`invalid_edit`, 404 `entry_not_found`.
- 같은 `client_id`·같은 내용의 재전송은 최초 결과(`Idempotent-Replay: true`), 다른 내용은 409 `client_id_conflict`.
  거절된 요청도 같은 `client_id`로 같은 거절을 다시 받는다. 응답을 못 받은 재전송(502/503 포함)은 결정적
  Thread ID로 Fabric 행을 중복 만들지 않는다(`correction_ids`는 항상 원래 ID).

## 멱등과 공통 journal

| 쓰기 | 키 | 공통 journal |
|---|---|---|
| capture | `capture_id` + sha | `ArchiveStore.captures`(파일 경로와 동일) |
| Verdict | `(package_id, revision)` + 정규화 JSON 해시 | 결정 journal(`Courier` 적용 경로) + operations |
| ActionResult | `action_id` | `ActionOutbox` + operations |
| 편집 | `client_id` + 내용 해시 | operations(결정적 Thread ID) |

HTTP 요청은 durable inbox(operations)만 커밋하고 202를 답한다. 데몬의 `mobile` 유지보수 작업이
`Courier.apply_external`(파일 경로와 같은 적용·재시도·backoff·`apply_stopped`)과 `ActionCourier.apply_result`로 적용한다.
적용 뒤 operation 갱신 전에 종료돼도 도메인 journal이 막아 중복 적용이 없다. 재시작하면 중지된 Verdict를 다시 시도한다.

## 인증 (서버 설정)

`[mobile] whois`의 기본값은 **`cli`**다: `tailscale whois --json <ip:port>`를 실행한다. `tailscale_cli`를
비우면 macOS 앱 번들(`/Applications/Tailscale.app/Contents/MacOS/Tailscale`)이 있으면 그것을, 없으면 PATH의
`tailscale`을 쓴다(App Store판은 `/var/run/tailscaled.socket`이 없어 `localapi` 모드를 쓸 수 없다. 이 Mac에서
앱 번들 CLI의 whois가 0.09초에 성공함을 실측했다). `localapi`는 소켓이 있는 배포판용이다. 결과는 peer `ip:port`
기준 **5초 동안 캐시**한다(성공만; 토큰은 키에 넣지 않으며 실패는 기억하지 않는다). 토큰은 256비트, 해시 저장,
node(+로그인 사용자) 결합, scope 분리다.

## 전송 전환 시 재시작 순서

설정(`transport`/`[mobile.features]`)을 바꾼 뒤 두 프로세스를 **이 순서로** 재시작한다. `mobile serve`는 시작할 때
설정을 읽어 `capabilities.transports`와 HTTP 디스패치를 정하고, 데몬(`weaver run`)은 파일 게시와 재게시를 정한다.

- **files → http**: ① 데몬 재시작(Action 파일을 비운다) → ② `mobile serve` 재시작(HTTP 디스패치와 capabilities를 연다).
- **http → files**: ① `mobile serve` 재시작(HTTP 디스패치를 닫고 capabilities가 `files`가 된다) → ② 데몬 재시작
  (미결 검토 패키지·Action을 폴더에 다시 게시한다). 데몬을 띄우기 전에 `weaver mobile reconcile`로 재게시만 먼저 할 수도 있다.

어느 쪽이든 실행기 두 대가 같은 Action을 받는 구간이 없고, 이미 보낸 capture·Verdict·결과는 어느 경로로 늦게 와도
공통 journal이 한 번만 반영한다.

## 범위 밖

Alert, `/pipeline`, vault 스냅샷·첨부 API는 아직 없다(`features.alerts/pipeline=false`). 검토 패키지 파일
(`proposals/`) 게시는 설정과 무관하게 계속된다.
