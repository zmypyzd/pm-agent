"""Tests for pm_agent.tui.TUILogHandler — thread-safe log → UI bridge."""
from __future__ import annotations

import asyncio
import collections
import logging
import threading

import pytest


class _FakeApp:
    def __init__(self):
        self.screen_stack = []
        self.appended: list[str] = []


@pytest.mark.asyncio
async def test_emit_from_main_thread_appends_to_buffer():
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=10)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    rec = logging.LogRecord("x", logging.INFO, "f", 0, "hello", None, None)
    h.emit(rec)
    await asyncio.sleep(0)  # let call_soon run

    assert list(buf) == ["hello"]


@pytest.mark.asyncio
async def test_emit_from_worker_thread_is_safe():
    """Repeated emits from a worker thread must not crash or drop messages."""
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=200)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    def worker():
        for i in range(100):
            rec = logging.LogRecord("x", logging.INFO, "f", 0,
                                    f"msg-{i}", None, None)
            h.emit(rec)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    # Drain all scheduled call_soon callbacks
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert len(buf) == 100
    assert buf[0] == "msg-0"
    assert buf[-1] == "msg-99"


@pytest.mark.asyncio
async def test_emit_drops_to_widget_when_daemon_screen_current():
    from pm_agent.tui import TUILogHandler, DaemonScreen
    buf = collections.deque(maxlen=10)
    app = _FakeApp()

    class _Stub(DaemonScreen):
        def __init__(self):
            self.appended: list[str] = []
        def append_log_line(self, msg):
            self.appended.append(msg)
    screen = _Stub()
    app.screen_stack.append(screen)

    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("x", logging.INFO, "f", 0, "hi", None, None)
    h.emit(rec)
    await asyncio.sleep(0)

    assert list(buf) == ["hi"]
    assert screen.appended == ["hi"]


@pytest.mark.asyncio
async def test_buffer_maxlen_enforced():
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=3)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    for i in range(5):
        rec = logging.LogRecord("x", logging.INFO, "f", 0, str(i), None, None)
        h.emit(rec)
    await asyncio.sleep(0)
    assert list(buf) == ["2", "3", "4"]
