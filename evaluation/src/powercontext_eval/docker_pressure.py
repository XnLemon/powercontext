"""Process-wide admission control for Docker control-plane operations."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

DOCKER_HEAVY_OPERATION_MAX_CONCURRENCY = 4
_DOCKER_HEAVY_OPERATION_SEMAPHORE = threading.BoundedSemaphore(DOCKER_HEAVY_OPERATION_MAX_CONCURRENCY)


@contextmanager
def heavy_operation() -> Iterator[None]:
    """Bound operations that hold Docker daemon streams or extract images."""

    with _DOCKER_HEAVY_OPERATION_SEMAPHORE:
        yield
