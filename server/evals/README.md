# Evals

Loom·Spinner의 퇴고·정제·추출 결과가 모델이나 프롬프트를 바꾼 뒤 나빠지는지 보는 평가 세트다. pytest 단위 테스트와 분리하며, 형식 검증은 실제 LLM·음성 모델이나 사용자 볼트에 접근하지 않는다. 실행기는 #11에서 만든다.

```sh
cd server
uv run pytest tests/evals/test_dataset.py   # 전체 샘플 형식·무결성·최소 개수 검증
uv run python -m weaver.evals               # 채널·태그·정답 유형별 개수 출력
```

## 배치와 버전

- `cases/<case_id>.json` 한 파일이 한 사례다. 파일 이름은 `case_id`와 같아야 한다. 사례별 파일이라 리뷰 diff가 작고, 피드백 승격 파일(`feedback-<hash>.json`)도 같은 디렉터리에 그대로 들어간다.
- 모든 사례는 `schema_version: 1`(정수)을 가진다. 형식이 호환되지 않게 바뀌면 버전을 올리고 로더가 이전 버전을 거부하거나 변환한다.
- 로더는 `weaver.evals`(`server/src/weaver/evals/dataset.py`)다. `load_dataset()`은 `LoadedCase` 목록을 돌려주며, 각 사례를 도메인 모델(`Source`, `Thread`, `Candidate`, `EntityIndex`, `ReviseHints`, `Extraction`)로 만든다.

## 사례 형식

모든 객체는 정의되지 않은 필드를 거부한다. "필수"는 키가 반드시 있어야 한다는 뜻이고, `null`이 허용되는지는 따로 적는다.

| 필드 | 필수 | 내용 |
|---|---|---|
| `schema_version` | 예 | 정수 `1` |
| `case_id` | 예 | `^[a-z0-9]+(-[a-z0-9]+)*$`, 세트 안에서 고유 |
| `channel` | 예 | `call`(갤럭시 통화 스크립트 텍스트), `kakao`(카톡 내보내기), `share_text`(공유 텍스트). ADR-028 기준이며 `voice_memo`는 v1에서 받지 않는다 |
| `title` | 예 | 사람이 읽는 한 줄 설명 |
| `reference.at` | 예 | 고정 기준 시각. 시간대 오프셋이 있는 RFC 3339. `Source.captured_at`과 `occurred_at`이 된다 |
| `reference.timezone` | 예 | IANA 시간대. `reference.at`의 오프셋이 이 시간대의 그 시각 오프셋과 같아야 한다 |
| `tags` | 예 | 아래 범주 태그 목록(중복 금지, 빈 목록 가능) |
| `input.stage` | 예 | 가장 먼저 채점할 단계: `spin`(퇴고부터), `refine`(정제부터), `extract`(추출만) |
| `input.text` | 예 | 원본 텍스트 그대로. 통화는 스크립트 전체, 카톡은 내보내기 파일 전체 |
| `input.counterpart` | 아니오 | 통화 상대 힌트(`Source.hints.counterpart`, `ReviseHints.counterpart`). 없으면 `null` |
| `known.entities` | 예 | 기존 대상 목록(빈 목록 가능). 각 항목은 `key`, `type`, `name`, `aliases` |
| `expected.revise` | `spin`이면 예 | 퇴고 기대값 |
| `expected.refine` | 예 | 후보 범위 기대값이자 추출 단계의 정답 입력 |
| `expected.extract` | 예 | 추출·연결 기대값 |
| `notes` | 아니오 | 판정 근거 설명 |
| `origin` | 아니오 | 승격된 사례만. `{"kind": "feedback", "versions": {...}}` |

### 기존 대상 키와 ID

`known.entities[].key`(`^[a-z][a-z0-9_]*$`)는 사례 안에서 고유한 안정 키다. 로더가 `case_id`와 키로 결정적인 `ent_` ID를 만들고 `LoadedCase.entity_ids`(키 → ID)로 대응을 준다. 기대값에서는 `ent_` ID를 직접 쓰지 않고 `"@키"`로 쓴다(`entity_id`, `entities`, `from`/`to`, `request`, `ask.choices` 어디서든). 리터럴 `ent_…`나 없는 키는 거부한다. 실행기는 이 대응으로 채점하므로 임의 ID 차이로 오답이 되지 않는다. 같은 이름의 대상(동명이인)은 키만 다르게 여러 개 둘 수 있다.

새 대상(`entity_id: null`)은 로컬 `ref`로 식별하므로 실행기는 `type`과 정규화한 `name`(NFKC·casefold·공백 정리)으로 짝을 맞춘다. `ref` 이름 자체는 비교하지 않는다.

### `expected.revise`

- `{"policy": "keep"}`: 텍스트 원본(`kakao`, `share_text`)은 퇴고하지 않는다. 퇴고 결과가 원문과 같아야 한다.
- `{"policy": "revise", "edits": [{"from": "…", "to": ["…", …]}]}`: `call`만. `from`은 화자가 있는 발화 줄 하나와 정확히 같아야 하고, `to` 중 하나면 정답이다. `edits`에 없는 발화 줄과 헤더·안내문·제목은 바뀌지 않아야 한다. `edits: []`는 "고칠 것이 없다"는 정답이다.
- `to[0]`이 정답 대표값이다. 로더는 `GalaxyCallParser`가 만든 원본 Thread에 `to[0]`을 적용해 `LoadedCase.thread`(퇴고 후 정답 Thread)를 만든다.

### 좌표계와 근거 범위

- 오프셋은 Unicode 코드 포인트 기준 반열린 구간 `[start, end)`다(`Segment`, `Candidate`와 같다).
- `call`의 정제·추출 근거는 **퇴고 후 정답 Thread**(`thread`) 좌표다. 원본 스크립트 좌표는 `raw_thread`에 있다. 텍스트 원본은 두 Thread가 같다.
- 근거는 오프셋 대신 인용문으로 쓴다. 문자열이면 Thread 텍스트에 정확히 한 번 나와야 하고, 여러 번 나오면 `{"text": "…", "occurrence": n}`(1부터)으로 고른다. 로더가 오프셋으로 바꾼다.

### `expected.refine`

- `select`: 정제가 반드시 덮어야 할 범위 목록. 서로 겹칠 수 없다. 로더는 각 범위로 정답 `Candidate`(문장 경계 기본값 0..1, 트리거 없음)를 만들어 추출 단계 입력으로 준다.
- `reject`: 후보에 들어가면 안 되는 범위(잡담 등). `select`와 겹칠 수 없다. 생략하면 빈 목록이다.
- `select: []`는 "후보가 하나도 없어야 한다"는 정답이다.
- RuleRefiner는 날짜·시각·기간·호칭 신호만 고르므로, 신호 없이 사실이 있는 경계 사례는 `input.stage: "extract"`로 두어 정제 채점에서 뺀다(정답 후보는 여전히 `select`).

### `expected.extract`

- `alternatives`: `Extraction`(`server/src/weaver/schema/internal.py`) 목록. 그중 하나와 맞으면 정답이다. 정답이 여러 개 허용되는 사례는 대안을 나열하고 `multi_answer` 태그를 붙인다.
- `alternatives: []`는 **빈 정답**이다. 추출할 것이 없으며 실행기는 `Extractor`가 `None`이거나 대상·기록·일정·관계가 모두 빈 결과를 정답으로 본다. 이 경우 `empty` 태그를 붙인다. 키가 없는 것(누락)은 형식 오류다.
- 각 대안은 `Extraction`과 제안 참조 무결성(`validate_proposal_references`)을 통과해야 한다. 기록 유형은 `요청 | 논의 | 결정 | 폐기 | 약속 | 일정 | 변경`, 관계는 `원인 | 후속조치 | 같은논의 | 참석 | 소속`이다. AI 결과이므로 `supersedes`·`retracts`·`resolves`는 `null`이다.
- 입력에 없는 사실은 정답이 될 수 없다. 로더가 검사하는 것: 모든 `thread_excerpt`(최상위와 각 항목)가 `select` 범위 하나 안의 문자열이다. 새 대상 이름이 원문에 나온다. 사실이 있는데 `select`가 비면 거부한다.
- 확인 질문: 동명이인·별칭만 일치·작성 시각 불명처럼 확정할 근거가 없으면 `ask`를 둔다. 동명이인은 `entity_id: null`과 후보 `choices`로, 별칭만 맞는 경우는 새 대상+`ask` 또는 기존 대상+`ask` 대안을 함께 허용한다.
- 관계 방향: `소속`은 사람 → 조직, `참석`은 사람(또는 조직) → 회의, `원인`은 원인 → 결과(`this`는 이 원본), `후속조치`는 후속 항목 → 원래 일, `같은논의`는 세부 → 상위 주제·프로젝트.

실행기가 비교하는 값(#11 권장): 대상의 연결 결과(키)·`type`·이름, `ask` 유무와 `choices` 키 집합, 기록의 `kind`·대상·`field`·`request` 유무와 `blocked_by_request` 유무, 일정의 `start`/`end`, 관계의 양 끝·`type`·방향. `summary`, `thread_excerpt`, `confidence`, `reason`, 질문 문장, 자유 텍스트 `value`는 사람이 검토하는 근거이며 정확 일치로 채점하지 않는다.

### 날짜 정규화

- 상대 날짜의 기준: 카톡은 메시지 시각(Segment `occurred_at`), 통화·공유 텍스트는 `reference.at`이다.
- `내일`·`모레`는 기준일 +1·+2일, `이번 주 X요일`·`X요일`은 기준일이 속한 월요일 시작 주의 X요일(지났으면 다음 주 — 해당 사례 `notes`에 적는다), `다음 주 X요일`은 그다음 주의 X요일이다.
- `…까지` 마감은 그날 23:59, 시각만 있고 오전/오후가 없으면 업무 맥락의 오후로 보되 사례 `notes`에 근거를 적는다. 시각이 없거나(`오전`, `다음 달`) 장소가 없으면 일정·값을 만들지 않는다.
- 기대값의 모든 `Timestamp` 필드(`start`, `end`, `occurred_at`, `due_at`)는 `reference.timezone`의 오프셋으로 쓴다. 다른 시간대로 말한 시각(뉴욕·런던)은 이 시간대로 환산하고, 자유 값 `value`에는 원문 시간대를 보존해도 된다.
- 작성 시각을 알 수 없는 공유 텍스트의 상대 날짜는 확정하지 않고 `ask`로 남긴다(`missing_reference`).

## 범주 태그

| 범주 | 태그 |
|---|---|
| STT 오류(`call`만) | `stt_name`, `stt_number`, `stt_word`, `stt_spacing` |
| 대상 연결 | `pseudonym`(가명·닉네임), `homonym`, `alias`, `link_pending`, `new_entity` |
| 시간 | `relative_date`, `absolute_date`, `timezone`, `missing_reference` |
| 내용 | `request`, `decision`, `promise`, `discussion`, `cancel`, `reschedule`, `change`, `chitchat`, `missing_info` |
| 관계 | `relation_direction`, `affiliation`, `attendance`, `follow_up` |
| 정답 형태 | `empty`, `multi_answer`, `boundary` |

로더가 강제하는 일관성: `empty` ⇔ `alternatives: []`, `multi_answer` ⇔ 대안 2개 이상(또는 퇴고 `to` 2개 이상), `call`의 `stt_*` 태그 ⇔ `edits`가 비지 않음. `summarize()`와 `python -m weaver.evals`가 채널·태그·정답 유형(빈 정답, 복수 정답, 확인 질문, 기존 연결, 새 대상)·기록 유형·관계 유형별 개수를 보여 준다.

## 공개 데이터 규칙

- 모든 샘플은 실제 자료를 변형하지 않고 처음부터 쓴 가짜 내용이다. 실제 개인·회사·내부 대화, 실제 통화를 쓰지 않는다. 날짜는 2031년, 사람·회사 이름은 지어낸 것이다. 전화번호·계좌번호·주민번호 형태의 숫자를 넣지 않는다(테스트가 검사한다).
- STT 오류는 텍스트로 흉내 낸 것이며 실제 음성 인식 정확도를 주장하지 않는다.

## 피드백 승격 경로 (#27)

`weaver.loom.eval_candidates`가 승인 피드백을 원문 없는 비공개 후보(`state_root/eval_candidates/`, 후보 파일 `format_version: 1`)로 내보낸다. 사람이 후보의 구조(정정 종류·유형·슬롯)만 보고 이 형식의 사례를 처음부터 쓴다. `mark_authored`가 잔존 검사와 함께 사례 형식을 검증하고, `mark_reviewed` 뒤 `promote(store, root, candidate_id, Path("server/evals/cases"))`가 다시 검증해 `cases/feedback-<hash>.json`으로 쓴다. `<hash>`는 `case_id`·`origin`을 뺀 내용의 SHA-256 앞 12자이고, 승격본의 `case_id`는 `feedback-<hash>`, `origin`은 `{"kind": "feedback", "versions": …}`(프롬프트·모델 버전)다. 작성자가 쓴 `case_id`는 덮어쓴다.
