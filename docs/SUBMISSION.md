# OpenMainty — 제출 개요 (검증된 것만 말한다)

근거: `/Volumes/SSD/AI/Claude/openmainty-supervision/20261031-claude-root/Root-Claude-submission-evidence-index-v4-20261104T192153Z.json`
(sha256 `4fd97d73533f947387aa2bbd2d549356e22708f5f57ebd1370a128a45c7dc494`).
아래 숫자는 전부 이 목록이 가리키는 Root 독립 검증 기록에서만 가져왔다.
기록 원문은 비공개로 소유자 SSD에만 있고, 여기서는 파일 이름으로만 인용한다.

## 한 줄 소개 · 문제 · 해결

한 줄 소개: OpenMainty는 오픈소스 저장소의 의존성 드리프트와 열린
이슈를 지켜보고, 각 업데이트를 소유자 Mac의 샌드박스에서 직접 검증한
뒤, 검토 가능한 묶음과 PR 초안을 준비한다. 합치는 건 사람이다.
OpenMainty는 절대 푸시하지 않는다.

문제: 작은 오픈소스 저장소는 릴리스 사이에 썩는다. 의존성 업데이트가
검토 없이 쌓이고, 락파일이 매니페스트와 어긋나고, 네이티브 락은 지난달
팟을 고정한다. 범프 하나를 확인하려면 설치·테스트·빌드 한 사이클이
필요한데 그럴 시간이 없다.

해결: OpenMainty가 그 사이클을 대신 돈다. 지켜보기 → 검토 → 준비 →
빠른검사(샌드박스) → 묶음 → 사람 보류/승인 → 합치기 PR 초안. 근거를
기록하고, 사람이 필요한 곳에서는 정직하게 멈춘다. 전체 파이프라인은
[docs/HACKATHON.md](HACKATHON.md)에 있다.

## 지금 실제로 동작하는 것

- 마지막 코드 슬라이스(289b: 실행 기록에 상태 칩과 개수,
  저장소·최신만 필터, 30건씩 보기, 주소에 남는 필터, 읽을 수 있는
  단계 요약, 좁은 화면 카드; 칩 간격과 저장소 고르기 모양)는
  `72b08fffcba09891`로 승격·검증됨 (2026-11-04T19:19:18Z).
  (Root-Claude-promotion-289b-to-72b08fffcba09891-and-live-check-20261104T192107Z.json)
  앞선 승격(288a+288b, 문서만)은 `4ce6023a183d8766`으로 승격·검증됐다
  (2026-11-04T15:29:11Z).
  (Root-Claude-promotion-288ab-to-4ce6023a183d8766-and-live-check-20261104T153216Z.json)
  그 앞 코드 슬라이스(287c)는 `0215fe6129da8c84`로 승격·검증됐다
  (2026-11-04T13:19:11Z).
  (Root-Claude-promotion-287c-to-0215fe6129da8c84-and-live-check-20261104T132123Z.json)
  그 앞 코드 슬라이스(280b)는 `60dcdc324179c93f`로 승격·검증됐다
  (2026-11-03T17:33:15Z, 고정된 과거 사실).
  (Root-Claude-promotion-280b-to-60dcdc324179c93f-and-live-check-20261103T173459Z.json)
- Root가 독립 검증한 승격 29회(240 → 289b): 매번 Root 독립 수락
  선언서가 기준이고, 그중 27회(245c → 289b)는 Python 3.14·3.9 전체
  모듈 독립 실행 기록이 있으며, UI를 바꾼 슬라이스는 실제 브라우저를
  따로 검사했다. 285b+286, 288a+288b, 289+289b은 각각 한 번에
  함께 승격했고, 283·284·288a+288b는 문서만이었다. 검사에서
  실패한 기록도 지우지 않고 남겨 다음 슬라이스에서 고쳤다: 287
  검토 L31 — 오해 소지가 있는 '예정' 표현은 Root 자신의
  steering에서 나왔고 바로잡았다
  (Root-Claude-287-staged-review-L31-L32-release-and-counts-honesty-20261104T110610Z.json);
  L33 — UI 전용 묶음이 핀 하나를 놓쳐 Root 전체 스위트가 잡았고,
  이후 Muse는 전 모듈을 돈다
  (Root-Claude-287c-staged-review-PASS-20261104T122455Z.json).
  289에서는 Muse의 Python 3.9 실행에서 test_prs 1건이 임시 폴더
  정리 경합으로 ERROR가 났고, 기록은 남기고 재실행 3회와 Root
  실행은 OK였다
  (Root-Claude-289-289b-staged-review-PASS-sandbox-browser-20261104T190428Z.json).
  Root가 겉모습 문제 2건(칩 라벨·개수 붙음, 기본 모양 셀렉트)을
  찾아 289b에서 고쳤다(같은 검토 기록).
  Root 자신의 실수도 기록에 남긴다: 282 승격 1차 시도는 Root의 늦은
  승인으로 마감 전에 승격 없이 중단됐고 2차 시도에서 승격했다
  (Root-Claude-failure-282-promote-attempt1-deadline-missed-root-latency-20261103T194058Z.json).
  Root의 라이브 키보드 검사 중 실수로 실제 플랜 1회가 시작됐으나
  (플랜 문구만, 적용 없음) 기록만 하고 증거로 쓰지 않았다
  (Root-Claude-incident-live-keyboard-check-started-plan-chat-610994d7-20261103T200132Z.json).
  승격은 Muse가 `openmainty promote --manifest`로만 했다.
- 최근 독립 전체 스위트: 124개 모듈, Python 3.14와 3.9에서 각각
  3659개 테스트. `test_ncontain`만 비-OK이며, 이는 격리 환경 거부로
  설계상 제외다.
  (Root-Claude-gate4-allmodules-289-289b-py314-py39-PASS-20261104T1905Z.json)
- gate 3 통과: 승격된 앱 `72b08fffcba09891`이 확인된 경로에서
  통과했다. 전 기준 감사는 스테이징 286 트리에서 Root 8942
  샌드박스로 했다: 1280/390 × light/dark, 44px, AT 이름, 대비,
  넘침, 본문 스크롤, 마우스·키 목록 너비, GET→다시그리기,
  입력/캐럿, 오프라인 상태, 콘솔 0
  (Root-Claude-286-staged-review-PASS-gate3-sandbox-20261104T094813Z.json).
  스테이징 트리의 287c 매트릭스도 통과했다
  (Root-Claude-287c-staged-review-PASS-20261104T122455Z.json).
  289/289b 샌드박스 감사도 통과했다(독립 79건 시드)
  (Root-Claude-289-289b-staged-review-PASS-sandbox-browser-20261104T190428Z.json).
  승격 뒤에는 라이브 앱을 다시 확인했다(실제 Chrome #/runs
  실데이터 105건, 내장 390 카드)
  (Root-Claude-promotion-289b-to-72b08fffcba09891-and-live-check-20261104T192107Z.json).
  282 이후 새로 보이는 것: 한눈에 보기의 결정 중심부 '오늘 결정할
  일'과 저장소별 원장, 마우스·키로 조절하고 기억하는 목록 너비
  (280–560px), 분명한 단추 위계와 빈 화면, 정직한 요약 출시/수 행.
  287c 이후 새로 보이는 것: 실행 기록에서 필요한 일을 찾는 수단
  (개수 있는 필터, 30건씩 보기, 주소에 남는 필터, 읽을 수 있는
  단계 요약, 휴대폰 카드). 라이브 앱 390px에서 실행 기록
  페이지는 27,149px에서 5,782px로 줄었다.
- 추적 현황 (2026-11-04T19:21:21Z 기록, 개수 불변, 앱 목록 ==
  GitHub 목록): react-native-nitro-sound 열린 이슈 11건·열린 PR
  14건·토론 26건(열림 23건·답변됨 1건),
  flutter_calendar_carousel 열린 이슈 1건·PR 0건·토론 비활성.
  (Root-Claude-gate6-issue-pr-discussion-tracking-refresh-20261104T192121Z.json)
- 비공개 데모는 Root가 288a 트리에서 DEMO.md를 글자 그대로 따라
  격리 환경에서 재현했고, DEMO.md는 그 뒤로 바뀌지 않았다: 시드,
  수치(pushes 0), 할당량 게이트 403/403/200/200, §4 화면 10개
  전부(1280/390), 콘솔 오류 0.
  (Root-Claude-288a-staged-review-and-gate5-demo-replay-PASS-20261104T140920Z.json)

## 어떻게 만들었나 (검증 방식)

- Muse가 소스·테스트·문서를 쓰고 자신의 UI 검사를 한다.
- Root(Claude)는 쓰지 않고 감독만 한다: 코드 검토, 격리 홈에서 전체
  스위트 독립 재실행, 별도 실제 브라우저 검사, 독립 수락 선언서 작성.
- 승격은 `openmainty promote --manifest` 한 경로로만 한다. Muse가
  실행하고, Root 선언서가 기준이다.
- OpenMainty는 절대 푸시하지 않는다. 합치기·푸시·제출은 소유자 손이다.

## 데모 재현

합성 데이터만으로, 격리 홈에서, 8901도 실제 홈도 건드리지 않고 돈다.
[docs/DEMO.md](DEMO.md) 1–5단계를 글자 그대로 따르면 된다:
시드(`openmainty demo seed`), 로컬 서빙(`openmainty serve`),
기대 화면 확인, 읽기 전용 수치. 끝내면 디렉터리째 지운다.

## 남은 보류와 사람이 할 일

- gate 1 (열림): 실제 앱 사례(case856, Work run-42fd6a6a)가 끝까지
  돌지 않았다. A 격리 수정, B 새 웹 빌드 + JS 부팅, C 렌더 검토 +
  비공개 등록이 gate 2 프리플라이트 뒤에 막혀 있다.
- gate 2 (열림): 구독 프로필 바인딩은 제품 경로에서 증명됐으나(E24),
  설정 읽기에 `~/.config/muse` 안 쓰기 잠금이 필요해서 소유자 결정
  D2가 열려 있고, 워커 모델 id(D1)도 소유자 결정이다. 사용량·기여자
  바인딩은 아직 관측되지 않았다. API 키 과금은 쓰지 않고, 모델도
  바꾸지 않는다.
- gate 3 (통과): 승격된 앱이 확인된 경로에서 통과했다(위의 gate 3
  통과 기록). L25(실패 시 PR 가져오기)와 부팅 시 중복 요약 조회는
  285b에서 고쳐서 승격됐다(실패해도 오픈당 PR 1회, 방문당 요약
  기본 1회+전날 1회)
  (Root-Claude-285b-staged-review-PASS-not-promoted-20261104T040501Z.json).
  통과를 막지 않는 메모는 장식 수준(L8/L10)만 남았다.
- 사람이 할 일: D1 워커 모델 id, D2 쓰기 잠금 허가, 대회 규칙·마감
  확인, git 커밋/푸시와 실제 제출. 마지막 둘은 소유자 손이다
  (OpenMainty와 Root는 절대 푸시하지 않는다).

## 하지 않는 주장

- 우승 주장을 하지 않는다.
- 모든 GitHub 이슈·PR·토론을 해결했다는 말을 하지 않는다.
- 검증되지 않은 네이티브(CocoaPods/기기) 동작을 주장하지 않는다.
- 대회 규칙·마감은 소유자 확인 전까지 "미확인"이다.

## English summary

OpenMainty watches repos for drift and open issues, verifies updates in a
sandbox on the owner's Mac, and prepares reviewable batches and PR drafts;
it never pushes. Last code slice (289b: runs list with state chips and
counts, repo/latest filters, 30-at-a-time paging, filter kept in the
address, readable stage summary, phone cards): `72b08fffcba09891`,
verified 2026-11-04T19:19:18Z, after 29 Root-gated promotions
(240 → 289b); 27 carry all-modules runs on both Pythons, UI slices got
separate browser checks, and failed checks were kept and fixed in
follow-ups, and Root's own procedural misses were recorded too. Latest
suite: 124 modules, 3659 tests per Python (only fenced test_ncontain
non-OK); gate 3 passed on the promoted app on the checked routes, plus a
live re-check after promotion. Muse writes, Root independently
re-verifies, promotion is `openmainty promote --manifest` only. Open:
real-app end-to-end (gate 1), worker model id (D1), settings write-lock
(D2). No win/all-resolved/unverified-native claims; git push and
submission are owner actions.
