import asyncio
import base64
import json

from backend.native_esp32 import NativeESP32, Session


def test_actual_guest_panic_event_is_reported_and_worker_stopped():
    async def run():
        class Process:
            def __init__(self):
                self.returncode = None
                self.stdout = asyncio.StreamReader()
                self.finished = asyncio.Event()

            def terminate(self):
                self.returncode = -15
                self.finished.set()

            def kill(self):
                self.returncode = -9
                self.finished.set()

            async def wait(self):
                await self.finished.wait()
                return self.returncode

        runtime = NativeESP32(stop_timeout=0.1)
        session = Session('project', 'esp32-devkit-v1', 'artifact', 1, 1)
        session.process = Process()
        data = base64.b64encode(b'Guru Meditation Error: Cache error\n').decode()
        session.process.stdout.feed_data((json.dumps({'type': 'uart', 'port': 0, 'data': data}) + '\n').encode())
        session.process.stdout.feed_eof()
        await runtime._events(session)
        assert session.status == 'error'
        assert 'panicked' in session.error
        assert session.process.returncode is not None
        assert b'Guru Meditation' in session.serial

    asyncio.run(run())
