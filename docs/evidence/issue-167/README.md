# #167 태그 편집 검증

2026-11-19, `origin/main`에서 만든 `feat/web-tags-167`에서 검증했다.
Node 22.22.0, pnpm 10.4.0, Python 3.14.5, uv 0.9.2를 사용했다.

## 필수 검사

- `uv sync --project server --locked`: 성공, lock 변경 없음.
- `uv run --project server python -c 'import weaver'`: 성공.
- `uv run --project server python -m weaver.app --help`: 성공.
- `uv run --project server pytest`: **1637 passed, 1 skipped, 1 warning in 120.36s**.
- `uv run --project server ruff check server`: **All checks passed!**
- `uv run --project server ruff format --check server`: **173 files already formatted**.
- `uv run --project server mypy server/src`: **Success: no issues found in 85 source files**.
- `pnpm --dir web install --frozen-lockfile`: 성공.
- `pnpm --dir web tokens:check`, `lint`, `typecheck`: 모두 성공.
- `pnpm --dir web test`: **Test Files 9 passed (9), Tests 69 passed (69)**.
- `pnpm --dir web build`: **Compiled successfully, Generating static pages (7/7), Exporting (2/2)**.
- `git diff --check`: 성공.

서버의 경고는 기존 Starlette/httpx 사용 중단 예고다. Android는 수정하지 않았다.

## 완료 조건별 웹 테스트

`web/src/components/TagWorkflow.test.tsx`:

| 완료 조건 | 테스트 이름 |
| --- | --- |
| 카드 단건 편집·별칭 자동완성·종류 색 | `edits colored card chips directly and adds a dictionary alias without opening detail` |
| 로그 단건·일괄 추가·일괄 삭제 | `edits log row chips and bulk adds/removes one correction per selected Thread` |
| 새 항목 생성 | `creates an unknown entry inline only after choosing its kind` |
| 합치기 제안·한 번에 합치기/별칭 | `suggests spacing, close typos and alias overlaps and merges or retains the source as alias in one click` |
| 5초 되돌리기·필터에서 사라진 카드 | `undoes correction IDs after the edited card disappears and expires after five seconds` |
| 일부 일괄 저장 실패 | `keeps successful bulk writes undoable when another write fails` |
| 일괄 되돌리기·실패한 철회 재시도 | `undoes every bulk correction and retries only failed retractions` |
| 항목별 사용 수·최근 사용일 | `shows dictionary usage counts and recent usage dates including unused entries` |

기존 `Editing.test.tsx`의 낙관적 삭제 실패 복구, 새 항목 종류 필수 검사와
`FabricPerformance.test.tsx`의 3,000개 Thread 회귀도 함께 통과했다.

## 서버 API 회귀

`server/tests/web/test_editing.py` 전체 **60 passed**:

- `test_undo_tag_correction_appends_retraction_and_preserves_later_edits`: 정정 철회 Thread 추가, 중복 요청, 나중 정정 보존, 토큰 누락 및 외부 Origin 거부.
- `test_undo_rejects_non_corrections`: 미존재 404 및 일반 Thread 422.
- `test_dictionary_last_used_tracks_effective_active_threads`: 별칭 정규화, 철회된 Thread 제외, 실효 태그 삭제 후 최근 사용일 갱신.
- 기존 태그 생성·정정·사전 합치기 및 loopback/Origin/CSRF 경계 테스트 유지.

## 합성 서버 화면 확인

`/tmp/weaver-167-synthetic/config.toml`에 모든 경로를 임시 디렉터리로 지정하고
`[llm] backend = "none"`으로 실행했다. 직접 만든 합성 Thread 3개와 사전만
사용했으며 실제 LLM·음성 모델·사용자 vault·기기 시험 폴더에 접근하지 않았다.
Orca 내장 브라우저로 실제 서버의 태그 생성, 3개 선택 후 일괄 추가,
두 정정의 되돌리기, 사전 제안의 별칭으로 합치기까지 확인했다.
이미 같은 태그가 있는 Thread는 기존 API의 no-op 규칙에 따라 정정이 생기지 않는다.

- [카드 칩·새 항목 종류 선택](cards.png)
- [로그 일괄 편집·5초 되돌리기](bulk.png)
- [사전 합치기 제안·사용 수·최근 사용일](dictionary.png)

기존 사전 항목을 다른 항목의 별칭으로 넣을 때는 이름 충돌을 피하고 기존 참조를
보존하기 위해 같은 원자적 merge API를 사용한다. 대상 종류를 유지하고 원래 이름과
별칭을 모두 대상의 별칭으로 남긴다는 안내를 두 버튼 옆에 표시한다.
