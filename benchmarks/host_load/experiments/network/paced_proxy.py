"""Loopback TCP byte pacing, not netem: TLS stays end-to-end and unmodified.

Models one shared serialization bottleneck per direction plus propagation delay.
It does not model TCP congestion control, packet loss, retransmission, or real WAN
socket buffering. Optional stalls are a sensitivity test, not packet loss.
"""
import asyncio
import random
import threading
import time


class PacedProxy:
    def __init__(self, target_port, *, mbps=5.0, rtt_ms=100.0, stall_probability=0.0,
                 stall_ms=200.0, seed=1):
        self.target_port = target_port
        self.rate = mbps * 1_000_000 / 8
        self.delay = rtt_ms / 2000
        self.stall_probability = stall_probability
        self.stall_s = stall_ms / 1000
        self.random = random.Random(seed)
        self.next_free = [0.0, 0.0]
        self.byte_counts = [0, 0]
        self.stalls = [0, 0]
        self.loop = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.writers = set()
        self.tasks = set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError('proxy startup timed out')
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(
            asyncio.start_server(self._accept, '127.0.0.1', 0))
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    async def _accept(self, client_reader, client_writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        host_writer = None
        try:
            host_reader, host_writer = await asyncio.open_connection('127.0.0.1', self.target_port)
            self.writers.update((client_writer, host_writer))
            await asyncio.gather(self._pump(client_reader, host_writer, 0),
                                 self._pump(host_reader, client_writer, 1))
        except (ConnectionError, OSError):
            pass
        finally:
            self.tasks.discard(task)
            for writer in (client_writer, host_writer):
                if writer is not None:
                    self.writers.discard(writer)
                    writer.close()

    async def _pump(self, reader, writer, direction):
        # Queue allows propagation delay to pipeline rather than charging it
        # again serially for every chunk. Bounded queue imposes backpressure.
        queue = asyncio.Queue(maxsize=128)
        async def receive():
            while True:
                data = await reader.read(4096)
                if not data:
                    await queue.put(None)
                    return
                now = time.monotonic()
                done = max(now, self.next_free[direction]) + len(data) / self.rate
                if self.random.random() < self.stall_probability:
                    done += self.stall_s
                    self.stalls[direction] += 1
                self.next_free[direction] = done
                await queue.put((done + self.delay, data))
        async def deliver():
            while True:
                item = await queue.get()
                if item is None:
                    if writer.can_write_eof():
                        writer.write_eof()
                    return
                deadline, data = item
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                writer.write(data)
                await writer.drain()
                self.byte_counts[direction] += len(data)
        await asyncio.gather(receive(), deliver())

    def __exit__(self, *exc):
        async def close():
            self.server.close()
            await self.server.wait_closed()
            for writer in list(self.writers):
                writer.close()
            for task in list(self.tasks):
                task.cancel()
            if self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
        asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
