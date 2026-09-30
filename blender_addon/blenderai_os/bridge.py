"""WebSocket client that runs beside Blender's UI thread.

Blender executes operators and handlers on the main thread. This bridge owns
a daemon thread with its own asyncio loop, queues inbound envelopes, and
lets the main thread send results and user events back through the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from typing import Any

import websockets

from protocol.messages import MAX_FRAME_BYTES, dumps, hello, loads

_CAPS = ["ping", "get_scene", "create_mesh", "render_preview"]


class Bridge:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._stop.set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_outbox: asyncio.Queue[dict[str, Any]] | None = None
        self._ws: Any = None
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._uri = "ws://127.0.0.1:8765"
        self._ready = False
        self.session_id = ""

    @property
    def ready(self) -> bool:
        return self._ready

    def start(self, uri: str) -> None:
        if self._thread is not None and self._thread.is_alive():
            print("[BlenderAI-OS] bridge already running")
            return
        self._uri = uri
        self._ready = False
        self.session_id = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="blenderai-ws", daemon=True)
        self._thread.start()
        self._put_inbox({"type": "local_status", "state": "connecting", "detail": uri})

    def stop(self) -> None:
        self._stop.set()
        self._ready = False
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._request_close)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def send(self, msg: dict[str, Any]) -> None:
        loop = self._loop
        outbox = self._async_outbox
        if loop is None or outbox is None or not self._ready:
            return
        if outbox.qsize() > 100 and msg.get("type") == "event":
            return

        def _enqueue() -> None:
            if self._async_outbox is not None:
                self._async_outbox.put_nowait(msg)

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            return

    def poll_inbox(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return items

    def _request_close(self) -> None:
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())

    def _put_inbox(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "command" and self._inbox.qsize() > 200:
            return
        self._inbox.put(msg)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        finally:
            self._loop.close()
            self._loop = None
            self._async_outbox = None

    async def _async_main(self) -> None:
        self._async_outbox = asyncio.Queue()
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._uri,
                    open_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=MAX_FRAME_BYTES,
                ) as ws:
                    self._ws = ws
                    self._ready = False
                    await ws.send(dumps(hello("blender", "blenderai-os", capabilities=_CAPS)))
                    pump = asyncio.create_task(self._pump(ws))
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                break
                            try:
                                msg = loads(raw)
                            except ValueError as exc:
                                print(f"[BlenderAI-OS] bad message: {exc}")
                                continue
                            if msg.get("type") == "hello_ack":
                                self._ready = True
                                self.session_id = str(msg.get("session_id") or "")
                            self._put_inbox(msg)
                    finally:
                        self._ready = False
                        self._ws = None
                        pump.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pump
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ready = False
                self._ws = None
                self._put_inbox({"type": "local_status", "state": "retry", "detail": str(exc)})
                print(f"[BlenderAI-OS] connect failed: {exc}")
            if self._stop.is_set():
                break
            await asyncio.sleep(2)
        self._put_inbox({"type": "local_status", "state": "disconnected", "detail": ""})

    async def _pump(self, ws: Any) -> None:
        assert self._async_outbox is not None
        while True:
            msg = await self._async_outbox.get()
            await ws.send(dumps(msg))
