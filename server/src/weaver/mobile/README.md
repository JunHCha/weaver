# 폰 전용 HTTPS API (`/mobile/v1`)

[ADR-040](../../../../docs/domain/decisions/040-tailscale-courier-http-transport.md) 2단계 서버 구현이다.
운영 절차(설정·페어링·복귀)는 저장소 루트 README의 "폰 전용 HTTPS API"에 있다.
이 문서는 Android가 맞출 **전송 계약 v1**과 설계 결정을 적는다. 도메인 `schema_version`과
독립이며, 기존 JSON 모델(Manifest v1, Receipt v1, ReviewPackage v2, Verdict v2, Action v2,
ActionResult v2)은 그대로 본문에 쓴다.

## 구성

| 파일 | 역할 |
|---|---|
| `api.py` | FastAPI 앱 팩토리. 라우트·오류 형식 |
| `auth.py` | Host·Origin 검사 → whois → bearer token → node 대조 (순수 ASGI 미들웨어) |
| `whois.py`, `tls.py` | tailscaled whois(LocalAPI/CLI), 로컬 CA·서버 인증서 |
| `store.py` | `mobile.sqlite3`(0600): devices(token hash), operations(공통 journal), feed, checkpoint |
| `captures.py`, `reviews.py`, `edits.py`, `read.py`, `feed.py` | 기능별 서비스 |
| `applier.py`, `transport.py` | 데몬 안에서 접수 작업을 적용하는 worker, 기능별 전송 전환 |
| `cli.py` | `weaver mobile serve/pair/devices/revoke/cert/status/reconcile` (Typer) |

## 인증과 오류

모든 경로는 `/mobile/v1/...`이고 모두 인증한다. `Host`는 설정한 `bind:port`와 정확히 같아야
하고(아니면 400 `invalid_host`), `Origin`이 있으면 403 `origin_not_allowed`다. 다음 순서로
거절한다: 403 `peer_unverified`(whois 불가·미등록 peer, fail closed) → 401 `invalid_token` →
401 `token_revoked` → 403 `node_mismatch`(토큰에 묶인 node와 peer가 다르거나 로그인 사용자가
다름) → 403 `scope_denied`/`not_executor`. 오류 본문은 `{code, retryable, operation_id?, ...}`뿐이고
원문·예외 메시지·경로를 담지 않는다. 응답은 `Cache-Control: no-store`다.

scope: `read`(읽기 모델·피드), `capture`, `review`, `action`(+ 실행 폰 1대), `edit`.
`GET /capabilities`와 `GET /operations/{id}`, `GET /changes`, `PUT /devices/self/checkpoint`는 인증된
모든 기기에 열려 있다(피드 항목은 scope별로 걸러진다).

## 엔드포인트

| 요청 | 의미 |
|---|---|
| `GET /capabilities` | `server_id`, `transport_version`, `schema_versions`, 기능별 `transports`, `limits`, `feed_epoch`, 자기 기기 scope |
| `PUT /captures/{id}/payload` | `Content-Length`·`X-Content-SHA256` 필수. 스트리밍 검증·fsync 뒤 201 `staged`, 같은 내용 200, 다른 내용 409 `payload_conflict`, 이미 보관된 capture면 200 `stored`/409 `capture_conflict`. 413 `too_large`, 422 `sha256_mismatch`, 400 `length_mismatch`. **Receipt가 아니다** |
| `PUT /captures/{id}/manifest` | Manifest v1. 본문 미도착 409 `payload_missing`(retryable). archive에 보관한 뒤 Receipt v1로 201, 재전송은 최초 Receipt로 200. 같은 ID·다른 메타데이터는 409 `manifest_conflict` |
| `GET /captures/{id}/receipt` | 영속 Receipt 200, 없으면 404. 응답 유실 뒤 본문 재전송 전에 조회 |
| `GET /reviews` | 미결 최신 revision 요약 `{items:[{package_id, revision, source_id, title, source_type, stage, etag}]}` |
| `GET /reviews/{id}/revisions/{rev}` | ReviewPackage v2 + `ETag` |
| `PUT /reviews/{id}/revisions/{rev}/verdict` | Verdict v2, `If-Match`=패키지 ETag. 202 + `operation_id`(`Location`). 같은 키·내용 재전송은 같은 operation(적용 후에도), 다른 내용 409 `verdict_conflict`, 최신이 아닌 revision 412 `stale_revision`(`current_revision`), ETag 불일치 412 `etag_mismatch`, 헤더 없음 428 |
| `GET /operations/{id}` | `queued / applying / applied / failed / stale` + 고정 `error` 코드. 접수한 기기만 조회. `applied`는 도메인 journal이 확인한 뒤에만 |
| `GET /actions`, `GET /actions/{id}` | 실행 폰 전용. 해당 기능이 `http`일 때만(아니면 409 `transport_files`). seq 순, lineage 순서·external_id는 outbox가 보장 |
| `PUT /actions/{id}/result` | ActionResult v2 → 202. 같은 결과 재전송은 같은 operation, 다른 결과 409 `result_conflict`, 모르는 action 404 |
| `GET /changes?cursor&limit` | 변경 피드(아래) |
| `PUT /devices/self/checkpoint` | `{cursor}` 적용 확인. 후퇴하지 않으며 body의 기기 ID는 보지 않는다 |
| `GET /read/now` · `threads` · `threads/{id}` · `tags` · `states` · `timeline` · `wiki/pages` · `wiki/pages/{id}` | 읽기 모델(아래) |
| `POST /edits/tags` · `POST /edits/dictionary/{rename,kind,alias-add,alias-remove,merge}` | 태그·사전 편집(아래) |

한도(`capabilities.limits`): payload 64 MiB(설정 `max_payload_bytes`), JSON 1 MiB, 변경 피드 100건/4 MiB.
이어 올리기는 없고 항상 전체 본문을 다시 보낸다. 미확정 staging은 7일 뒤(설정 `staging_days`)
정리되며, 이미 archive에 들어간 capture는 지우지 않는다.

## 멱등과 공통 journal

| 쓰기 | 키 | 공통 journal |
|---|---|---|
| capture | `capture_id` + sha | `ArchiveStore.captures` (파일 경로와 동일) |
| Verdict | `(package_id, revision)` + 정규화 JSON 해시 | 결정 journal(`Courier` 적용 경로, `reviews` 표) + operations |
| ActionResult | `action_id` | `ActionOutbox`(상태·결과 대조) + operations |
| 태그·사전 편집 | 클라이언트가 만든 `client_id`(8~64자) + 내용 해시 | operations (결정적 Thread ID로 Fabric 재생도 안전) |

HTTP 요청은 durable inbox(operations 행)만 커밋하고 202를 답한다. 데몬의 `mobile` 유지보수
작업이 `Courier.apply_external`(파일 경로와 같은 적용·재시도·backoff·`apply_stopped`)과
`ActionCourier.apply_result`로 적용한다. 적용 뒤 operation 갱신 전에 종료돼도 도메인 journal이
`already applied`로 막아 중복 Thread·적용이 생기지 않는다. 재시작하면 중지된 Verdict를 다시 시도한다.
LLM 게이트가 닫혀 있으면 operation은 `applying`/`apply_waiting`으로 남는다.

## 변경 피드

`GET /changes?cursor=<epoch>.<seq>&limit=<=100`은
`{feed_epoch, items:[{seq, kind, id, revision, op, body}], next_cursor, has_more}`를 돌려준다.
`kind`는 `review`(미결 패키지, body에 etag·stage), `action`(body 없음; 내용은 `GET /actions`),
`fabric`(읽기 모델이 바뀌었다는 힌트; revision=Fabric head seq). `op`는 `upsert|tombstone`.
영속 단조 `seq`와 `feed_epoch`를 쓰고, 도메인 상태와 마지막 기록을 대조해 차이만 기록한다
(영속 producer checkpoint + 재조정; 메모리 큐 없음). 로그는 리소스별 최신 항목만 남도록 압축되므로
**cursor를 주지 않으면 곧 스냅샷**이다. 다른 epoch(서버 재설치·복원)이거나 head보다 앞선 cursor는
410 `cursor_expired` → 폰은 처음부터 다시 받는다. 페이지를 모두 로컬에 적용한 뒤에만 checkpoint를
보낸다. 필터로 걸러진 항목이 있어도 `next_cursor`는 원본 페이지 끝으로 나아가므로 멈추지 않는다.
Fabric seq 커서(`/read/threads`)와 피드 cursor는 별개다.

## 읽기 모델

웹의 분류 함수(`web.presentation`, `fabric.state.project`, 웹의 `relations`·`mentionMatch`)를 그대로
쓴다. `GET /read/threads?after_seq&limit`는 seq 순 페이지이며 각 항목은 Thread에
`presentation`(`fact|history`), `open`(미결 요청), `retracted`, `effective_deadline`,
`effective_schedule`을 더한다. `counterpart`와 `tags`는 사전의 정식 이름과 태그정정을 반영한 유효 값이다
(웹 `/api/threads`와 같다). 유효 값은 요청 시점에 전체 스냅샷으로 계산하므로, 이전에 받은 Thread의 값은
이후 이력 Thread가 생기면 다시 받아야 한다. `GET /read/tags`, `/read/states`는 웹 `/api/tags`,
`/api/states`와 같은 결과다.

- `GET /read/now`: 마감 지난 미결 요청(`overdue`), 오늘 마감·오늘 일정(`due_today`), 어제 0시 이후 기록된 사실
  (`recent`). 날짜 경계는 설정의 `time.timezone`.
- `GET /read/timeline?subject&mentions&history`: 사람·조직은 counterpart 기준(`mentions=true`면 태그로만
  언급된 Thread를 `mention`으로 추가), 영역은 태그 기준. 이력 Thread는 `history=true`일 때만 나오고 대상 사실의
  소속을 따른다. 사전에 사람·조직으로 등록된 이름은 태그에 없어도 사람으로 취급한다.
- `GET /read/wiki/pages[/id]`: `pages/` 아래 Wiki 페이지의 `page_id`(불투명), 제목, `kind`, `area`, frontmatter,
  본문, 전체 파일 SHA-256(`ETag`, `If-None-Match` → 304). 사람 수정이 아직 반영되지 않은 페이지는 마지막으로
  커밋한 버전을 보여 준다. 경로·`.obsidian`·첨부는 노출하지 않는다.

## 편집

웹 편집 로직(`web.editing.apply_tags`, `edit_entry`, `add_alias`, `remove_alias`, `merge`)을 그대로 재사용한다.
이력 Thread 태그 편집은 422 `history_thread_readonly`로 거절되며 아무것도 쓰지 않는다. 같은 `client_id`로 다시
보내면 최초 결과를 돌려주고(`Idempotent-Replay: true`) 내용이 다르면 409 `client_id_conflict`다. 새 태그에는
종류(`kinds`)가 필요하다(422 `kind_required`). 거절된 요청도 같은 `client_id`로 같은 거절을 다시 받는다.

## ADR-040 초안에서 달라진 점·범위 밖

- 구현한 범위는 이슈 #172의 6개 묶음이다. **Alert, PipelineItem(`/pipeline`), vault 스냅샷·첨부 API는 이번 범위
  밖**이다(ADR의 해당 행은 후속 이슈에서 구현).
- `GET /fabric/threads` 대신 `/read/threads`, `/vault/*` 대신 `/read/wiki/pages`를 쓴다.
- 변경 피드는 로그 압축형이다(위). `cursor_expired`는 epoch 불일치·미래 cursor일 때만 발생한다.
- 검토 패키지 파일(`proposals/`) 게시는 기능 설정과 무관하게 계속된다(폰이 HTTP를 고르면 무시하면 된다).
  `files`로 돌아올 때는 미결 패키지를 다시 게시한다.
- 기능 전송 경로 설정은 `capabilities.transports`로 폰에 알려 주며 폰은 서버 값을 따른다.
