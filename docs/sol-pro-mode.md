# gjc Sol Pro 모드 — 실측으로 확인된 경로

**질문:** gjc 세션 자체를 Sol Pro로 돌릴 수 있나?
**답:** 된다. 단 default 모델이 아니라 **role 모델**로만 쓸 값어치가 있다.

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

## 왜 default 모델로 쓰면 안 되나

- Pro는 한 턴에 수 분을 쓴다. 에이전트 루프는 태스크당 수십 턴이다. 곱하면 시간이 폭발한다.
- Pro 쿼터는 웹 구독 메시지 캡이다. 턴마다 깎인다.
- 브라우저 하나·대화 하나 = 동시성 1. 서브에이전트 병렬이 죽는다.
- CDP 대화는 stateful인데 OpenAI API는 stateless다. 매 요청 전체 메시지를 다시 보내면
  Pro 컨텍스트가 금방 넘친다. 세션↔대화 매핑 전략이 필요하다.

## 그래서 쓸 만한 형태

Pro를 **판단 역할에만** 붙인다. 태스크당 Pro 호출 1~3회로 끝난다.

```yaml
profiles:
  sol-pro:
    required_providers: [sol-pro-local, openai-codex]
    model_mapping:
      default:  openai-codex/gpt-5.6-sol:high
      planner:  sol-pro-local/sol-pro
      critic:   sol-pro-local/sol-pro
      executor: openai-codex/gpt-5.6-terra:xhigh
```

이러면 `gjc --mpreset sol-pro`가 곧 Sol Pro 모드다. 계획과 비평은 Pro가, 수십 번의
도구 호출은 빠른 모델이 한다.

## 남은 작업 (로드맵 3단계)

1. `lane serve` — SSE + usage 계약을 만족하는 shim
2. tool call 브리지 — 프롬프트 주입 + 파싱, 파싱 실패 시 fail-closed
3. 세션↔ChatGPT 대화 매핑 (컨텍스트 재전송 최소화)
4. 동시 요청 직렬화 (브라우저 1개 전제)
