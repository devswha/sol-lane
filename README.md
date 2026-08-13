# sol-lane

GPT-5.6 Sol Pro는 웹 구독 전용이고 API가 없다. 그래서 리뷰를 받으려면 코드를 골라
패킹하고, 로그인된 브라우저를 CDP로 몰아 투입하고, 모델을 검증하고, 응답을 회수해야
한다. 그 절차가 프로젝트마다 셸 스크립트로 복사되던 것을 하나의 CLI로 모은 것이다.

## 설치

```bash
uv sync --extra dev
uv run lane engine sync      # 핀된 업스트림 엔진 + vendor/patches 적용
uv run lane doctor
```

## 사용

```bash
lane drive ea "T3 2AFC 집계 대안 적용"    # 계획→구현→게이트 루프
lane review ea "persona_brain과 persistent_memory 결합에 경합 없나"
lane review ea "<질문>" --include "src/ea/persona_brain.py,tests/test_persona_brain.py"
lane review ea "<질문>" --paste     # CDP 없이 codexpro 번들 + 클립보드
lane review ea "<질문>" --dry-run   # 실행할 커맨드만 출력
lane harvest ea                    # 이미 값을 치른 대화에서 답만 회수 (전송 없음)
lane salvage ea                    # 중단된 대화의 추론이라도 건진다 (미검증)
lane projects
lane doctor
```

종료코드는 스크립트에 물릴 수 있게 고정돼 있다.

| 코드 | 의미 |
|---|---|
| 0 | 성공 — 검증된 응답을 회수해 저장 |
| 1 | 전달 실패 — CDP 없음, fail-closed 중단, 빈 응답 |
| 2 | 설정/엔진 문제 — 알 수 없는 프로젝트, 위험한 루트, 엔진 미벤더링 |

## `lane drive` — Pro가 계획, gjc가 구현, 게이트가 판정

```bash
lane drive ea "<하고 싶은 일>" [--max-iters 2] [--session <sdk-session-id>]
```

```
① Pro에게 계획 1회 요청 (레포 패킹해서)      ← Pro 2분·메시지 1통
② 계획 → .ai-bridge/current-plan.md
③ gjc가 자기 도구로 구현 (lane 전용 세션 디렉토리, --continue)
④ 프로젝트 gate 실행 → 종료코드가 판정
⑤ PASS면 끝, FAIL이면 게이트 출력을 들고 ①로 (최대 max_iters회)
```

설계 원칙 하나: **Pro는 구현 루프 안에 들어가지 않는다.** 판단은 비싸고 느리며
(턴당 2분·구독 메시지 1통), 검증은 싸고 빠르다. 그래서 게이트가 심사관이고 Pro는
제안자다. `max_iters`가 한 작업이 쓸 수 있는 Pro 메시지 수의 상한이다.

구현은 **lane 전용 세션 디렉토리**(`.ai-bridge/lane-session`)에서 돈다. 네가 쓰고 있는
 live 세션에 프롬프트가 주입될 일이 없다 — 그러려면 `--session <id>`로 명시해야 한다.

### 게이트를 고쳐서 게이트를 통과할 수는 없다

구현자와 게이트는 같은 워크트리를 공유하므로, "게이트를 통과시켜라"는 실패 테스트
삭제·`addopts = --ignore=tests`·게이트 스크립트 `exit 0`으로도 만족된다. 프롬프트의
금지 문구로는 막을 수 없어서, 드라이브 시작 시점에 **검증 파일들의 해시를 얼린다.**

- 대상: `gate_protected` 글롭(기본 `tests/**/*`, `**/conftest.py`, `pyproject.toml`,
  `pytest.ini`, `setup.cfg`, `tox.ini`, `noxfile.py`, `Makefile`) + 게이트 커맨드가
  실제로 가리키는 레포 내 파일(`./scripts/gate.sh` 같은 것).
- 구현 후 하나라도 수정·삭제됐으면 **게이트를 실행하지 않고** 실패한다. 이미 초록인
  게이트를 물려받아 성공으로 보고할 여지를 남기지 않는다.
- 파일 추가는 허용된다. 빨간 게이트는 테스트를 더 넣어도 초록이 되지 않는다.

게이트 출력은 도착하는 대로 tail(`GATE_LOG_LIMIT`)만 남긴다. 수 GB를 찍는 테스트
스위트가 lane을 OOM으로 끌고 가지 못한다.

## Sol Pro 모드 — gjc 세션을 Pro로 돌리기

```bash
lane serve                    # 127.0.0.1:8799 에 OpenAI 호환 엔드포인트
SOL_PRO_LOCAL_KEY=local gjc --mpreset sol-pro
```

`~/.gjc/agent/models.yml`에 provider와 profile을 한 번 등록해두면 된다.

```yaml
providers:
  sol-pro-local:
    baseUrl: http://127.0.0.1:8799/v1
    apiKeyEnv: SOL_PRO_LOCAL_KEY
    api: openai-completions        # openai-chat 등 다른 철자는 조용히 무시된다
    auth: apiKey
    models:
      - id: sol-pro
profiles:
  sol-pro:
    required_providers: [sol-pro-local]
    model_mapping:
      default: sol-pro-local/sol-pro
```

자세한 계약과 제약은 [docs/sol-pro-mode.md](docs/sol-pro-mode.md)에 있다.

## 긴 판은 죽는다 — 죽어도 값은 치렀다

실측 3연속: 93 KB 판은 18분+에 마감으로 포기, 290 KB 판은 40분 20초 뒤 수동 salvage,
164 KB 판은 **31분 33초 생성 후 중단**됐다. 마지막 판의 증상 문자열은 대화에 남는
`생각 중단됨`이고, assistant 메시지 노드는 0개다. 그 뒤 구엔진은 죽은 대화를 19분 더
폴링했다.

그래서 회수를 분리했다. 엔진은 전송 직후 결속된 대화 URL을
`.insane-review/manifest_<label>_<tag>.json`에 적고, 실패한 `lane review`는 그 URL과
회수 커맨드를 알려준다.

```
lane: review did not produce a verified response (fail-closed)
chat       https://chatgpt.com/c/…
retry      lane harvest lane   # no new message is sent
```

`lane harvest`는 패킹도 프롬프트도 전송도 하지 않는다. 대화에서 답만 긁어온다.

엔진 v0.6.1은 `max_wait` 소진 지점에서 마지막 수단으로 '지금 답변 받기'를 누르고
유예 대기한다 — 마감이 답을 버리는 사례는 그걸로 닫혔다. 반대로 **턴이 스스로
중단된 경우는 여전히 미해결**이다: `is_streaming`이 False라 강제 클릭 경로를 타지
않고, 새 message-id도 생기지 않으니 `max_wait`까지 폴링한다. "스트림 정지 + 신규
메시지 없음이 N분 지속되면 중단으로 판정"은 지금 넣지 않았다 — 보유한 사례가
1건(29분 정지)이고, Pro가 정상 추론 중에 오랫동안 무변화로 보이는 표본은 없다.
임계값을 추측으로 잡으면 이 레포가 피하려는 실패(확신에 찬 미완성 답)를 직접 만들게
된다. 표본이 더 생기면 결정한다.

### 긴 판이 죽는 지점 (실측 7판)

| 팩 | 질문 | 추론 | 결과 |
|---|---|---|---|
| **15 KB (2파일)** | **1개, "5분 분량"** | **7분 47초** | **완주 — 무인 회수 성공** |
| 70 KB (7파일) | 4영역 전수 | 27분 12초 | 스스로 중단, assistant 노드 0개 |
| 78 KB (7파일) | 4영역 전수 | 37분 00초 | 스스로 중단, assistant 노드 0개 |
| 93 KB | 전수 | 18분+ | 옛 20분 마감에 잘려 유실 |
| 164 KB (22파일) | 4영역 전수 | 31분 33초 | 스스로 중단, assistant 노드 0개 |
| 290 KB | 전수 | 40분 20초 | 엔진 회수 실패, 수동 salvage |

처음엔 크기 문제로 보였다. 아니다 — 78 KB 판이 164 KB 판보다 6분 더 추론하고 죽었다.
**시간을 정하는 건 팩 크기가 아니라 요구의 크기다.** 4영역 전수조사를 시키면 팩을 반으로
줄여도 30분을 넘기고, 그 구간에서 4판 연속 죽었다. 질문을 하나로 줄이자 7분 47초에
완주했다.

그래서 자기감사의 단위는 "레포 한 판"이 아니라 **"보장 하나"** 다.

**시간을 강제로 묶는 수단은 없다.** 엔진 v0.5.8+는 `force_answer_after` 지점에서
'지금 답변 받기'를 클릭하지만, 2026-08-13 실측으로 그 어포던스가 UI에 없다:

```
div[data-testid="cot-v5-pinned-row"]              → 0개
/답변\s*받기|Get answer|answer now/ 텍스트 매칭    → 0개
data-testid 중 cot|pinned|thinking|reason         → 없음
```

스트리밍이 살아 있는 21분 지점에 조회한 값이고, 엔진도 같은 판에서
`⚠️ 1329s — '지금 답변 받기' 버튼 6회 실패 → 자연완료 대기`를 찍었다. 업스트림에
보고했다(fivetaku/insane-review#6). `force_answer_after = 1200`은 현재 무효이며,
UI가 버튼을 되돌리면 다시 동작하도록 값만 남겨둔다.

죽어도 건지는 경로는 세 겹이다.

1. **결속 URL이 전송 직후 manifest에 남는다** (`0003` 패치). 대기 중 프로세스가 죽어도
   회수 대상이 사라지지 않는다.
2. **`lane harvest`** — 메시지 없이 같은 대화에서 재회수. 검증된 답만 저장한다.
3. **`lane salvage`** — 답이 없고 추론만 남았으면 그 텍스트를 가져온다. 파일은 항상
   `salvaged_*.md`이고 헤더에 "UNVERIFIED"가 박힌다. 2026-08-13에 이 경로로 건진
   추론 패널에서 실제 결함 2건(신규 conftest 우회, 게이트 실행 중 파일 교체)이 나왔다.

## 설정 (`lane.toml`)

```toml
[projects.ea]
root = "~/workspace/ea-sol-wt"
include = ["README.md", "src/ea/**/*.py", "tests/**/*.py"]
gate = "uv run pytest -q"          # lane drive의 판정자
force_answer_after = 600
```

프로젝트 추가는 이 네 줄이 전부다. `[defaults]`가 공통값을 주고 프로젝트가 덮어쓴다.

**루트는 소독된 워크트리여야 한다.** 패킹된 컨텍스트는 외부로 나가므로, `.env*`,
개인키, `artifacts/private/`이 있는 루트는 실행 전에 거부된다(코드 2). 필터를
믿지 않고 루트 자체를 막는다.

## 엔진은 핀 + 패치로 관리한다

엔진(`pack_and_ask.py`)은 ChatGPT DOM을 직접 다루므로 UI가 바뀔 때마다 로컬 수정이
필요하다. 업스트림 기본 동작은 이 수정본을 `~/.cache/`에 두는데, 핀을 올리면 그대로
사라진다. 여기서는 반대로 한다.

```
lane.toml [engine] repo/sha   →  업스트림 원본 (vendor/.upstream/<sha>.py 캐시)
vendor/patches/*.patch        →  버전 관리되는 로컬 수정
lane engine sync              →  둘을 합쳐 vendor/pack_and_ask.py 생성 + 컴파일 검증
```

패치가 안 붙거나 결과가 컴파일되지 않으면 **아무것도 쓰지 않고 중단한다.**

- `0001-chatgpt-dom-2026-08.patch` — 2026-08-07 Pro 감사 때 캐시에만 남아 있던 DOM
  수정(모델명 label/value 라인, effort 값라인 읽기, 이미 선택된 항목 스킵, 메뉴 토글
  가드)을 되살린 것.
- `0002-effort-slider-2026-08.patch` — 추론단계가 라디오 목록에서 5단 슬라이더로
  바뀐 UI 대응. 이 패치는 0001이 적용된 트리 기준이다 — 단독으로 업스트림에 얹으면
  둘째 헝크가 거부된다. 적용 순서가 계약이다.
- `0003-persist-conversation-on-bind.patch` — 업스트림은 대화 URL 영속화를 "전송
  직후"라고 주석에 적어놓고 정작 `wait_for_turn_response`가 **끝난 뒤** 기록한다.
  대기 중에 프로세스가 죽는 창(2026-08-13 실측: 31분 생성 후 중단)에서는 디스크에
  아무것도 남지 않아 `--harvest` 대상이 사라진다. 결속 시점에 콜백으로 즉시 적는다.

그리고 이 레이아웃은 한동안 거짓이었다. `~/.gitignore_global`의 맨 `vendor/` 한 줄이
글로벌 ignore로 먼저 적용되어 **패치 두 개가 어느 커밋에도 들어간 적이 없었다.**
"핀을 올리면 로컬 수정이 사라진다"를 막겠다고 만든 구조가, 정작 그 수정을 이 디스크
하나에만 두고 있었다. 이제 `.gitignore`가 `!vendor/` → `vendor/*` →
`!vendor/patches/*.patch` 삼단으로 되짚어 추적한다.

## 정밀도에 대한 기본값

- `--compress`를 절대 쓰지 않는다. 함수 본문이 사라지면 리뷰 모델이 구현을 상상한다.
- `force_answer_after = 0`이 기본이다. Pro의 추론을 중간에 끊으면 확신에 찬
  미완성 답이 나온다. 시간을 묶어야 하는 프로젝트만 값을 준다.
- `--require-model`로 모델을 검증하고, 불일치·첨부 실패·빈 응답은 저장하지 않는다.

## 다음 단계

1. ~~`lane review` 안정화~~
2. ~~`lane serve` — Sol Pro 모드(대화)~~
3. ~~`lane drive` — 계획·구현·게이트 루프~~
4. ~~엔진 핀 v0.6.1 + `lane harvest`~~ ← 지금 여기. 중단된 판을 메시지 없이 회수한다.
5. insane-review fork — `0001`은 업스트림 main에 세 헝크 전부 붙는다(offset 167).
   PR 후보 확정.
6. tool call 브리지 — serve가 도구 턴을 돌리려면 필수. 지금은 400으로 거절한다.
7. 선별·구조화 강화 — 파일 집합 자동 확정, 계획 스키마 검증
