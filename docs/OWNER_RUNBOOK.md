# 매일 쓰기 (소유자 실행서)

실제 앱(8901 포트·`~/.openmainty`)을 쓰는 소유자용이다. 합성 데모는
[DEMO.md](DEMO.md)를 본다. 아래 명령은 모두 설치된 앱에서 실행한다.

## 1. 한눈에 보기

- 브라우저에서 `http://127.0.0.1:8901/#/glance`를 연다.
- 저장소 카드·오늘 결정할 일·접힌 전체 현황이 한 화면에 있다.
- 사람이 할 일(게이트 승인·보류 사유·함께 다시 시도)은 여기서 시작한다.

## 2. 사람이 직접 누르는 것 (게이트·보류·판정)

워커·드라이버는 절대 대신하지 못한다. `--as`는 owner 또는 root만 된다.

```sh
# 묶음 보류 (사유 필수) / 보류 풀기
openmainty hold --batch <묶음ID> --as owner --reason "<한 줄 사유>"
openmainty hold --batch <묶음ID> --as owner --release

# 게이트 승인: 영수증 검토·직접 확인·해당없음 중 하나
openmainty attest --batch <묶음ID> --gate <게이트> --as owner \
  --note "<확인한 내용 한 줄>" --receipt <영수증sha>
openmainty attest --batch <묶음ID> --gate <게이트> --as owner \
  --note "<확인한 내용 한 줄>" --log-file <로그파일>
openmainty attest --batch <묶음ID> --gate <게이트> --as owner \
  --note "<확인한 내용 한 줄>" --na "<해당없는 이유>"
```

검토 판정(승인·메모와 함께 승인·수정 요청)을 기록해야 자동 묶음이
READY가 된다. 판정이 없으면 절대 READY가 되지 않는다.

## 3. 자율 스위치 (스케줄러)

```sh
openmainty sched status                 # 현재 상태 읽기
openmainty sched enable --repo <저장소>  # 저장소 스케줄 켜기
openmainty sched disable --repo <저장소> # 저장소 스케줄 끄기
```

그 밖의 하위 명령(`scan`, `tick`, `auto`, `run-due`, `cancel`,
`set-watch`, `reconcile`, `repair-bindings`)은 `openmainty sched --help`를
보고 쓴다. 데몬은 대시보드·CLI가 `/api/sched/daemon`을 명시 호출할 때만
돈다.

## 4. 수치 다시 계산 (읽기 전용)

```sh
openmainty metrics --days 7   # 최근 7일 흐름 (한국어 기본, --lang en 가능)
openmainty metrics --json     # 원시 수치
```

저장소별 자율 시작·준비·검사·묶음·보류와 pushes(항상 0)를 홈에서 직접
다시 계산해서 보여준다.

## 5. 되돌리기 (승격 롤백)

```sh
openmainty release --dry-run                 # 계획만 출력, 변경 없음
openmainty release --rollback --yes          # 바로 이전 sha로 되돌리기
openmainty release --rollback --to <sha> --yes  # 지정 sha로 되돌리기
```

`--yes`는 대시보드 재시작 확인이다. 롤백은 `current` 연결만 이전 승격
사본으로 옮긴다.

## 6. 디스크 정리 (읽기 먼저, 지우기는 명시)

```sh
openmainty gc --dry-run                       # 보고서만, 아무것도 안 지움
openmainty gc --apply --category <분류> --confirm-bytes <바이트>
```

분류는 `node_modules|cache|old_app|tmp_scratch|stale_tmp` 중 하나이며,
`--confirm-bytes`는 방금 본 그 분류 합계와 정확히 같아야 한다.
`stale_worktree`는 보고서 전용이라 지우지 않는다.

## 7. 하지 않는 것

- 푸시·머지·배포·댓글은 없다. PR 초안은 텍스트일 뿐이며 `git push`와
  `gh pr create`는 소유자가 직접 손으로 한다.
- `pod install`·기기 테스트·스토어 제출은 OpenMainty 안에서 절대 안 한다.
- 공유 머신에서는 attestor 이름이 다르면 주장일 뿐이다. 대시보드는
  loopback(127.0.0.1)에 둔다.
