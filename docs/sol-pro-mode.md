# gjc Sol Pro 모드 — 실측으로 확인된 경로

**질문:** gjc 세션 자체를 Sol Pro로 돌릴 수 있나?
**답:** 된다. **default 모델 하나로 붙이고, 서브에이전트는 붙이지 않는다.**

## 확인된 사실 (2026-08-12 실측)

### 1. gjc는 커스텀 OpenAI 호환 provider를 받는다
`~/.gjc/agent/models.yml`에 provider를 직접 등록할 수 있다.

```yaml
providers:
  sol-pro-local:
    baseUrl: http://127.0.0.1:8799/v1
    apiKeyEnv: SOL_PRO_LOCAL_KEY
    api: openai-completions
    auth: apiKey
    models:
      - id: sol-pro
```

`api` 값은 실측으로 아래만 로드된다.

| 값 | 결과 |
|---|---|
| `openai-completions` | 로드됨 (`/v1/chat/completions`) |
| `openai-responses` | 로드됨 (`/v1/responses`) |
| `openai-chat`, `openai`, `chat-completions`, `openai-compatible` | **조용히 무시** |

틀린 값을 써도 에러가 없다. provider가 목록에서 사라질 뿐이라 디버깅이 어렵다.
`gjc --list-models`에 모델이 보이는지로 확인해야 한다.

### 2. 로컬 provider로 gjc 세션이 실제로 돈다
`spikes/openai_shim_probe.py`(카나리 응답만 내는 최소 서버)를 띄우고:

```bash
GJC_CODING_AGENT_DIR=/tmp/gjc-probe SOL_PRO_LOCAL_KEY=x \
  gjc -p --no-tools --no-session --model sol-pro "ping from gjc"
# → SHIM-OK: this text came from the local provider, not from any hosted model.
```

gjc가 shim에 보낸 요청:

```
POST /v1/chat/completions
stream=True
keys=['max_completion_tokens', 'messages', 'model', 'store', 'stream', 'stream_options', 'tools']
```

즉 shim이 만족해야 하는 계약은 세 가지다.
- **SSE 스트리밍 필수.** 비스트리밍 JSON만 돌려주면 gjc가
  `empty response with anomalously low token usage`로 거절한다.
- `usage` 토큰 수를 실제값에 가깝게 채워야 한다(위 거절 조건과 연결됨).
- **`tools`가 요청에 들어온다.** 완전한 에이전트 루프를 원하면 shim이 tool 스펙을
  프롬프트로 내리고, 응답에서 tool call을 파싱해 OpenAI 포맷으로 되돌려야 한다.

### 3. 스트림은 첫 이벤트까지의 시간이 제한된다

`gjc config`의 실측값:

```
retry.streamFirstEventTimeoutMs = 100000   # 100초
retry.streamMaxRetries = 5
retry.requestMaxRetries = 5
```

Pro는 사소한 질문에도 2분 안팔을 쓴다(실측 125~129초, gjc 시스템 프롬프트 15.8k자 포함).
그대로 두면 gjc가 100초에 끊고 재시도하며, 뒤달아 도착한 답은 죽은 소켓에 써지고
(`BrokenPipeError`) **같은 질문에 Pro 메시지를 한 통 더 날린다.**

클라이언쪽 증상은 이 한 줄로 나온다:

```
OpenAI completions stream timed out while waiting for the first event
```

중요: **SSE 주석(`: ...`)은 이벤트로 치지 않는다.** 주석으로 하트비트를 보내면
소켓은 살아있지만 타임아웃은 그대로 난다. 내용 없는 찭크(`delta.content = ""`)를
주기적으로 보내야 한다.

### 4. 추론단계는 메뉴가 아니라 슬라이더다 (2026-08 UI)

예전 동작(`menuitemradio` 목록)은 사라졌고, `[role="slider"]` 하나로 바뀌었다.

| `aria-valuenow` | pill |
|---|---|
| 0 | 빠름 |
| 1 | 중간 |
| 2 | 높음 |
| 3 | 매우 높음 |
| **4** | **Pro** |

엔진은 목록이 비면 fail-closed로 전송을 멈췄다(`'pro' 추론단계 항목 못 찾음`).
`vendor/patches/0002-effort-slider-2026-08.patch`가 슬라이더를 최소칸으로 정규화한 뒤
오른쪽으로 밀면서 composer pill을 판정 근거로 읽는다.

## 최종 구조

```
gjc 세션 ──/v1/chat/completions (stream, tools)──▶ lane serve (로컬 shim)
                                                        │
                                                        ▼
                                              vendor/pack_and_ask.py (CDP)
                                                        │
                                                        ▼
                                            ChatGPT 웹 · GPT-5.6 Sol Pro
```

`codexpro`는 이 경로에 등장하지 않는다. **Pro 모드의 병목은 codexpro가 아니라
ChatGPT 쪽 Pro 표면이므로, codexpro를 fork해도 여기서 얻는 게 없다.**

## 채택한 형태 — default 단독

판단을 쪼개지 않는다. Pro가 세션 전체를 잡고, 다른 모델을 role로 끼워넣지 않는다.

```yaml
profiles:
  sol-pro:
    required_providers: [sol-pro-local]
    model_mapping:
      default: sol-pro-local/sol-pro
```

`gjc --mpreset sol-pro`가 곧 Sol Pro 모드다.

서브에이전트를 붙이지 않으므로 `task`의 role 매핑도 이 프로필에서는 쓰지 않는다.
`~/.gjc/agent/config.yml`의 `task.agentModelOverrides`가 살아 있으면 서브에이전트는
여전히 다른 모델로 새므로, 이 모드에서는 `task`를 쓰지 않거나 오버라이드를 비운다.

## default 단독이 강제하는 것

역할을 쪼개면 Pro 호출이 태스크당 1~3회로 끝나지만, default 단독은 모든 턴이 Pro다.
그래서 아래가 선택이 아니라 **필수**가 된다.

- **tool call 브리지 필수.** 모든 도구 호출 턴이 Pro를 지난다. 프롬프트로 툴 스펙을
  내리고 응답에서 tool call을 파싱해 OpenAI 포맷으로 되돌리는 게 shim의 본체다.
  파싱 실패는 fail-closed — 추측해서 실행하지 않는다.
- **요청 직렬화 필수.** 브라우저 하나·대화 하나라 동시성은 1이다. 큐를 두고 한 번에
  하나만 태운다.
- **대화 매핑 필수.** CDP 대화는 stateful, OpenAI API는 stateless다. 매 요청마다 전체
  메시지를 다시 보내면 Pro 컨텍스트가 금방 넘친다. gjc 세션 ↔ ChatGPT 대화를 1:1로
  묶고 증분만 보낸다.
- **비용 인식.** 도구 왕복 한 번이 Pro 메시지 한 통이다. 턴당 수 분이 곱해진다.
  긴 자동화가 아니라 깊은 한 판에 쓰는 모드로 취급한다.

## 남은 작업 (로드맵 3단계)

1. `lane serve` — SSE + usage 계약을 만족하는 shim
2. tool call 브리지 — 프롬프트 주입 + 파싱, 파싱 실패 시 fail-closed
3. 세션↔ChatGPT 대화 매핑 (컨텍스트 재전송 최소화)
4. 동시 요청 직렬화 (브라우저 1개 전제)

현재 진척: 1번의 전제(gjc가 로컬 provider로 세션을 돈다)까지 실측 완료.
shim은 아직 고정 응답을 내는 카나리이고 CDP에 연결되지 않았다.
