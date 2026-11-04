"""
tiny-tracer — Zero-Dependency Distributed Tracing for AI Agents
---------------------------------------------------------------
OpenTelemetry-style spans, W3C TraceContext propagation, sampling,
stdout + file exporters, and async support. One file, zero deps, MIT.

Usage:
    from tiny_tracer import trace, get_current_span, configure, new_trace_id

    configure(service_name="my-agent", exporter="jsonl", output_file="traces.jsonl")
    with trace("llm.call", attributes={"model": "gpt-4o"}):
        response = llm.call(prompt)
"""


from __future__ import annotations

__version__ = "0.1.0"

import os
import sys
import json
import time
import uuid
import threading
import contextvars
from typing import Any, Callable, Iterator, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum

# ─── Types ────────────────────────────────────────────────────────────────────

class SpanStatus(Enum):
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"

@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    service_name: str
    start_time_iso: str
    end_time_iso: Optional[str] = None
    duration_ms: Optional[float] = None
    status: str = "unset"
    error_message: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Optional[dict[str, Any]] = None) -> None:
        self.events.append({
            "name": name,
            "timestamp_iso": _now_iso(),
            "attributes": attributes or {},
        })

    def record_exception(self, exc: BaseException) -> None:
        self.status = "error"
        self.error_message = f"{type(exc).__name__}: {exc}"
        self.add_event("exception", {
            "exception.type": type(exc).__name__,
            "exception.message": str(exc),
        })

    def end(self) -> None:
        if self.end_time_iso is not None:
            return  # already ended
        self.end_time_iso = _now_iso()
        self.duration_ms = round(
            (datetime.fromisoformat(self.end_time_iso.replace("Z", "+00:00"))
             - datetime.fromisoformat(self.start_time_iso.replace("Z", "+00:00"))).total_seconds() * 1000, 2
        )
        if self.status == "unset":
            self.status = "ok"

# ─── Config ───────────────────────────────────────────────────────────────────

_config: dict[str, Any] = {
    "service_name": os.environ.get("OTEL_SERVICE_NAME", "tiny-tracer"),
    "sampler": "always_on",   # "always_on" | "always_off" | "trace_id_ratio"
    "sample_ratio": 1.0,
    "exporter": "stdout",      # "stdout" | "jsonl" | "null"
    "output_file": "traces.jsonl",
    "pretty": False,
    "lock": threading.Lock(),
}

_global_spans: list[Span] = []
_finished_spans: list[Span] = []

# ─── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def new_trace_id() -> str:
    return uuid.uuid4().hex[:32]

def new_span_id() -> str:
    return uuid.uuid4().hex[:16]

def _should_sample(trace_id: str) -> bool:
    sampler = _config["sampler"]
    if sampler == "always_off":
        return False
    if sampler == "always_on":
        return True
    # trace_id_ratio: use first bytes as 0-255 value
    ratio = _config.get("sample_ratio", 1.0)
    try:
        bucket = int(trace_id[:8], 16) % 256
        return bucket < int(ratio * 256)
    except Exception:
        return True

# ─── Context ──────────────────────────────────────────────────────────────────

_current_span_var: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar(
    "current_span", default=None
)

def get_current_span() -> Optional[Span]:
    """Return the currently active span (or None)."""
    return _current_span_var.get()

def get_trace_context() -> dict[str, str]:
    """Return W3C TraceContext headers for propagation."""
    span = get_current_span()
    if span:
        return {
            "traceparent": f"00-{span.trace_id}-{span.span_id}-01",
            "tracestate": "",
        }
    trace_id = new_trace_id()
    return {
        "traceparent": f"00-{trace_id}-{new_span_id()}-00",
        "tracestate": "",
    }

def extract_trace_context(headers: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    """Parse W3C TraceContext traceparent header. Returns (trace_id, span_id)."""
    tp = headers.get("traceparent") or headers.get("traceparent".title()) or ""
    if tp.startswith("00-"):
        parts = tp.split("-")
        if len(parts) >= 3:
            return parts[1], parts[2]
    return None, None

# ─── Core Tracing ─────────────────────────────────────────────────────────────

class SpanContext:
    """Active span scope. Created by trace() / trace_async()."""

    __slots__ = ("_span", "_token")

    def __init__(self, span: Span):
        self._span = span
        self._token: contextvars.Token = _current_span_var.set(span)

    def __enter__(self) -> Span:
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self._span.record_exception(exc_val)
        self._span.end()
        _current_span_var.reset(self._token)
        _export_span(self._span)

def trace(
    name: str,
    attributes: Optional[dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
    links: Optional[list[dict[str, Any]]] = None,
) -> SpanContext:
    """
    Open a new span. Acts as a context manager:

        with trace("db.query", attributes={"sql": "SELECT *"}):
            rows = db.execute(sql)

    Nested spans inherit the current span as parent automatically.
    """
    current = get_current_span()
    trace_id = current.trace_id if current else new_trace_id()

    if not _should_sample(trace_id):
        # Return a no-op context that skips export
        return _NoOpSpanContext()

    span_id = new_span_id()
    parent_id = current.span_id if current else (parent_span_id or None)

    span = Span(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_id,
        service_name=_config["service_name"],
        start_time_iso=_now_iso(),
        attributes=dict(attributes or {}),
        links=list(links or []),
    )

    return SpanContext(span)


class _NoOpSpanContext:
    """No-op span context when sampling is off."""
    def __enter__(self):
        noop = Span(
            name="", trace_id="", span_id="",
            parent_span_id=None, service_name="",
            start_time_iso=_now_iso(),
        )
        return noop
    def __exit__(self, *a):
        pass
    def __getattr__(self, name):
        return lambda *a, **k: None


async def trace_async(
    name: str,
    attributes: Optional[dict[str, Any]] = None,
    parent_span_id: Optional[str] = None,
) -> SpanContext:
    """Async version of trace(). Use with `async with`."""
    return trace(name, attributes, parent_span_id)


# ─── Decorator ────────────────────────────────────────────────────────────────

def traced(
    name: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
) -> Callable:
    """
    Decorator to auto-span any function:

        @traced()
        def my_func(x, y):
            return x + y

        @traced("custom.name")
        async def my_async_func(data):
            return await process(data)
    """
    def decorator(fn: Callable) -> Callable:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        if asyncio_iscoroutinefunction(fn):

            async def async_wrapper(*args, **kwargs):
                attrs = dict(attributes or {})
                attrs["function"] = fn.__qualname__
                attrs["module"] = fn.__module__
                with trace(span_name, attributes=attrs) as span:
                    span.set_attribute("input.args", str(args)[:200])
                    try:
                        result = await fn(*args, **kwargs)
                        span.set_attribute("output.size", _sizeof(result))
                        return result
                    except Exception as exc:
                        span.record_exception(exc)
                        raise

            return async_wrapper

        else:

            def sync_wrapper(*args, **kwargs):
                attrs = dict(attributes or {})
                attrs["function"] = fn.__qualname__
                attrs["module"] = fn.__module__
                with trace(span_name, attributes=attrs) as span:
                    span.set_attribute("input.args", str(args)[:200])
                    try:
                        result = fn(*args, **kwargs)
                        span.set_attribute("output.size", _sizeof(result))
                        return result
                    except Exception as exc:
                        span.record_exception(exc)
                        raise

            return sync_wrapper

    return decorator


def _sizeof(obj: Any) -> int:
    """Rough byte size estimate."""
    try:
        return sys.getsizeof(json.dumps(obj, default=str))
    except Exception:
        return -1

def asyncio_iscoroutinefunction(fn: Callable) -> bool:
    try:
        import asyncio
        return asyncio.iscoroutinefunction(fn)
    except Exception:
        return False

# ─── Exporters ────────────────────────────────────────────────────────────────�

def _export_span(span: Span) -> None:
    if _config["exporter"] == "null":
        return
    payload = span.to_dict()
    line = json.dumps(payload, default=str, ensure_ascii=True)
    lock = _config["lock"]
    with lock:
        if _config["exporter"] == "stdout":
            indent = 2 if _config.get("pretty") else None
            print(json.dumps(payload, default=str, indent=indent, ensure_ascii=False))
        elif _config["exporter"] == "jsonl":
            mode = "a"
            try:
                path = _config["output_file"]
                with open(path, mode, encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as exc:
                print(f"[tiny-tracer] failed to write to {_config['output_file']}: {exc}", file=sys.stderr)


# ─── Public API ───────────────────────────────────────────────────────────────

def configure(
    service_name: Optional[str] = None,
    exporter: Optional[str] = None,
    output_file: Optional[str] = None,
    sampler: Optional[str] = None,
    sample_ratio: Optional[float] = None,
    pretty: Optional[bool] = None,
) -> None:
    """
    Configure the global tracer.

    Args:
        service_name: Name of this service for span attribution.
        exporter: "stdout" (default) | "jsonl" | "null"
        output_file: Path for jsonl exporter. Default: "traces.jsonl"
        sampler: "always_on" | "always_off" | "trace_id_ratio"
        sample_ratio: 0.0–1.0, used when sampler="trace_id_ratio"
        pretty: Pretty-print JSON on stdout. Default: False.
    """
    if service_name is not None:
        _config["service_name"] = service_name
    if exporter is not None:
        _config["exporter"] = exporter
    if output_file is not None:
        _config["output_file"] = output_file
    if sampler is not None:
        _config["sampler"] = sampler
    if sample_ratio is not None:
        _config["sample_ratio"] = max(0.0, min(1.0, sample_ratio))
    if pretty is not None:
        _config["pretty"] = pretty


def spans() -> Iterator[Span]:
    """Iterate over all finished spans in the current process."""
    yield from _finished_spans


def clear() -> None:
    """Clear all in-memory spans (useful in tests)."""
    with _config["lock"]:
        _finished_spans.clear()
