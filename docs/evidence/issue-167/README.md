# #167 태그 편집 검증 — PR #170 리뷰 반영

2026-11-19, `feat/web-tags-167`에서 실행했다.
환경: Python **3.12.13** (`uv sync --project server --locked --python 3.12`),
Node 22.22.0, pnpm 10.4.0, uv 0.9.2.

## 필수 검사 결과

| 검사 | 결과 |
| --- | --- |
| 서버 locked sync·`import weaver`·CLI `--help` | 통과, lock 변경 없음 |
| `uv run --project server pytest` | **1648 passed, 1 skipped, 1 warning in 117.95s** |
| Ruff check | **All checks passed!** |
| Ruff format check | **174 files already formatted** |
| mypy strict | **Success: no issues found in 86 source files** |
| 웹 frozen-lockfile install·tokens:check·lint·typecheck | 통과 |
| `pnpm --dir web test` | **11 files, 79 passed** |
| `pnpm --dir web build` | **Compiled successfully; static pages 7/7; Exporting 2/2** |
| `git diff --check` | 통과 |

서버 경고는 기존 Starlette/httpx 사용 중단 예고다. Android와 공유 Thread 스키마는
변경하지 않았으며, 웹 출처 표시와 무시 기록은 기존 DB의 부가 테이블에 저장한다.

## 리뷰 항목별 근거

| 항목 | 변경 및 테스트 |
| --- | --- |
| B1 합치기 | 별칭 버튼을 제거하고 남는 이름·별칭·최종 종류를 보여 주는 대화상자로 통일; 종류가 다르면 직접 종류 선택 필수; `requires an explicit resulting kind for a manual cross-kind merge`, `test_dismissal_persists_in_dictionary_and_cross_kind_merge_needs_choice` |
| B2 추천 | 같은 종류만; 띄어쓰기·별칭 겹침은 길이 제한 없음; 1글자 오타는 양쪽 4자 이상이며 사람·조직 제외; 서버 DB에 무시 저장; `tagSuggestions.test.ts`의 짧은 인명·종류 불일치·띄어쓰기·별칭·무시·그룹화·2,000개 성능 테스트 및 무시 기록 로딩 전 추천 미노출 회귀 |
| B3 일괄 | 배치 API 하나, 최대 500개, 실효 값 읽기와 전체 정정 추가를 같은 BEGIN IMMEDIATE 안에서 실행; 변경·건너뜀 수와 이유 표시; `test_batch_atomic_counts_limit_and_unknown_removal`, `test_batch_rolls_back_first_insert_when_second_insert_fails`, `api.test.ts`의 토큰 1회 조회 테스트 |
| B4 이력 | 읽기 API와 같은 presentation 함수로 태그정정·거절·철회·Wiki 필드 변경·실행의 단건/배치 쓰기 422; 화면 편집 및 전체 선택 제외; `test_history_rejects_single_and_batch_without_side_effects` 6종류, `excludes every history class from editing and bulk selection` |
| 철회 원자성·출처 | 같은 잠금 안에서 확인·추가, 웹 편집 메타데이터 필수, 본문 문구로 판정하지 않음; `test_concurrent_undo_has_one_retraction_and_requires_explicit_provenance`의 동시 요청 8개 |
| 나중 변경 보존 | 철회 Thread만 추가하고 나중 정정을 유지; 사전의 0회 사용 항목도 유지, 서버 시간 제한 없음; `test_undo_tag_correction_appends_retraction_and_preserves_later_edits`, 서버 README |
| 폴링 | 저장 중 낙관적 칩 보존, 폴링 중지 및 이미 진행 중인 응답 폐기; `cancels an in-flight poll and pauses further polls while optimistic tags save` |
| 결과 피드백 | 무변경과 건너뛴 ID·이유 표시, 사전 밖 이름 삭제 허용; `uses one atomic batch call, reports skipped rows and removes unknown tag names`, `reports a no-op batch and does not offer undo` |
| 팝업 | 스크롤/크기 변경 시 anchor 재측정, 위/옆 배치와 viewport 경계 제한; 카드가 사라지면 닫힘; 위치 좌표를 검사하는 `keeps optimistic chips during a poll, repositions on scroll/resize and closes when the card disappears`, `closes an open popup when polling removes its card` |
| 접근성 | dialog 이름, IME 대응 combobox/listbox·방향키/Enter, 저장 후 +로 포커스 복귀, 행의 되돌리기 및 행 부재 시 toast 포커스, 종류 텍스트, 삭제 표적 24px; `does not submit during Korean IME composition`, 카드 직접 편집 테스트 |
| 조회·충돌 | 칩은 공유 Map으로 사전 조회, 생성 직전 재조회, 충돌한 표준 이름 명시; `requires a kind for creation, refreshes first and names a newly conflicting entry`, `test_new_tag_conflict_names_existing_entry_and_preserves_kind` |
| 표시 | 보관 프로젝트 칩을 흐리게 표시하고 보관 문구 유지; 일괄 범위 “선택한 N개에”, 스크롤 가능한 칩, 최근 사용일 연도, 주제 문구; 카드/사전 테스트와 아래 스크린샷 |

완료 조건의 단건 편집·일괄 편집·새 항목 생성·합치기 제안·되돌리기는
`TagWorkflow.test.tsx`의 12개 UI 회귀로 검증한다. 서버 웹 편집 테스트는
71개이며, 기존 3,000개 Thread 성능 회귀도 함께 통과했다.

## 실제 합성 서버 확인

모든 경로를 `/tmp/weaver-167-synthetic` 아래로 둔 임시 설정과
`[llm] backend = "none"`을 사용했다. 직접 만든 합성 Thread와 사전만 사용했고
실제 LLM·음성 모델·사용자 vault·기기 시험 폴더에 접근하지 않았다.

Orca 내장 브라우저에서 종류/보관 칩과 자동완성, 3개 선택 후 단일 배치
저장(1개 변경·2개 건너뜀), 새 `/api/corrections/{id}/undo` 호출 **200**,
합치기 확인 대화상자의 표시와 확인 후 합치기를 검증했다.

- [카드: 종류·보관·접근성 자동완성](cards.png)
- [로그: 범위·배치 결과/사유·행/토스트 되돌리기](bulk.png)
- [사전: 묶인 추천·무시·사용 수·연도](dictionary.png)
- [합치기 확인: 남는 이름·별칭·종류](merge-confirm.png)
