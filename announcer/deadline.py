"""Shared stream and two-phase subprocess deadline helpers."""

import queue
import subprocess
import time
from typing import Any, Callable, List, Optional


class TwoPhaseDeadline:
    """Wait for initial activity, then grant a fresh completion window."""

    def __init__(
        self,
        first_activity_deadline: Optional[float] = None,
        completion_timeout: Optional[float] = None,
        deadline: Optional[float] = None,
        first_timeout_message: str = "no activity",
        completion_timeout_message: str = "operation timed out",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.first_activity_deadline = first_activity_deadline
        self.completion_timeout = completion_timeout
        self.deadline = deadline
        self.first_timeout_message = first_timeout_message
        self.completion_timeout_message = completion_timeout_message
        self.clock = clock
        self.activity_seen = first_activity_deadline is None

    def active_deadline(self) -> float:
        deadline = (
            self.deadline if self.activity_seen else self.first_activity_deadline
        )
        if deadline is None:
            raise ValueError("a response deadline is required")
        return deadline

    def remaining(self) -> float:
        remaining = self.active_deadline() - self.clock()
        if remaining <= 0:
            self.raise_timeout()
        return remaining

    def raise_timeout(self) -> None:
        message = (
            self.completion_timeout_message
            if self.activity_seen
            else self.first_timeout_message
        )
        raise TimeoutError(message)

    def record_activity(self) -> None:
        if self.activity_seen:
            return
        self.activity_seen = True
        if self.completion_timeout is not None:
            self.deadline = self.clock() + self.completion_timeout

    def get(self, messages: Any) -> Any:
        try:
            return messages.get(timeout=self.remaining())
        except queue.Empty:
            self.raise_timeout()


def read_lines(stream: Any, messages: Any) -> None:
    try:
        for line in iter(stream.readline, ""):
            messages.put(line)
    finally:
        messages.put(None)


def drain_lines(stream: Any, lines: List[str], max_chars: int = 4000) -> None:
    size = 0
    for line in iter(stream.readline, ""):
        if size < max_chars:
            lines.append(line)
            size += len(line)


def stop_subprocess(
    process: Any,
    clean_exit: bool = False,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    if clean_exit and process.stdin is not None:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    if process.poll() is not None:
        return

    if deadline is None:
        deadline = clock() + 0.5
    if clean_exit:
        grace = min(1.0, max(0.0, deadline - clock()))
        if grace:
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                pass

    if clock() >= deadline:
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        process.terminate()
    except OSError:
        return
    termination_grace = max(0.0, min(0.5, deadline - clock()))
    if not termination_grace:
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        process.wait(timeout=termination_grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


__all__ = [
    "TwoPhaseDeadline",
    "drain_lines",
    "read_lines",
    "stop_subprocess",
]
