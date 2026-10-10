"""OpenTelemetry stub. Call sites use `span(...)` now; wiring a real exporter later changes only this module."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """No-op span. Replace the body with `tracer.start_as_current_span(name, attributes=attributes)`."""
    yield
