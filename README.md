# tiny-tracer — Zero-Dependency Distributed Tracing for AI Agents

> **OpenTelemetry-style spans, W3C TraceContext propagation, sampling, and exporters. One file, zero deps, MIT.**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](tiny_tracer.py)

## Why?

Every AI agent is a distributed system — LLM calls, tool executions, retries, and external APIs span multiple components. Without tracing, debugging a slow agent is guesswork. With OpenTelemetry, you need SDKs, exporters, and configuration. `tiny-tracer` gives you the core concepts — spans, trace context, sampling, and export — in a single Python file.

## Features

- **🪶 Zero dependencies** — stdlib only (`json`, `uuid`, `threading`, `contextvars`)
- **📦 Single file** — drop `tiny_tracer.py` anywhere
- **🏷️ OpenTelemetry-style spans** — name, attributes, events, status, exception recording
- **🔗 W3C TraceContext** — `traceparent` header format, propagatable across HTTP/service boundaries
- **🎯 Deterministic sampling** — `always_on`, `always_off`, `trace_id_ratio` (same trace always gets same sample decision)
- **📤 Dual exporters** — `stdout` (pretty JSON) or `jsonl` file (pipeline-friendly)
- **🔁 Async support** — `trace_async()` for `async with` spans, decorator works for both sync and async
- **🧩 Nesting** — spans auto-parent to the current active span
- **📊 `@traced` decorator** — auto-span any function with one line

## Install

```bash
pip install tiny-tracer
```

Or copy `tiny_tracer.py` — it's MIT licensed, no attribution required.

## Quick Start

```python
from tiny_tracer import trace, configure, traced, get_current_span

# Configure once at startup
configure(
    service_name="my-agent",
    exporter="jsonl",          # "stdout" | "jsonl" | "null"
    output_file="traces.jsonl",
    sampler="trace_id_ratio",  # sample 10% of traces
    sample_ratio=0.1,
)

# ── Context manager ──────────────────────────────────────────
with trace("llm.call", attributes={"model": "gpt-4o", "prompt_tokens": 500}):
    response = llm.generate(prompt)
    # ... do work
    # Exception auto-recorded on exit

# ── Decorator (recommended for functions) ────────────────────
@traced()
def process_user_message(message: str) -> str:
    with trace("embed.query"):
        embedding = embed(message)
    with trace("retrieval"):
        context = db.search(embedding)
    with trace("llm.response"):
        return llm.generate(f"Context: {context}\n\nUser: {message}")

@traced("custom.span.name")
async def async_agent_loop(messages: list) -> str:
    ...

# ── Access current span ──────────────────────────────────────
def my_tool():
    span = get_current_span()
    if span:
        span.add_event("tool.started", {"tool": "file.read"})
        span.set_attribute("file.path", "/tmp/data.json")
    # ... work ...
    if span:
        span.add_event("tool.done", {"bytes": 1024})

# ── W3C propagation (HTTP headers) ───────────────────────────
from tiny_tracer import get_trace_context, extract_trace_context

# Client side: inject into outgoing request headers
headers = {**get_trace_context()}
response = requests.post("http://api.example.com/chat", json=payload, headers=headers)

# Server side: extract and continue trace
trace_id, span_id = extract_trace_context(request.headers)
with trace("api.handler", parent_span_id=span_id, attributes={"trace_id": trace_id}):
    ...
```

## Architecture

```
 trace("outer")
   └─ trace("llm.call")
        └─ trace("embed.query")
        └─ trace("retrieval")
   └─ trace("async_agent")
        └─ trace("step.1")
        └─ trace("step.2")
```

Each span records:
- `trace_id` — shared across all related spans
- `span_id` — unique per span
- `parent_span_id` — links to the calling span
- `start_time`, `end_time`, `duration_ms`
- `attributes` — key/value metadata
- `events` — timestamped log entries
- `status` — `ok` | `error` | `unset`

## Output Formats

**stdout (pretty):**
```json
{
  "name": "llm.call",
  "trace_id": "a1b2c3d4e5f6...",
  "span_id": "0123456789abcdef",
  "parent_span_id": "fedcba9876543210",
  "service_name": "my-agent",
  "start_time_iso": "2026-08-11T13:00:00Z",
  "end_time_iso": "2026-08-11T13:00:01Z",
  "duration_ms": 1203.45,
  "status": "ok",
  "attributes": {"model": "gpt-4o"},
  "events": []
}
```

**jsonl file (for pipelines / Jaeger / GrafanaTempo ingestion):**
```jsonl
{"name":"llm.call","trace_id":"...","span_id":"...","parent_span_id":"...","service_name":"my-agent","start_time_iso":"...","end_time_iso":"...","duration_ms":1203.45,"status":"ok","attributes":{},"events":[]}
{"name":"embed.query","trace_id":"...","span_id":"...","parent_span_id":"...","service_name":"my-agent",...}
```

## API Reference

### `configure(...)`

```python
configure(
    service_name="my-service",   # Default: OTEL_SERVICE_NAME env var or "tiny-tracer"
    exporter="jsonl",            # stdout | jsonl | null
    output_file="traces.jsonl", # Path for jsonl exporter
    sampler="always_on",         # always_on | always_off | trace_id_ratio
    sample_ratio=0.1,           # 0.0–1.0, only for trace_id_ratio
    pretty=False,               # Pretty-print JSON on stdout
)
```

### `trace(name, attributes=None, parent_span_id=None)`

Context manager that opens a span. Returns a `SpanContext`.

### `trace_async(name, attributes=None, parent_span_id=None)`

Async context manager for async functions.

### `@traced(name=None, attributes=None)`

Decorator that auto-wraps any sync or async function with a span.

### `get_current_span() -> Optional[Span]`

Returns the currently active span (or None).

### `get_trace_context() -> dict`

Returns W3C TraceContext headers for injecting into outgoing HTTP requests.

### `extract_trace_context(headers: dict) -> (trace_id, span_id)`

Parses W3C TraceContext `traceparent` header.

### `Span` methods

```python
span.set_attribute("key", value)
span.add_event("event.name", {"attr": "val"})
span.record_exception(exc)   # auto-sets status="error" + exception event
span.end()                  # usually called automatically by context manager
```

## Ecosystem

Part of the **tiny-\*** ecosystem — zero-dependency, single-file Python tools:

| Tool | Description |
|------|-------------|
| `tiny-tracer` | Distributed tracing |
| `tiny-budget` | Token cost tracking |
| `tiny-feature-flags` | Feature flags |
| `tiny-log` | Structured logging |
| `tiny-validator` | Input validation |
| `tiny-agent` | AI agent framework |
| `fast-cache` | LRU + TTL cache |
| [All tiny-* repos →](https://github.com/hussain-alsaibai?tab=repositories&q=tiny-) |

## License

MIT — use freely, no attribution required.
