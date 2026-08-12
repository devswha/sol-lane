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

## 설정 (`lane.toml`)

```toml
[projects.ea]
root = "~/workspace/ea-sol-wt"
include = ["README.md", "src/ea/**/*.py", "tests/**/*.py"]
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
`vendor/patches/0001-chatgpt-dom-2026-08.patch`는 2026-08-07 Pro 감사 때 캐시에만
남아 있던 DOM 수정(모델명 label/value 라인, effort 값라인 읽기, 이미 선택된 항목
스킵, 메뉴 토글 가드)을 되살린 것이다.

## 정밀도에 대한 기본값

- `--compress`를 절대 쓰지 않는다. 함수 본문이 사라지면 리뷰 모델이 구현을 상상한다.
- `force_answer_after = 0`이 기본이다. Pro의 추론을 중간에 끊으면 확신에 찬
  미완성 답이 나온다. 시간을 묶어야 하는 프로젝트만 값을 준다.
- `--require-model`로 모델을 검증하고, 불일치·첨부 실패·빈 응답은 저장하지 않는다.

## 다음 단계

1. ~~`lane review` 안정화~~
2. ~~`lane serve` — Sol Pro 모드(대화)~~
3. ~~`lane drive` — 계획·구현·게이트 루프~~ ← 지금 여기
4. insane-review fork — `vendor/patches/*`를 커밋으로 승격하고 업스트림에 PR
5. 선별·구조화 강화 — 파일 집합 자동 확정, 계획 스키마 검증
