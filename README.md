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

1. ~~`lane review` 안정화~~ ← 지금 여기
2. insane-review fork — 패치를 커밋으로 승격하고 업스트림에 PR
3. gjc SDK 판단층 — 파일 선별, 응답→계획 구조화, 게이트 PASS/FAIL
4. `lane implement` — codexpro `watch-handoff`/`loop-handoff`로 구현·검증 루프
