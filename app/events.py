"""Thread-safe fan-out of live events to WebSocket clients."""

import asyncio
import threading


class EventHub:
    def __init__(self):
        self._loop = None
        self._queues = set()
        self._lock = threading.Lock()

    def bind_loop(self, loop):
        self._loop = loop

    def subscribe(self):
        queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            self._queues.discard(queue)

    def _deliver(self, event):
        with self._lock:
            queues = list(self._queues)
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow client: drop the event, the dashboard resyncs by polling.
                pass

    def publish(self, event):
        """Publish from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            pass
