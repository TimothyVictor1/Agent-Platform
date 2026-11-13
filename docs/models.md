# Models: providers, profiles and the client stack

How the platform talks to language models (SPEC §8) and how to point a profile in
`config/models.yaml` at a particular server. Code: `backend/src/agentplatform/models/`.

## The client stack

Everything in the runtime calls models through `ModelClient` (`models/base.py`).
`build_client(role, prices, shared=..., debug_dir=...)` in `models/factory.py` builds one
client per profile role. From the outside in, the stack is:

| Layer | What it does |
| --- | --- |
| `DebugLoggingClient` (opt-in, off by default) | Only added when `build_client(..., debug_dir=...)` is given. Appends one JSONL record per call to `<debug_dir>/<client>-<date>.jsonl`. Images are replaced by `{sha256, bytes}`, secrets in error messages are redacted, and API keys never reach this layer. |
| `AccountingClient` | Fills `usage.images` and `usage.cost_usd` from the `prices` table (tokens per 1M, `image` per image; a missing model costs 0). |
| `RetryingClient` | Up to 4 attempts. Uses exponential back-off with full jitter and honours `Retry-After`. Retries 408/409/429/5xx, timeouts and connection errors. `req.timeout_s` bounds the whole call, retries included. |
| `ConcurrencyLimitedClient` | A semaphore shared by every client for the same server: per `base_url` for OpenAI-compatible, per model for Anthropic. The size comes from the role's `concurrency`; if two roles on one server disagree, the first one built wins and a warning is logged. |
| Provider client | `OpenAICompatibleClient` or `AnthropicClient`. SDK retries are off. |

**Debug logs contain prompts.** A record holds the full request (system prompt, task
text, board digests, typed text, tool arguments) and the full response. That can include
personal data or credentials that a user or a web page put into the conversation. Logging
is therefore opt-in: `build_client` adds `DebugLoggingClient` only when `debug_dir` is
passed (the conventional location is `data/model-debug/`, `DEFAULT_DEBUG_DIR`). Enable it
only while debugging, keep the directory out of backups and shared bundles, and delete
the files afterwards. Payloads that cannot be dumped are logged as
`{"unloggable": <error type>}`. Nesting deeper than 64 levels is cut off as
`"<max-depth>"`. Logging never fails the model call.

The semaphore sits inside the retry loop. A request sleeping out a back-off therefore does not
hold a slot another agent could use.

Create **one** `SharedModelState` per process (in `main.py`, T13) and pass it to every
`build_client` call. It holds the semaphores and the "server rejects forced tool use"
cache. Without it, each client gets private state.

The text tool-call fallback parser (`models/fallback_parser.parse_text_tool_calls`) is
not part of the stack. Provider clients never run it. The agent loop runs it through
`AgentRuntime(fallback_parser=parse_text_tool_calls)`.

## Retry layers: the loop and the client both retry

There are two retry layers, as the SPEC describes, and they multiply:

- **`RetryingClient`**: up to **4 attempts per `complete()` call** for transient
  provider errors, all within the request's `timeout_s`.
- **The agent loop** (`runtime/agent/loop.py`, `AgentRuntimeConfig.max_consecutive_model_errors = 3`, `error_backoff_s = 2.0`):
  when a `complete()` call still fails (`ModelError`), the loop emits `agent.error`,
  backs off and runs the step again. The task fails after **3 consecutive failed
  steps**.

In the worst case, a provider that keeps returning 503 sees up to 3 × 4 = **12 requests**
before a task fails, plus both layers' back-off time. That is deliberate, so a short
provider outage does not fail tasks. It does put more load on a struggling server.
**Decision for T13:** keep both, or reduce one of them, for example
`RetryingClient(max_attempts=2)` for local servers, or a lower
`max_consecutive_model_errors`. Non-retryable errors (400, 401, 404) are not retried by
`RetryingClient`, so they cost one request per loop attempt.

## Provider behaviour worth knowing

**OpenAI-compatible**:
- Images are sent as `image_url` data URLs inside **user** messages only. Tool results
  are text-only `role: tool` messages.
- `tool_choice: "required"` is sent as-is. If the server answers 400/422, the client
  retries once with `"auto"` and remembers that base URL. Many local servers do not
  support `required`; the loop and the fallback parser handle replies with no tool call.
- `response_format: "json"` is sent as `{"type": "json_object"}`.
- With no `api_key_env`, the client sends the placeholder key `not-needed`. This keeps
  the SDK from picking up `OPENAI_API_KEY` from the environment and sending it to an
  unrelated (e.g. local) server.
- Reasoning models (e.g. Nemotron 3.5 Super VL and GLM-5.3 on Nebius) return their
  thinking in `reasoning_content` (or `reasoning`), often with an empty `content`. The
  client keeps it in `ModelResponse.reasoning`. It is never sent back to the model; the
  agent step's `reasoning_excerpt` uses it when there is no text (secret-redacted,
  whitespace-collapsed, at most 400 characters). Reasoning tokens count against
  `max_output_tokens`.

**Coordinates (vision roles)**: set `coordinate_mode` to what the model actually emits,
not what the prompt asks for. **Nemotron 3.5 Super VL answers in screen fractions**: with
`absolute` it ignored "pixels in the 1280x800 screenshot" and sent 0..1000 values (the
first live run clicked (499, 470) meaning the centre, and (58, 912) below an 800-pixel
screen), and without instructions it sent 0..1 decimals. Use `normalized_1000` for it.
Two safety nets in `actions.py`: in `absolute` and `normalized_1000` modes, a point
whose coordinates both look like 0..1 fractions (e.g. `0.499, 0.47`) is rejected with a
model-readable error instead of clicking the top-left corner, and a point off the
screenshot (but within twice its range) is clamped to the edge and the tool result says
so (`; y=912 off-screen, clamped to the edge (...)`).

**Text-only supervisor**: the supervisor attaches up to 3 screenshots to stuck and
review runs only when its role has `vision: true`. GLM-5.3 on Nebius is text-only and
rejects any image with HTTP 400 "This model does not support image input", so with it
the supervisor decides from the run text alone and cannot look at the agent's screen.
For later: a vision-capable supervisor (e.g. a VL model with good tool use, or reusing
the agent's VL model) would let it judge stuck agents and results from the screenshots.

**Anthropic**:
- System messages go into the `system` parameter. Images are base64 `image` blocks.
  Tool results become `tool_result` blocks, merged into one user turn together with the
  following observation.
- `tool_choice: "required"` maps to `{"type": "any"}`. Current models (Opus 5.5, Sonnet
  5.5, Fable 5.1) reject forced tool use with a 400. The client then retries with
  `{"type": "auto"}` and remembers that model.
- `temperature` is **not sent**. The Messages API rejects sampling parameters on every
  model, and SDK 1.x removed them.
- `response_format: "json"` is only a hint: one line is appended to the system prompt.
- Current models think by default. Thinking tokens count against `max_output_tokens`
  (the role's `max_output_tokens`, default 1024), so give Anthropic roles a larger value,
  e.g. 4096–16000.
- **Known limitation: thinking blocks are dropped.** `AnthropicClient` keeps only the
  `text` and `tool_use` blocks of a reply. `thinking` and `redacted_thinking` blocks are
  discarded, because the provider-agnostic `Message` contract has no field to carry them
  into the next request. The Messages API expects the thinking blocks of an assistant
  turn that called tools to be sent back unchanged with the matching `tool_result`s. So
  **multi-step tool use with a thinking model may fail** (a 400 on the second step, or
  the model losing its reasoning between steps). This stays open until a contract change
  adds an opaque, provider-specific field to assistant messages, for example
  `Message.provider_blocks`, that the client round-trips. That change needs a Contract
  change request; it is not part of T02.

## Pointing a profile at a server

Keys are never written in `models.yaml`. `api_key_env` names the variable in `.env`,
and the factory raises `ConfigError` if that variable is missing or empty. A base URL
that is not HTTPS counts as "local" (smaller prompt budget; usually no price entry, so
cost 0).

### llama.cpp server

```yaml
agent:
  provider: openai_compatible
  base_url: http://z2.local:8081/v1      # llama-server --host 0.0.0.0 --port 8081
  model: "qwen2.5-vl-7b"                 # any name; llama-server serves one model
  vision: true                           # start llama-server with --mmproj for images
  coordinate_mode: normalized_1000       # check what your GUI model outputs
  concurrency: 2                         # match llama-server --parallel
```

- Start the server with `--jinja` so the chat template handles tool calls.
- If the model answers with text actions instead of tool calls, the fallback parser
  converts them.
- Add `api_key_env: LOCAL_LLM_KEY` only if you started it with `--api-key`.

### Ollama

```yaml
agent:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: "qwen2.5vl:7b"                  # the Ollama model tag
  vision: true
  concurrency: 1                         # raise together with OLLAMA_NUM_PARALLEL
```

Ollama needs no key.

### vLLM

```yaml
agent:
  provider: openai_compatible
  base_url: http://gpu-box:8000/v1       # vllm serve <model> --port 8000
  model: "Qwen/Qwen2.5-VL-7B-Instruct"   # the served model name
  api_key_env: VLLM_API_KEY              # only if started with --api-key
  vision: true
  concurrency: 6
```

For structured tool calls, start vLLM with `--enable-auto-tool-choice` and a
`--tool-call-parser` that matches the model.

### OpenRouter

```yaml
agent:
  provider: openai_compatible
  base_url: https://openrouter.ai/api/v1
  model: "<vendor/model:free>"           # pick a vision model for the agent role
  api_key_env: OPENROUTER_API_KEY
  vision: true
  concurrency: 1                         # free models are heavily rate limited
```

429s are retried using OpenRouter's `Retry-After`. Add the model to `prices` if it is
not free.

### Gemini (OpenAI-compatible endpoint)

```yaml
agent:
  provider: openai_compatible
  base_url: https://generativelanguage.googleapis.com/v1beta/openai/
  model: "<gemini model id>"
  api_key_env: GEMINI_API_KEY
  vision: true
  coordinate_mode: normalized_1000       # Gemini models usually answer in 0..1000
```

### Anthropic

```yaml
agent:
  provider: anthropic
  model: "claude-sonnet-5-5"
  api_key_env: ANTHROPIC_API_KEY         # the default for provider: anthropic
  vision: true
  coordinate_mode: absolute
  max_output_tokens: 8192                # leave room for thinking
  concurrency: 6
prices:
  "claude-sonnet-5-5": { input: 2.0, output: 10.0 }
```

Leave `base_url` out to use the public API.

## Health checks

`client.health()` never raises. It returns `HealthStatus(ok, latency_ms, error)`.

- **OpenAI-compatible**: sends `GET {base_url}/models`.
- **Anthropic**: sends `GET /v1/models/{model}`, which checks the key, reachability
  and the model id at no cost.

## Tests

`backend/tests/unit/models/` runs against `httpx2.MockTransport`, so it needs no network.
The response fixtures are in `backend/tests/fixtures/models/`.
