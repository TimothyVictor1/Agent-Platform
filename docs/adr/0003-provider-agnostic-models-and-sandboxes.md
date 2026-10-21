# ADR 0003: Provider-agnostic models and sandboxes

Status: accepted (T00)

## Context

Earlier systems were locked to one model gateway and one paid sandbox vendor. The
platform must run free on local hardware (local vision models, local Docker
desktops) and switch to cloud providers by configuration only.

## Decision

- `ModelClient` (`models/base.py`) is the only way the runtime calls a model.
  Two adapters cover everything: `openai_compatible` (llama.cpp, Ollama, vLLM,
  LM Studio, OpenRouter, Gemini's OpenAI endpoint) and `anthropic`. Retries,
  concurrency limits, accounting and debug logging are wrappers around any client.
  A text fallback parser turns textual actions into tool calls for models without
  reliable tool calling.
- `SandboxProvider` / `SandboxHandle` (`sandboxes/base.py`) is the only way the
  runtime touches a desktop. Coordinates given to a handle are always sandbox
  pixels; mapping from the model's coordinate mode is the action layer's job.
- Model profiles in `config/models.yaml` map roles (planner, supervisor, agent,
  summarizer) to providers; switching providers is a config change.
- A shared contract test suite runs against every sandbox provider; `FakeModel`
  and `FakeSandbox` make every other test independent of networks and Docker.

## Consequences

- Provider features beyond the common interface (e.g. native computer-use tools)
  are optional extras, never required by the loop.
- Each new provider must pass the same contract suite before it is used.
