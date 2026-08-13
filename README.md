# sol-lane

GPT-5.6 Sol Pro는 웹 구독 전용이고 API가 없다. 그래서 리뷰를 받으려면 코드를 골라
패킹하고, 로그인된 브라우저를 CDP로 몰아 투입하고, 모델을 검증하고, 응답을 회수해야
한다. 그 절차가 프로젝트마다 셸 스크립트로 복사되던 것을 하나의 CLI로 모은 것이다.

```
lane review/drive/serve ──▶ vendor/pack_and_ask.py (CDP) ──▶ ChatGPT 웹 · Sol Pro
                                      │
                                      ▼
                     <project root>/.insane-review/response_*.md
```

핵심 약속 하나: **검증되지 않은 것은 저장하지 않는다.** 빈 응답, 거절 페이지, 질문의
메아리는 exit 0을 받지 못한다. 대신 이미 값을 치른 대화에서 답을 다시 긁는 경로
(`harvest`/`salvage`)가 항상 열려 있다.

## 요구사항

- **Linux** (프로세스 간 락이 abstract socket이라 다른 OS에선 동작하지 않는다)
- **[uv](https://docs.astral.sh/uv/)** 와 Python 3.11+
- **ChatGPT Pro 구독으로 로그인된 Chrome/Chromium**, CDP 디버그 포트 `9222`.
  없으면 `lane review`/`lane serve`가 엔진의 `--ensure-env`로 전용 프로필을
  띄워보고, 그래도 안 되면 멈춘다(첫 실행은 그 프로필에서 손으로 로그인해야 한다).
- `patch` 바이너리 (`lane engine sync`가 쓴다)
- 선택: `gjc` (`lane drive`의 구현자, Sol Pro 모드의 클라이언트),
  `codexpro` + `xclip`/`wl-copy` (`--paste` 레인)

## 설치

```bash
git clone <이 레포> && cd sol-lane
uv sync --extra dev
uv run lane engine sync      # 핀된 업스트림 엔진 다운로드 + vendor/patches 적용
uv run lane doctor           # engine·browser·root 전부 ok면 준비 끝
```

### 첫 로그인 (새 머신에서 한 번만)

엔진은 주 브라우저가 아니라 **전용 프로필**(`~/.insane-review/browser-profile`)을
쓴다 — Chrome 136+는 기본 프로필에서 디버그 포트를 정책적으로 막기 때문이다.
새 머신에서는 그 프로필로 한 번 로그인해둬야 한다:

```bash
chromium --remote-debugging-port=9222 \
  --user-data-dir=$HOME/.insane-review/browser-profile \
  --no-first-run --no-default-browser-check
# 열린 창에서 chatgpt.com 으로 가서 Pro 계정으로 로그인
```

쿠키는 프로필 디렉토리에 보존되므로 이후는 `lane review`/`lane serve`가 알아서
띄우고 붙는다(`--ensure-env`). 로그인 상태는 `uv run lane doctor`의 `browser` 줄로
확인한다.

`lane doctor`가 곰 셀프테스트다. 줄마다 무엇이 빠졌는지 말해준다:

```
engine     ok       .../vendor/pack_and_ask.py
browser    up       CDP http://127.0.0.1:9222/json/version
root:myproj ok      /home/you/workspace/myproj-clean
```

## 설정 — `lane.toml`

레포 루트의 `lane.toml` 하나가 전부다. `[engine]` 핀은 그대로 두고, 자기 프로젝트를
`[projects.<이름>]`으로 추가한다.

```toml
[engine]                     # 그대로 두면 된다 — 핀 + 패치는 검증된 조합이다
repo = "fivetaku/insane-review"
sha = "eaab0b0d9e7f7ff84d5f3601289128aa8e70eb69"

[defaults]                   # 모든 프로젝트의 공통값 (프로젝트가 덮어쓴다)
model = "pro"
require_model = "GPT-5.6"
max_wait = 4200

[projects.myproj]
root = "~/workspace/myproj-clean"
include = ["README.md", "src/**/*.py", "tests/**/*.py"]
gate = "uv run pytest -q"    # lane drive의 판정자 (review만 쓰면 생략 가능)
```

**루트는 소독된 워크트리여야 한다.** 패킹된 컨텍스트는 외부로 나가므로, `.env*`,
개인키, `artifacts/private/`이 있는 루트는 실행 전에 거부된다(exit 2). 필터를
믿지 않고 루트 자체를 막고, 패킹 직전에 파일 단위로 한 번 더 검사한다.

## 첫 리뷰

```bash
uv run lane review myproj "cache.py의 TTL 만료가 동시 접근에서 성립하나" --dry-run
uv run lane review myproj "cache.py의 TTL 만료가 동시 접근에서 성립하나"
```

`--dry-run`은 실행할 엔진 커맨드만 출력한다. 실제 실행이 성공하면:

```
pack       6 files, 41 KB
response   /home/you/workspace/myproj-clean/.insane-review/response_….md
```

실패하면 이유와 함께 **돈을 더 쓰지 않는 재시도 커맨드**를 알려준다:

```
lane: review did not produce a verified response (fail-closed)
chat       https://chatgpt.com/c/…
retry      lane harvest myproj   # no new message is sent
```

### 질문 쓰는 법 (실측에서 나온 규칙 세 개)

- **보장 하나 = 판 하나.** "전체 감사해줘"는 30분 추론 후 출력 0으로 죽는다
  (4판 연속 실측). 질문 하나짜리 판은 7분 47초에 완주했다.
- **검증형으로 묻는다.** "이 보장이 성립하나, 안 하면 어디서"는 답이 오고,
  "깨는 경로를 찾아라"는 정책 거절을 부른다.
- **팩은 질문이 필요로 하는 파일만.** `--include`로 좁혀라. 200 KB를 넘으면
  경고가 뜬다.

근거 실측은 [docs/field-notes.md](docs/field-notes.md)에 있다.

## 커맨드

```bash
lane review <proj> "<질문>"        # 패킹 → 전송 → 검증된 응답 저장
lane review <proj> "<질문>" --include "src/a.py,tests/test_a.py"
lane review <proj> "<질문>" --paste    # CDP 없이 codexpro 번들 + 클립보드
lane drive  <proj> "<하고 싶은 일>"    # Pro가 계획, gjc가 구현, 게이트가 판정
lane followup <proj> "<후속 질문>"     # 이미 컨텍스트가 있는 대화에 이어 묻기 (재패킹 없음)
lane harvest <proj>                # 값을 치른 대화에서 답만 회수 (전송 없음)
lane salvage <proj>                # 중단된 대화의 추론이라도 건진다 (UNVERIFIED 표기)
lane serve                         # Sol Pro를 로컬 OpenAI 호환 엔드포인트로
lane projects                      # 설정된 프로젝트 목록
lane doctor                        # 환경 점검
lane engine sync [--refresh]       # 핀 + 패치 재조립
```

모든 서브커맨드가 `--dry-run`(review/drive/harvest/followup)과 고정 종료코드를 지원한다:

| 코드 | 의미 |
|---|---|
| 0 | 성공 — 검증된 응답을 회수해 저장 |
| 1 | 전달 실패 — CDP 없음, fail-closed 중단, 빈 응답 |
| 2 | 설정/엔진 문제 — 알 수 없는 프로젝트, 위험한 루트, 엔진 미벤더링 |

`harvest`/`salvage`/`followup`은 대상 대화를 명시하지 않으면 그 프로젝트의 최신 run
manifest(`.insane-review/manifest_*.json`)를 쓴다. URL이나 manifest 경로를 직접 줄
수도 있다.

## `lane drive` — Pro가 계획, gjc가 구현, 게이트가 판정

```bash
lane drive myproj "<하고 싶은 일>" [--max-iters 2] [--session <sdk-session-id>]
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

구현은 **lane 전용 세션 디렉토리**(`.ai-bridge/lane-session`)에서 돌고 30분 상한이
있다. 네가 쓰고 있는 live 세션에 프롬프트가 주입될 일이 없다 — 그러려면
`--session <id>`로 명시해야 한다.

### 게이트를 고쳐서 게이트를 통과할 수는 없다

구현자와 게이트는 같은 워크트리를 공유하므로, "게이트를 통과시켜라"는 실패 테스트
삭제·`addopts = --ignore=tests`·게이트 스크립트 `exit 0`으로도 만족된다. 프롬프트의
금지 문구로는 막을 수 없어서, 드라이브 시작 시점에 **검증 파일들의 해시를 얼린다.**

- 대상: `gate_protected` 글롭(기본 `tests/**/*`, `**/conftest.py`, `pyproject.toml`,
  `pytest.ini`, `setup.cfg`, `tox.ini`, `noxfile.py`, `Makefile`, `uv.lock` 등) +
  게이트 커맨드가 실제로 가리키는 레포 내 파일(`./scripts/gate.sh` 같은 것).
- 구현 후 하나라도 수정·삭제됐으면 **게이트를 실행하지 않고** 실패한다. 게이트 실행
  중에 바뀌어도 판정을 무효화한다.
- 파일 추가는 허용된다 — 단 `conftest.py`류의 신규 등장은 기존 테스트를 재배선하므로
  막는다. 빨간 게이트는 테스트를 더 넣어도 초록이 되지 않는다.

게이트 출력은 도착하는 대로 tail만 남기고(수 GB 로그가 lane을 OOM으로 못 끌고 간다),
환경변수는 allowlist로 주고, 남은 출력에서 살아있는 시크릿을 지운 뒤에야 다음 Pro
프롬프트에 실린다.

## Sol Pro 모드 — gjc 세션을 Pro로 돌리기

```bash
uv run lane serve             # 127.0.0.1:8799 에 OpenAI 호환 엔드포인트
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

알아둘 것 세 가지:

- **턴마다 구독 메시지 1통 + 수 분이 든다.** 도구 왕복 하나하나가 Pro 메시지다.
  긴 자동화가 아니라 깊은 한 판에 쓰는 모드다.
- tool call은 산문 브리지로 오간다. 파싱 불가능한 호출은 실행되지 않고 에러가 된다
  (fail-closed), `tool_choice: required`는 400이다.
- 루프백 밖에 바인드하려면 `SOL_PRO_LOCAL_KEY`가 필수다 — 포트에 닿는 누구나
  구독 메시지를 쓸 수 있기 때문이다.

계약 전체(스트리밍·타임아웃·gjc 쪽 실측)는 [docs/sol-pro-mode.md](docs/sol-pro-mode.md).

## 엔진은 핀 + 패치로 관리한다

엔진(`pack_and_ask.py`)은 ChatGPT DOM을 직접 다루므로 UI가 바뀔 때마다 로컬 수정이
필요하다. 업스트림 기본 동작은 이 수정본을 `~/.cache/`에 두는데, 핀을 올리면 그대로
사라진다. 여기서는 반대로 한다.

```
lane.toml [engine] repo/sha   →  업스트림 원본 (vendor/.upstream/<sha>.py 캐시)
vendor/patches/*.patch        →  버전 관리되는 로컬 수정
lane engine sync              →  둘을 합쳐 vendor/pack_and_ask.py 생성 + 컴파일 검증
```

패치가 안 붙거나 결과가 컴파일되지 않으면 **아무것도 쓰지 않고 중단한다.** 실행
시점에는 manifest(`vendor/engine.json`)가 핀·패치·결과물 해시의 일치를 증명해야
엔진이 돈다. `LANE_ENGINE=<경로>`로 임시 엔진을 꽂을 수 있지만, 그 우회는 매 실행마다
stderr에 경고를 찍는다.

각 패치가 왜 존재하는지는 [docs/field-notes.md](docs/field-notes.md#패치별-유래).

## 정밀도에 대한 기본값

- `--compress`를 절대 쓰지 않는다. 함수 본문이 사라지면 리뷰 모델이 구현을 상상한다.
- `force_answer_after = 0`이 기본이다. Pro의 추론을 중간에 끊으면 확신에 찬
  미완성 답이 나온다. 시간을 묶어야 하는 프로젝트만 값을 준다.
- `--require-model`로 모델을 검증하고, 불일치·첨부 실패·빈 응답·거절 페이지·프롬프트
  메아리는 저장하지 않는다.

## 테스트

```bash
uv run pytest -q        # 단위 + 샌드박스 전부 — Pro 메시지 0통, 브라우저 불필요
```

`tests/test_sandbox.py`는 프로세스 경계를 실물로 건넌다: 스텁 엔진 서브프로세스
(`LANE_ENGINE` 봉합선), 실제 HTTP 소켓의 serve, PATH의 가짜 `gjc` 실행파일, 실제
게이트 프로세스. 돈이 드는 두 가지(Pro 호출, CDP 브라우저)만 가짜다. 유일하게
샌드박스로 검증 불가능한 것은 ChatGPT DOM 자체 — 그건 `lane review <proj> "<질문>"`
한 판이 곧 live 테스트다.

## 문서

- [docs/sol-pro-mode.md](docs/sol-pro-mode.md) — gjc 통합 계약과 실측
- [docs/field-notes.md](docs/field-notes.md) — 긴 판 사망 기록, 감사 원장, 패치 유래

## 다음 단계

1. ~~`lane review` 안정화~~
2. ~~`lane serve` — Sol Pro 모드(대화)~~
3. ~~`lane drive` — 계획·구현·게이트 루프~~
4. ~~엔진 핀 v0.6.1 + `lane harvest` + `lane salvage`~~
5. ~~tool call 브리지~~
6. insane-review 업스트림 반영 — [docs/field-notes.md](docs/field-notes.md#패치별-유래) 참고
7. 세션↔대화 매핑 — `serve`는 아직 호출마다 전체 트랜스크립트를 다시 렌더한다.
   조각(`--continue-chat`, `lane followup`)은 있고 serve 결선만 남았다.
8. 선별·구조화 강화 — 파일 집합 자동 확정, 계획 스키마 검증.
