# 오프라인 비공개 데모 (합성 데이터만)

네트워크·모델 CLI·git 리모트·실제 사용자 데이터가 필요 없다.
모든 것은 아래에서 만드는 디렉터리 하나 안에 산다.
**8901·실제 홈을 열지 않는다**: 실제 앱(8901 포트)과 `~/.openmainty`는
절대 건드리지 않는다.

## 1. 빈 디렉터리에 심기

```sh
CAND=/path/to/openmainty   # openmainty 체크아웃 (루트에 openmainty 실행 파일이 있는 폴더)
D=$(mktemp -d /tmp/om-demo.XXXXXX); mkdir -p "$D/home" "$D/seed"
HOME=$D/home OPENMAINTY_HOME=$D/home TMPDIR=$D \
  "$CAND/openmainty" demo seed --home "$D/seed"
```

`demo seed`는 디렉터리가 존재하고 비어 있지 않으면 거부하고, 그 안에만
쓴다(호출 동안 `OPENMAINTY_HOME`을 대상에 두었다가 되돌린다).
`demo home: …` 한 줄과 아래 목록을 출력한다. 만든다:

- 저장소 `demo-sample-repo`(github 라벨 `example-org/sample-repo`, 합성.
  체크아웃을 연결하지 않으므로 git 프로브가 절대 안 돈다);
- 저장소 `demo-empty-repo`(실행 없음: 빈 화면들);
- 실행 `run-demowait01`: 질문 "데모 위젯의 빈 화면 문구를 확정해 주세요."에
  막힘 — 분석 글에 GFM 파이프 표, 표를 담은 펜스 코드 블록,
  `[file](workfile:…)` 연결이 있다;
- 실행 `run-demodone01`: 끝남. 보관된 `fix.diff.txt`와 파싱된
  `fix.diffstat.txt`(과거 소스 변경 1파일: 모델 주장과 보관 근거가 일치);
- 실행 `run-demomiss01`: 살아 있는 작업 폴더 없음 (사용 불가);
- 실행 `run-demostop01`: 트리아지 끝, 나머지 대기, 실행 중 없음 —
  "멈춤 — 진행 중인 작업 없음"이라 읽힌다.

저장소 이름에 `/`가 없는 이유: 저장소 이름이 `<이름>.json` 파일이 된다
(`repos.validate_name` 문자 규칙).

## 2. 로컬로 서빙하기 (뒤에서 도는 것 없음)

```sh
PORT=8942
HOME=$D/home OPENMAINTY_HOME=$D/seed TMPDIR=$D \
  "$CAND/openmainty" serve --port $PORT
```

그 뒤 `http://127.0.0.1:$PORT/`를 연다(바인드는 127.0.0.1; 8942라 실제
8901 앱을 건드리지 않는다).

`serve`가 시작하는 것: HTTP 서버, 그리고 저장된 파일에서 한눈/묶음
스냅샷을 다시 계산하고 끝나는 단일-flight 스냅샷 재생성 스레드 최대
하나(모델 호출 없음). `serve`는 스케줄러·워처·cron을 시작하지 않는다.
유지보수 launchd 서비스는 별도 설치이며 `serve`는 읽기 전용으로만
들여다본다(`launchctl list`). 스케줄러 데몬은 대시보드·CLI가
`/api/sched/daemon`을 명시 호출할 때만 돈다. 끌 플래그가 없는 이유:
꺼야 할 것이 없기 때문이다. 데모는 Ctrl-C로 끝낸다.

## 3. 할당량 엔드포인트는 설계상 거부

`PATH`에 `muse` 바이너리가 없으면, 할당량을 쓸 수 있는 모든 엔드포인트
(채팅, 계획 승인, 작업 maintain/run/resume, integration, 검토 POST)는
403(`chat.subscription_error()` 게이트)으로 답한다. 확인하려면:

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin HOME=$D/home TMPDIR=$D \
  OPENMAINTY_HOME=$D/seed "$CAND/openmainty" serve --port 8932
```

대기 중 실행의 질문에 답하기(`POST /api/job/answer`)도 403으로 답한다 —
데모는 워커를 절대 시작하지 않는다. 읽기 전용 GET
(`/api/overview`, `/api/work?run=run-demowait01`, …)은 정상 동작한다.

## 4. 기대 화면

- 한눈에 보기(`#/` 또는 `#/glance`): "오늘 결정할 일"이 먼저 — 다음
  판단 1건(질문 #7)과 "판단 열기" 단추, 그 아래 "읽기 전용 판단" 1건
  저장소별 목록. 데스크는 저장소 필터와 목록/상세 나누기("목록 너비
  조절"로 마우스·키로 조절)이며, 비어 있는 상세에는 "목록에서 판단을
  선택하세요."가 보인다. 상태 카드는 새 데모 홈에서 "지금 상태를 확인할
  수 없어요"(미확인)가 정상이고, PATH에 muse가 없으면 "Muse 로그인이
  필요합니다"도 나온다.
- 관제센터(`#/command`): 위에 관제 현황 이름표 줄(서비스·모드·마지막
  스캔·다음 스캔·워커), 아래에 저장소 카드 2개(스캔 전·자동 실행 끔,
  "지금 스캔"이 기본 동작). "답변이 필요한 일" 2건(#7 답변 상자와
  "보내기", #9 "확인 필요"). "최근 사건"은 "아직 사건 없음 — 스캔 후에
  기록됩니다."라서 항목 목록("의존성에서 보기" 연결)은 데모 고정구에서
  보이지 않는다.
- 작업 대기(`#/work/run-demowait01`): 막힌 질문, 분석 보고서가 진짜 표로
  렌더(펜스 안은 코드 유지), workfile 단추들.
- 작업 완료(`#/work/run-demodone01`): 보관된 패치의 "과거 기록" 소스 변경.
- 작업 폴더없음(`#/work/run-demomiss01`): "작업 폴더가 사라져 소스를
  읽을 수 없음" (막힌 단계, 확인 필요) — fail-closed, 깨끗한 척 안 한다.
- 작업 멈춤(`#/work/run-demostop01`): "멈춤 — 진행 중인 작업 없음".
- 빈 저장소(`#/repo/demo-empty-repo?tab=work`): 작업 탭에
  "체크아웃 미연결" 제목과 "연결된 작업 폴더가 없어 진단하지
  않았습니다." 문장이 보이고, 다음 행동은 저장소 연결 후 머리글의
  다시 진단이다.
- 커뮤니티(`#/repo/demo-sample-repo?tab=community`): 토론 분류 필터 3개가
  44px. 시드된 토론이 없으니 목록은 비어 있다.
- 관계 그래프(`#/graph`): 실행·이슈에서 그린 작은 그래프와 검색창.
  이 작은 그래프는 렌더 확인용이다.
- 하루 요약(`#/digest`): 제목(h1) "하루 요약" 아래에 "아직 <오늘 날짜>
  요약이 없습니다"와 스케줄러 설명, "한눈에 보기로" 연결. 데모 홈은
  스케줄러가 한 번도 안 돌아 전날 연결도 없다.

## 5. 수치 다시 계산 (읽기 전용)

```sh
HOME=$D/home OPENMAINTY_HOME=$D/seed TMPDIR=$D \
  "$CAND/openmainty" metrics --days 7
```

홈에서 직접 다시 계산한다. 데모 홈에서는 대부분 0이며, pushes는 항상
0이다.

## 6. 알려진 한계

- 하루 요약은 스케줄러가 돌 때만 생긴다. 데모 홈에서는 "아직 요약 없음"이
  정상이다.
- 커뮤니티 목록·PR 목록은 시드하지 않는다. 필터와 빈 화면만 보인다.
- 데모 그래프는 작다. 수백 노드 렌더는 이 홈에서 확인하지 않는다.
- 체크아웃을 연결하지 않은 저장소는 진단이 형식 응답(`no_checkout`)으로
  끝나 작업 탭에 "체크아웃 미연결"이라 표시된다. 콘솔 오류는 없다.

## 7. 정리

```sh
# 서버를 Ctrl-C로 끝낸 뒤, 디렉터리째 지운다.
# rm -rf "$D"
```
