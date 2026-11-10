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
- 로더는 `weaver.evals`(`server/src/weaver/evals/dataset.py`)다. `load_dataset()`은 `LoadedCase` 목록을 돌려주며, 각 사례를 도메인 모델(`Source`, `Script`, `Candidate`, `EntityIndex`, `ReviseHints`, `Extraction`)로 만든다.

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
- `{"policy": "revise", "edits": [{"from": "…", "to": ["…", …]}]}`: `call`만. `from`은 화자가 있는 발화 줄 하나와 정확히 같아야 하고, `to` 중 하나면 정답이다. `to`의 모든 대안은 한 줄이어야 하며 로더가 각각 `revision_to_thread`로 Script를 만들 수 있는지 확인한다. `edits`에 없는 발화 줄과 헤더·안내문·제목은 바뀌지 않아야 한다. `edits: []`는 "고칠 것이 없다"는 정답이다.
- `to[0]`이 정답 대표값이다. 로더는 `GalaxyCallParser`가 만든 원본 Script에 `to[0]`을 적용해 `LoadedCase.thread`(퇴고 후 정답 Script)를 만든다.

### 좌표계와 근거 범위

- 오프셋은 Unicode 코드 포인트 기준 반열린 구간 `[start, end)`다(`Segment`, `Candidate`와 같다).
- `call`의 정제·추출 근거는 **퇴고 후 정답 Script**(`thread`) 좌표다. 원본 스크립트 좌표는 `raw_thread`에 있다. 텍스트 원본은 두 Script가 같다.
- 근거는 오프셋 대신 인용문으로 쓴다. 문자열이면 Script 텍스트에 정확히 한 번 나와야 하고, 여러 번 나오면 `{"text": "…", "occurrence": n}`(1부터)으로 고른다. 로더가 오프셋으로 바꾼다.

### `expected.refine`

- `select`: 정제가 반드시 덮어야 할 범위 목록. 서로 겹칠 수 없다. 로더는 각 범위로 정답 `Candidate`(문장 경계 기본값 0..1)를 만들어 추출 단계 입력으로 준다. 범위 안에 있는 RuleRefiner 트리거를 그대로 붙이므로 `reference_at`(겹치는 첫 Segment의 `occurred_at`, 없으면 Script의 값)이 보존된다. 카톡처럼 메시지 시각과 저장 시각이 다른 사례도 정답 입력만으로 상대 날짜를 풀 수 있다. 신호가 없는 범위는 트리거가 없고 Script·Segment 시각을 쓴다.
- `reject`: 후보에 들어가면 안 되는 범위(잡담 등). `select`와 겹칠 수 없다. 생략하면 빈 목록이다.
- `select: []`는 "후보가 하나도 없어야 한다"는 정답이다.
- RuleRefiner는 날짜·시각·기간·호칭 신호만 고르므로, 신호 없이 사실이 있는 경계 사례는 `input.stage: "extract"`로 두어 정제 채점에서 뺀다(정답 후보는 여전히 `select`).

### `expected.extract`

- `alternatives`: `Extraction`(`server/src/weaver/schema/internal.py`) 목록. 그중 하나와 맞으면 정답이다. 정답이 여러 개 허용되는 사례는 대안을 나열하고 `multi_answer` 태그를 붙인다.
- `alternatives: []`는 **빈 정답**이다. 추출할 것이 없으며 실행기는 `Extractor`가 `None`이거나 대상·기록·일정·관계가 모두 빈 결과를 정답으로 본다. 이 경우 `empty` 태그를 붙인다. 키가 없는 것(누락)은 형식 오류다.
- 각 대안은 `Extraction`과 제안 참조 무결성(`validate_proposal_references`)을 통과해야 한다. 기록 유형은 `요청 | 논의 | 결정 | 폐기 | 약속 | 일정 | 변경`, 관계는 `원인 | 후속조치 | 같은논의 | 참석 | 소속`이다. AI 결과이므로 `supersedes`·`retracts`·`resolves`는 `null`이다.
- 입력에 없는 사실은 정답이 될 수 없다. 로더가 검사하는 것: 모든 `thread_excerpt`(최상위와 각 항목)는 공백 제외 4자 이상이고 `select` 범위 하나 안의 문자열이다. 새 대상의 이름과 별칭은 그 항목의 `thread_excerpt` 안에 나온다(NFKC·casefold 정규화 후). 기존 대상(`@키`)은 `type`이 known과 같고, 이름은 known의 이름이나 별칭 중 하나이며, 별칭은 known 별칭이거나 근거 안에 나온다. 사실이 있는데 `select`가 비면 거부한다.
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

로더가 강제하는 일관성: `empty` ⇔ `alternatives: []`, `multi_answer` ⇔ 대안 2개 이상(또는 퇴고 `to` 2개 이상), `call`의 `stt_*` 태그 ⇔ `edits`가 비지 않음. 모든 대안에서 `link_pending`이면 `ask`가 있고, `affiliation`이면 `소속`, `attendance`이면 `참석` 관계가 있으며, `new_entity`이면 새 대상, `request`이면 `요청`, `cancel`이면 `폐기` 기록이 있다. `summarize()`와 `python -m weaver.evals`가 채널·태그·정답 유형(빈 정답, 복수 정답, 확인 질문, 기존 연결, 새 대상)·기록 유형·관계 유형별 개수를 보여 준다.

## 공개 데이터 규칙

- 모든 샘플은 실제 자료를 변형하지 않고 처음부터 쓴 가짜 내용이다. 실제 개인·회사·내부 대화, 실제 통화를 쓰지 않는다. 날짜는 2031년, 사람·회사 이름은 지어낸 것이다. 실제 기록 시각을 옮기지 않으며 모든 타임스탬프와 `…년` 표기는 203x년만 쓴다. 전화번호(휴대·유선)·계좌번호·주민번호 형태의 숫자를 넣지 않는다(테스트가 검사한다).
- STT 오류는 텍스트로 흉내 낸 것이며 실제 음성 인식 정확도를 주장하지 않는다.

## 피드백 승격 경로 (#27)

`weaver.loom.eval_candidates`가 승인 피드백을 원문 없는 비공개 후보(`state_root/eval_candidates/`, 후보 파일 `format_version: 1`)로 내보낸다. 사람이 후보의 구조(정정 종류·유형·슬롯)만 보고 이 형식의 사례를 처음부터 쓴다. `mark_authored`가 잔존 검사와 함께 사례 형식과 타임스탬프 연도(203x)를 검증하고, `mark_reviewed` 뒤 `promote(store, root, candidate_id, Path("server/evals/cases"))`가 다시 검증해 `cases/feedback-<hash>.json`으로 쓴다. `<hash>`는 `case_id`·`origin`을 뺀 내용의 SHA-256 앞 12자이고, 승격본의 `case_id`는 `feedback-<hash>`, `origin`은 `{"kind": "feedback", "versions": …}`(프롬프트·모델 버전)다. 작성자가 쓴 `case_id`는 덮어쓴다.

## 평가 러너 (#11)

레포 루트에서 실행한다. 인자 없는 `python -m weaver.evals`는 기존처럼 데이터 개수를
출력한다. 별도 경로 검증은 `validate --cases PATH`를 쓴다.

```sh
uv run --project server python -m weaver.evals run
uv run --project server python -m weaver.evals run \
  --cases server/evals/cases --fixtures server/evals/fixtures.json \
  --output reports/evals.json --baseline server/evals/baseline.json
# 검토한 새 기준을 명시적으로 저장한다. 기존 기준을 자동 갱신하지 않는다.
uv run --project server python -m weaver.evals run \
  --output reports/new.json --save-baseline reports/new-baseline.json
# 유료 실행: 기존 weaver.config의 설정과 존재하는 state_root가 필요하다.
uv run --project server python -m weaver.evals run --live \
  --config /path/to/config.toml --max-cost-usd 2 --confirm-budget \
  --output reports/live.json
```

기본 실행은 설정 파일·키·로그인·모델 다운로드 없이 실제 `RuleRefiner`,
`LlmExtractor`, `LlmReviser`, `Spinner.spin(Source)`를 호출한다. Spinner의 입력 파일과
SQLite 체크포인트는 사례별 임시 디렉터리에 만들고 지운다. 사용자 볼트는 읽지 않는다.
STT는 호출하지 않는다(ADR-028). `input.stage=extract`는 정제·퇴고를 채점하지 않고,
`refine`은 정답 Script에서 정제·추출을, `spin`은 다음 경로를 추가로 실행한다.

- `extract`: 정답 Script + 정답 Candidate의 독립 추출·연결 평가.
- `refine`: 정답 Script에서 실제 RuleRefiner의 선택 범위·트리거 평가.
- `pipeline`: 실제 Spinner가 만든 Script → RuleRefiner → LlmExtractor.
- `raw_pipeline`: call만, 퇴고 전 Script → RuleRefiner → LlmExtractor.
  `pipeline`과의 사례별·범주별 차이로 퇴고의 후속 영향을 확인한다.

`fixtures.json`은 최초 정답에서 작성한 **합성 공급자 응답**의 독립 스냅샷이다.
후보 밖 항목과 공급자 입력에 없는 기존 ID를 제거해 작성했으며, 실행 중 정답을
읽어 응답을 만들거나 보정하지 않는다. 추출 응답은 입력 payload의 canonical JSON
SHA-256으로 구분한다. 입력이 바뀌면 없는 fixture 오류가 나므로 새 입력의 응답을
명시적으로 작성해야 한다. 퇴고 응답은 고정 segment_id/text를 반환한다. fixture 점수는
러너·어댑터·연결 후처리의 회귀 기준이며 실제 모델 성능 추정치가 아니다. 합성 STT
오류를 실제 STT 정확도로 해석하지 않는다. 프롬프트 의미 변경을 검증하려면 live
실행이 필요하다.

### 채점과 대응 규칙

모든 지표는 `numerator`, `denominator`, `score`를 갖는다. 분모가 0이면 score는
`null`(해당 없음)이며 100%로 바꾸지 않는다. 전체·채널·태그 집계는 분자/분모를
합하는 micro 집계다. 하나의 사례가 여러 태그에 속할 수 있다.

- `refine.context_recall`: 정답 select 구간을 **한 Candidate가 완전히 덮으면** 검출.
  Unicode 반열린 구간이며 경계만 닿거나 일부 문자만 겹치면 검출이 아니다.
  분모는 select 구간 수. 겹치는 후보가 여러 개여도 정답은 한 번만 센다.
- `refine.trigger_recall`: 정답 select와 실제 트리거의 교집합 길이가 1 이상이면 검출.
  문맥 창으로만 포함된 사실과 직접 신호가 있는 사실을 구분한다. 분모는 select 수다.
- `refine.reject_clear`: 실제 Candidate와 양의 길이로 겹치지 않은 reject 수 / reject 수.
  `empty_correct`는 select가 빈 사례에서 후보도 비었는지 센다.
  `selected_characters`는 전송 문맥 문자 수 / Script 문자 수이며 진단용이다.
- `entities`, `entry_drafts`, `schedules`, `relations` 각각 정규화된 항목 전체의
  일대일 정확 일치를 TP로 센다. precision=TP/예측 수, recall=TP/정답 수.
  `missing`=(정답 수−TP)/정답 수, `extra`=(예측 수−TP)/예측 수를 별도로 남긴다.
  잘못된 관계 유형·방향은 누락 하나와 추가 하나로 집계된다. `relation.소속` 등
  관계 종류별 precision/recall도 같은 규칙이다.
- `*.field.FIELD`: 정렬된 항목에 Hungarian 최대 가중치 일대일 대응을 적용한다.
  가중치는 같은 필드 개수, 동률은 정규화한 정렬 순서로 결정한다. 해당 필드가 같은
  대응 수 / max(예측 수, 정답 수)이며 누락·추가를 분모에 포함한다.
  `entry_drafts.field.kind`는 기록 유형, `entities.field.type`는 대상 유형,
  `entities.field.entity_id`는 기존/새 대상 연결, 각 `field.ask`는 확인 필요와
  선택지 키 집합, `relations.field.from`/`to`/`type`는 양 끝·방향·종류를 보여 준다.
- canonical 결과는 기존 대상 ID를 데이터의 `@key`로 치환하고, 새 대상 참조는
  (type, NFKC·casefold·공백 정규화 name)으로 바꾼다. draft 참조는
  occurred_at·요청·차단 참조를 포함한 채점 필드 전체, schedule 참조도 채점 필드
  전체로 치환한다. 시각 등이 다른 기록으로 entry_ref를 바꾸면 오연결로 감점한다.
  배열은 정렬하고 중복은 보존한다. ID 재발급과 출력 순서로 점수가 달라지지 않으며,
  서로 다른 동명이인 키는 합치지 않는다. 동일한 새 대상 type/name을 구분하는 근거가
  없는 현재 데이터 형식에서는 해당 참조를 의미상 동일하게 취급한다.
- summary, excerpt, confidence, reason, aliases, 일정 title, 자유 텍스트 value,
  질문 문장과 선택지 라벨은 정확 일치에서 제외한다. request의 구조·내용·기한,
  대상·필드·시각·요청 차단 참조 등 나머지 계약 필드는 비교한다. excerpt는 실제
  LlmExtractor의 후보 내 근거 검증을 반드시 통과해야 한다.
- 여러 정답은 **한 대안 전체**를 선택한다. 전체 exact 우선, 그다음 일대일 대응의
  동일 필드 수 합계가 가장 큰 대안을 택하고 동률은 데이터 순서다. 대안 사이에서
  필드별로 정답을 골라 섞지 않는다. 정답 alternatives가 비면 빈 항목 목록으로 비교한다.
  `None`과 네 항목 목록이 모두 빈 Extraction은 동일한 빈 예측이다.
- `extract.exact` 등은 정규화 결과 전체가 같은 사례 수 / 해당 단계의 사례 수다.
  오류가 난 사례도 exact와 success 분모에 남고 점수는 0이다. 정답이 빈 사례의
  호출 오류도 성공이 되지 않는다. 정답 항목은 recall/missing 분모에 그대로 남는다.
- `revise.lines`: 원본의 `splitlines()` 각 줄에 대해 허용된 to 중 하나와 같거나,
  미편집 줄이면 원문과 같은 줄 수 / max(원본 줄 수, 예측 줄 수). 구조 줄도 포함한다.
  삽입·삭제·순서 변경은 후속 줄 불일치로 드러나며 줄 정렬로 숨기지 않는다.

### 보고서·회귀·비용

`--output`은 전체 JSON과 같은 이름의 `.txt` 요약을 쓴다. JSON에는 데이터 버전·
내용 SHA-256, 실행 ID·UTC 시작 시각, backend/model, 실제 모듈·프롬프트 버전/해시,
fixture 해시, 표본 수, 지표, 범주, 사례별 정규화 예측/정답·대응 인덱스와 원본 예측,
호출별 프롬프트/입력/스키마 해시·지연·오류 종류를 남긴다. live 보고서는 사용량·가격
스냅샷·계산 비용·예약 비용이 있는 ledger도 포함한다. 공급자 사용량을 모르면 실제
비용은 `null`이며 예약액을 실제 비용처럼 쓰지 않는다. 전체 실패도 보고서를 남긴다.

기준 비교는 같은 데이터 해시·보고서/채점 버전만 허용하며 모델·프롬프트·fixture
해시는 달라도 비교한다. 비교 JSON은 전체·범주·사례별 차이와 회귀 여부를 담는다.
`--fixtures`로 별도의 고정 응답 세트를 비교할 수 있고, live에서는 현행 LlmClient가
지원하는 Anthropic 모델을 쓴다(모델 선택 범위 확장 자체는 이 이슈에 포함하지 않는다).
Python `runner.run(cases, factory, backend=...)`의 주입 가능한 LlmClient factory로
다른 모델·프롬프트 설정을 같은 데이터에서 측정할 수도 있다. 모델 출력의 완전한
재현을 보장하지 않는다.

커밋한 `baseline.json`은 초기 기준이다. `--save-baseline`은 전체 지표와 함께
범주·사례별 정제 지표, 단계 exact/success, 퇴고 줄 일치를 보관한다. 기본 허용 하락은
0이며 `--tolerance 0.01`이면 절대 점수 0.01 하락까지 허용한다. missing/extra와
문맥 전송량은 낮을수록 좋은 진단값이므로 하락 회귀 검사를 하지 않는다.
초기 문맥 재현율 **32/33**, 트리거 재현율 **30/33**을 CI에서 보호하며 임의의 100%
하한을 설정하지 않는다. 개선과 악화가 전체 평균에서 상쇄되어도 사례별 gate가 잡는다.
전체 상세 JSON을 기준으로 주면 그 안의 모든 범주·사례별 지표를 비교한다.
종료 코드는 0=정상, 1=단계 오류 또는 기준 대비 회귀, 2=입력/설정/기준 호환 오류다.

live는 `--live`, 양의 유한한 `--max-cost-usd`, `--confirm-budget`이 모두 필요하다.
이는 CLI에서 명시적으로 승인한 금액이며 대화형 확인을 대신한다. 각 요청의 재시도
예약을 공유 월 ledger(기존 $20 상한)와 별도 실행 ledger 양쪽에 먼저 잡으므로 이번
실행 예산으로 월 한도를 우회하지 않는다. 공유 ledger는 UTC 월별로 합산하고,
실행 ledger는 월과 무관하게 해당 실행 DB의 전체 예약·정산 합계를 SQLite 쓰기 잠금
안에서 검사한다. 실행 예산이 부족하면 실제 호출 전에 거절하고 공유 예약도 반환한다.
알 수 없는 비용·중단된 예약은 보수적으로 유지한다. 실행 ledger는 state_root에 남아
감사할 수 있다. 실제 LLM은 테스트·CI에서 사용하지 않는다.

```sh
cd server
uv run pytest tests/evals/test_runner.py
```
