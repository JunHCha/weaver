# #154 통화 브리핑 Android spike — 머지 금지

이 브랜치는 **머지하지 않는 시험용**이다. `app/src/debug/`의 manifest와 `callbrief/` 패키지, `app/src/testDebug/`만 추가한다. 기존 접근성 자동 공유 서비스, MainActivity, 서버, Fabric, 릴리스 서명은 사용하거나 변경하지 않는다. 삭제할 때 이 파일과 두 source set을 지우면 된다.

## 설치와 진입

저장소 루트에서 빌드:

```sh
cd android
ANDROID_HOME="$HOME/Library/Android/sdk" ./gradlew assembleDebug lint testDebugUnitTest
```

APK 절대 경로:

```text
/Users/chajunhyeong/orca/workspaces/weaver/w154-spike/android/app/build/outputs/apk/debug/app-debug.apk
```

기기를 USB 연결하고 디버깅을 허용한 뒤 설치한다. `adb`가 PATH에 없으면 `$HOME/Library/Android/sdk/platform-tools/adb`를 쓴다.

```sh
adb install -r /Users/chajunhyeong/orca/workspaces/weaver/w154-spike/android/app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n io.github.junhcha.weaver.debug/io.github.junhcha.weaver.callbrief.CallBriefSetupActivity
```

런처의 **Weaver 통화 시험 · 머지 금지**에서도 진입할 수 있다. 기존 Weaver 디버그 앱과 동일 applicationId이므로 기존 디버그 설치를 갱신한다. 실제 사용 데이터가 없는 시험 기기를 사용한다.

## 구현과 예상

- 화면에서 명시적으로 시작하는 `specialUse` foreground service가 `PHONE_STATE` 방송을 받는다. 상시 알림에 중지 버튼이 있다. 자동 부팅, 전화 걸기/받기/끊기, 접근성 클릭은 없다. 프로세스가 종료되면 자동 재시작하지 않는다. 이미 통화 중일 때 감시를 켜면 다음 상태 변경부터 관측한다.
- RINGING, OFFHOOK(통화/발신), 실제 통화 이후 IDLE마다 300 × 64dp 이하의 오른쪽 위 띠를 표시한다. 3초 뒤 48 × 48dp로 접고 30초 후 제거한다. 길게 누르면 즉시 닫힌다. 띠 바깥 터치는 통화 화면으로 전달된다.
- 삼성 전화의 아래쪽 받기/끊기 영역과 떨어진 상단에만 창을 둔다. 카드도 화면 높이 1/4과 200dp 중 작은 크기로 제한한다. **OEM·회전·접힌 폰·큰 글자에서 버튼을 가리지 않는다는 보장은 아직 기기 확인 전이다.** 겹치면 시험을 중단하고 배치를 다시 정해야 한다.
- 띠 터치는 `setLaunchBounds(Rect)`와 `NEW_TASK | MULTIPLE_TASK`를 기본으로 사용한다. 별도 task affinity와 `resizeableActivity=true`로 기존 앱 태스크가 통화 앞에 전체 화면으로 재사용되는 것을 줄인다. 체크박스를 끄면 NEW_TASK만 쓴다.
- **성공 가능성이 더 높은 변형은 NEW_TASK + MULTIPLE_TASK**다. 새 태스크에 경계를 적용하므로 이전 전체 화면 태스크의 재사용을 피한다. 다만 표준 API는 경계 요청일 뿐이고 freeform을 강제로 켜지 않는다. 기본 One UI에서 freeform feature 또는 해당 디스플레이 지원이 없으면 두 변형 모두 무시될 수 있으므로, 검증 전 기본 예상은 떠 있는 카드 대안이다. DeX/freeform이 활성인 환경에서 첫 변형의 성공 가능성이 높다. 숨겨진 API나 Samsung 전용 호출은 사용하지 않는다.
- Activity가 350ms 후에도 multi-window가 아니면 닫고 `TYPE_APPLICATION_OVERLAY` 카드로 전환한다. 시작 예외 또는 1.2초 내 Activity 생성이 없는 조용한 background launch 거절도 카드로 바꾼다. 잠깐 전체 화면 전환이 보일 수 있으므로 이 역시 시험 항목이다. `isInMultiWindowMode`는 분할 화면에서도 true이므로 실제 팝업인지는 사람이 확인해야 한다.
- 고정된 **솔빛 공방** 가상 브리핑만 표시한다. 전화번호로 `ContactsContract.PhoneLookup.DISPLAY_NAME`을 읽어 해석 성공 여부만 보여주고 이름 자체는 버린다. 실명·전화번호를 UI, 로그, 파일, 네트워크에 넣지 않는다. 연락처를 만들거나 통화 기록을 수정하지 않는다.
- READ_CALL_LOG 권한이 있어야 시스템 방송 번호를 받을 수 있다. 중복된 번호 있음/없음 방송을 합치고 이전 발신자 번호를 새 통화에 재사용하지 않는다. 발신 번호를 OS가 제공하지 않으면 `번호 미제공`으로 계속하며 과거 통화 기록에서 추측하지 않는다.
- 잠금 또는 화면 꺼짐에는 겹침 대신 가상 내용의 일반 알림을 사용한다. 통화 후 띠가 남은 상태에서 잠가도 제거한다. 알림 탭 후 잠금 해제 시 Activity를 열고, 전체 화면이면 같은 카드 대안을 쓴다. 알림 권한/채널/잠금 알림 표시가 꺼져 있으면 OS가 알림을 숨길 수 있다.

API 근거: [ActivityOptions.setLaunchBounds](https://developer.android.com/reference/android/app/ActivityOptions#setLaunchBounds(android.graphics.Rect)), [TelephonyManager.ACTION_PHONE_STATE_CHANGED](https://developer.android.com/reference/android/telephony/TelephonyManager#ACTION_PHONE_STATE_CHANGED).

## Galaxy / One UI 기기 시험 체크리스트

각 항목에 기종·Android·One UI 버전, 화면 방향·글자 크기, 성공/실패, 실제 창 모드(팝업/분할/전체/카드), 통화 화면이 뒤에 남는지를 기록한다. 연락 상대는 동의한 시험 담당자와 가상 이름으로 만든 시험용 연락처를 쓴다. 실제 사용자 이름이나 내용이 담긴 화면을 캡처하지 않는다.

1. **권한 온보딩**: 새 설치에서 위 진입점을 연다. 겹침 권한 없이 시작 시 안내가 나오는지 확인한다. 겹침 설정에서 거절→복귀→허용을 시험한다. 전화 상태·통화 기록·연락처·알림을 각각 거절/허용하고 상태가 복귀 후 갱신되는지 확인한다. 영구 거절은 앱 설정 버튼으로 복구한다. READ_CALL_LOG가 installer/OS에 의해 제한되면 그 사실을 기록하고 번호 미제공 경로를 확인한다. 기본 전화 앱 역할은 바꾸지 않는다.
2. **감시 시작/백그라운드**: 시작 버튼을 누른 뒤 홈으로 나간다. 감시 알림이 유지되는지 확인한다. 다른 접근성/자동 공유 서비스는 활성화할 필요가 없어야 한다.
3. **수신 중**: 잠금 해제 상태에서 시험 전화를 받는다. 삼성 전화 화면 위쪽에 띠가 보이고 받기/거절 버튼을 누를 수 있는지 확인한다. 3초 뒤 작은 W로 접히는지, 띠 바깥 터치가 통화 화면에 전달되는지, 길게 누르면 사라지는지 확인한다.
4. **연락처 해석**: 가상 이름으로 저장한 시험 번호로 다시 전화한다. `연락처 이름 해석 성공`을 확인한다. 미저장 번호는 `연락처 없음`, 연락처 거절은 `연락처 권한 없음`, 발신자 번호 비공개/통화 기록 거절은 `번호 미제공`이 되는지 확인한다. 실명이 표시되지 않아야 한다. 중복 방송 때문에 띠가 반복해서 펼쳐지지 않아야 한다.
5. **통화 중**: 전화를 받아 OFFHOOK 띠를 확인한다. 음소거/스피커/끊기 버튼이 작동하고 가려지지 않아야 한다. 화면 위·아래로 이동하는 삼성 인콜 UI, 큰 글자, 세로·가로 방향을 각각 확인한다. 하나라도 겹치면 실패로 기록한다.
6. **발신**: 통화를 끝내고 시험 번호로 직접 발신한다. OFFHOOK 띠를 확인한다. OS가 발신 번호를 제공하지 않으면 `번호 미제공`이어야 하며 직전 수신 상대를 사용하면 안 된다. 다시 통화 후 종료한다.
7. **팝업 변형 A**: 기본 체크 상태에서 수신/통화 중 띠나 W를 누른다. 별도 작은 팝업 창인지 눈으로 확인하고 뒤쪽 통화 화면과 끊기 버튼을 계속 쓸 수 있는지 확인한다. 브리핑은 가상 자료만 있고 수정 기능이 없어야 한다. 닫으면 원래 통화 화면으로 돌아가야 한다.
8. **팝업 변형 B**: 설정에서 MULTIPLE_TASK 체크를 끄고 7번을 반복한다. 첫 실행과 두 번째 실행을 모두 시험한다. A/B 각각 요청 경계와 실제 동작을 기록한다. 기본 One UI와 가능하다면 DeX/freeform 활성 환경을 나누어 시험한다. split-screen을 팝업 성공으로 세지 않는다.
9. **카드 대안**: 전체 화면으로 열린 경우 약 350ms 뒤 자동으로 닫히고 위쪽 카드가 남는지 확인한다. 지원되는 팝업에서는 `떠 있는 카드로 전환` 버튼으로도 시험한다. 카드 내부 스크롤, 닫기 버튼, 창 밖 받기/끊기 조작, 30초 후 제거를 확인한다. 팝업이 background 제한으로 아예 열리지 않을 때도 약 1.2초 후 카드가 보여야 한다.
10. **통화 종료**: 받은 통화 종료, 발신 종료, 수신 거절/부재중에 각각 종료 띠가 한 번 보이는지 확인한다. 3초 후 접힘/30초 후 제거를 확인한다. 감시 시작 직후 아무 통화 없이 종료 띠가 나타나면 실패다.
11. **잠금 화면**: 감시를 시작하고 전원 버튼으로 잠근 뒤 수신·응답·종료를 각각 시험한다. 띠/카드 대신 일반 알림만 있어야 한다. 알림을 누르고 잠금을 해제해 가상 브리핑을 확인한다. 잠금 해제 상태의 띠·카드를 표시한 다음 잠가도 즉시 제거되고 알림으로 바뀌어야 한다. 화면 꺼짐 직후 방송이 도착하는 경우도 반복한다.
12. **권한 철회/중지**: 감시 도중 겹침·연락처·알림 권한을 철회해 크래시가 없는지 확인한다. 알림 거절 시 잠금 알림이 안 보이는 것은 OS 제한으로 별도 기록한다. 설정 또는 알림의 중지 버튼을 눌러 감시 알림·띠·카드가 제거되고 다음 전화에서 다시 나타나지 않는지 확인한다. 프로세스 종료 후는 설정에서 다시 시작한다.
13. **격리**: 기존 Weaver 앱의 공유·접근성 기능이 같은 동작인지 확인한다. 릴리스 source set에는 이 Activity/서비스/전화·겹침 권한이 추가되지 않았는지 manifest를 확인한다. 서버 연결 없이 모든 가상 브리핑이 동작해야 한다.

## 로컬 검증 결과

2026-11-17, JDK 21, SDK 36: `assembleDebug lint testDebugUnitTest` 성공. 366 tests, 0 failures/errors/skipped (새 상태 전이 회귀 테스트 2개 포함). `BUILD SUCCESSFUL in 27s`, `60 actionable tasks: 16 executed, 44 up-to-date` (최종 재검증).

`adb devices`에 연결 기기가 없었으므로 실제 Galaxy/One UI, 전화 이벤트, 팝업, 잠금, 권한 UI는 **미검증**이다. 이 PR은 기기 결과를 모으기 위한 것이며 제품 구현이나 머지 승인이 아니다.
