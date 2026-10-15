import asyncio
import base64
import binascii
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.requests import HTTPConnection

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_LIMIT = 256000
SERIAL_INPUT_LIMIT = 4096
CLASSIC_ESP32_BOARDS = ('esp32-devkit-v1', 'esp32-devkit-c-v4')


def fail(status, message):
    raise HTTPException(status, message)


class NativeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runtime_token: str = Field(min_length=1, max_length=200)
    expected_revision: StrictInt = Field(ge=1)
    source_revision: StrictInt = Field(ge=1)


class RunRequest(NativeRequest):
    artifact_id: str | None = Field(default=None, min_length=1, max_length=80)


class StopRequest(NativeRequest):
    run_id: str | None = Field(default=None, min_length=1, max_length=80)


class SerialRequest(StopRequest):
    data: str = Field(min_length=1, max_length=SERIAL_INPUT_LIMIT)


class GPIORequest(StopRequest):
    pin: StrictInt = Field(ge=0, le=39)
    level: StrictBool


def qemu_runtime():
    configured = os.getenv('WIREUP_QEMU_XTENSA_DLL')
    dll = Path(configured) if configured else ROOT / 'vendor/qemu-picsimlab/bin/libqemu-xtensa.dll'
    if not dll.is_file():
        if configured:
            fail(503, 'WIREUP_QEMU_XTENSA_DLL does not point to the PICSimLab QEMU library.')
        fail(503, 'Native ESP32 PICSimLab QEMU is unavailable. Build the pinned runtime in vendor/qemu-picsimlab.')
    rom = dll.parent.parent / 'share/qemu'
    required = ('esp32-v3-rom.bin', 'esp32-v3-rom-app.bin')
    if not all((rom / name).is_file() for name in required):
        fail(503, 'Native ESP32 PICSimLab QEMU ROM files are unavailable.')
    return str(dll.resolve()), str(rom.resolve())


def authorize(service, project_id, token, expected_revision, source_revision):
    project = service.get_project(project_id)
    service.authorize_runtime(project, token)
    if (type(expected_revision) is not int or type(source_revision) is not int
            or project['revision'] != expected_revision
            or project['firmware']['revision'] != source_revision):
        fail(409, 'Project or firmware revision changed. Reload the current project.')
    return project


def merged_flash(board, artifact):
    try:
        size = board['flash_size_bytes']
        if (artifact.get('format') != 'bin' or artifact.get('encoding') != 'base64'
                or artifact.get('image_kind') != 'merged-flash' or artifact.get('chip') != 'esp32'
                or artifact.get('load_address') != 0 or artifact.get('size_bytes') != size
                or artifact.get('flash_size_bytes') != size or not isinstance(artifact.get('bin'), str)
                or len(artifact['bin']) != (size + 2) // 3 * 4):
            raise ValueError
        binary = base64.b64decode(artifact['bin'], validate=True)
        if len(binary) != size:
            raise ValueError
        segments = artifact['flash_segments']
        expected = [('bootloader', 'sketch.ino.bootloader.bin', 0x1000),
                    ('partitions', 'sketch.ino.partitions.bin', 0x8000),
                    ('boot_app0', 'boot_app0.bin', 0xe000),
                    ('application', 'sketch.ino.bin', 0x10000)]
        if not isinstance(segments, list) or len(segments) != len(expected):
            raise ValueError
        for index, (name, filename, offset) in enumerate(expected):
            segment = segments[index]
            length = segment['size_bytes']
            end = expected[index + 1][2] if index + 1 < len(expected) else size
            if (segment.get('name') != name or segment.get('filename') != filename
                    or segment.get('offset') != offset or type(length) is not int
                    or not 0 < length <= end - offset):
                raise ValueError
            data = binary[offset:offset + length]
            if name in ('bootloader', 'application'):
                if len(data) < 24 or data[0] != 0xe9 or int.from_bytes(data[12:14], 'little') != 0:
                    raise ValueError
            if name == 'partitions' and not data.startswith(b'\xaa\x50'):
                raise ValueError
        return binary
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
        fail(409, 'No valid complete classic ESP32 merged flash image is available. Recompile the project.')


@dataclass
class Session:
    project_id: str
    board: str
    artifact_id: str
    project_revision: int
    source_revision: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    process: object = None
    directory: object = None
    task: object = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    serial: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    gpio: dict = field(default_factory=dict)
    serial_offset: int = 0
    stderr_offset: int = 0
    status: str = 'starting'
    error: str | None = None
    returncode: int | None = None


class NativeESP32:
    def __init__(self, *, lifetime=3600, startup_timeout=10, stop_timeout=5,
                 output_limit=OUTPUT_LIMIT, max_running=4, history_limit=32):
        self.lifetime = lifetime
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.output_limit = output_limit
        self.max_running = max_running
        self.history_limit = history_limit
        self.sessions = {}
        self.lock = asyncio.Lock()

    def active(self, project_id):
        session = self.sessions.get(project_id)
        return bool(session and session.directory is not None)

    def _append(self, session, name, data):
        buffer = getattr(session, name)
        buffer.extend(data)
        excess = max(0, len(buffer) - self.output_limit)
        if excess:
            del buffer[:excess]
            key = name + '_offset'
            setattr(session, key, getattr(session, key) + excess)

    async def _terminate(self, process):
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), self.stop_timeout)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), self.stop_timeout)

    async def _send(self, session, message):
        if session.process is None or session.process.returncode is not None:
            fail(409, 'Native ESP32 worker is not running.')
        data = (json.dumps(message, separators=(',', ':')) + '\n').encode('utf-8')
        async with session.write_lock:
            try:
                session.process.stdin.write(data)
                await asyncio.wait_for(session.process.stdin.drain(), self.stop_timeout)
            except (OSError, RuntimeError, BrokenPipeError):
                fail(502, 'Native ESP32 worker input is closed.')
            except TimeoutError:
                fail(504, 'Native ESP32 worker input timed out.')

    async def _events(self, session):
        try:
            while line := await session.process.stdout.readline():
                if len(line) > 65536:
                    raise ValueError('Native ESP32 worker event exceeded the safe limit.')
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    raise ValueError('Native ESP32 worker emitted an invalid event.')
                kind = event['type']
                if kind == 'ready':
                    session.ready.set()
                elif kind == 'uart':
                    if event.get('port') != 0 or not isinstance(event.get('data'), str):
                        continue
                    data = base64.b64decode(event['data'], validate=True)
                    self._append(session, 'serial', data)
                    if any(marker in session.serial for marker in (b'Guru Meditation', b'Cache disabled but cached memory region accessed')):
                        session.status = 'error'
                        session.error = 'The actual ESP32 guest firmware panicked during execution. See UART output.'
                        await self._terminate(session.process)
                        return
                elif kind in ('gpio', 'gpio_direction'):
                    pin = event.get('pin')
                    if type(pin) is not int or not 0 <= pin < 40:
                        raise ValueError('Native ESP32 worker emitted an invalid GPIO pin.')
                    state = session.gpio.setdefault(pin, {'level': None, 'output': False})
                    if kind == 'gpio':
                        if type(event.get('level')) is not bool:
                            raise ValueError('Native ESP32 worker emitted an invalid GPIO level.')
                        state['level'] = event['level']
                    else:
                        if type(event.get('output')) is not bool:
                            raise ValueError('Native ESP32 worker emitted an invalid GPIO direction.')
                        state['output'] = event['output']
                elif kind in ('fatal', 'command_error'):
                    message = event.get('message')
                    if not isinstance(message, str):
                        message = 'Native ESP32 worker failed.'
                    if kind == 'fatal':
                        session.status = 'error'
                        session.error = message[:1000]
                        session.ready.set()
                elif kind == 'exit':
                    return
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as error:
            session.status = 'error'
            session.error = str(error)[:1000]
            session.ready.set()
            await self._terminate(session.process)
        finally:
            session.ready.set()

    async def _drain_stderr(self, session):
        while chunk := await session.process.stderr.read(8192):
            self._append(session, 'stderr', chunk)

    async def _watch(self, session):
        readers = [asyncio.create_task(self._events(session)),
                   asyncio.create_task(self._drain_stderr(session))]
        try:
            async with asyncio.timeout(self.lifetime):
                session.returncode = await session.process.wait()
                await asyncio.gather(*readers)
            if session.status != 'error':
                session.status = 'stopped' if session.status == 'stopping' else 'exited'
            if session.returncode != 0 and session.status == 'exited':
                session.status = 'error'
                session.error = 'Native ESP32 worker exited with a nonzero return code.'
        except TimeoutError:
            session.status = 'timeout'
            session.error = 'Native ESP32 runtime exceeded its lifetime limit.'
        except asyncio.CancelledError:
            session.status = 'stopped'
            raise
        except (OSError, RuntimeError) as error:
            session.status = 'error'
            session.error = 'Native ESP32 worker monitoring failed: ' + str(error)[:500]
        finally:
            session.ready.set()
            try:
                await asyncio.shield(self._terminate(session.process))
            finally:
                session.returncode = session.process.returncode
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                session.directory.cleanup()
                session.directory = None

    def _snapshot(self, session, project, cursor=None):
        running = session.process is not None and session.process.returncode is None and session.status == 'running'
        offset = session.serial_offset
        end = offset + len(session.serial)
        requested = offset if cursor is None else cursor
        start = min(max(requested, offset), end) - offset
        gpio = {}
        for pin, state in sorted(session.gpio.items()):
            level = state['level']
            gpio[f'GPIO{pin}'] = {'level': level,
                                  'drive': ('high' if level else 'low') if state['output'] and level is not None else 'z'}
        return {'project_id': session.project_id, 'run_id': session.run_id,
                'board': session.board, 'chip': 'esp32', 'runtime': 'qemu-native',
                'status': session.status, 'running': running,
                'returncode': session.process.returncode if session.process else session.returncode,
                'artifact_id': session.artifact_id, 'project_revision': session.project_revision,
                'source_revision': session.source_revision,
                'stale': project['revision'] != session.project_revision or project['firmware']['revision'] != session.source_revision,
                'serial': bytes(session.serial[start:]).decode('utf-8', errors='replace'),
                'serial_encoding': 'utf-8', 'serial_offset': offset + start, 'serial_cursor': end,
                'serial_truncated': offset > 0 if cursor is None else requested < offset,
                'stderr': bytes(session.stderr).decode('utf-8', errors='replace'),
                'stderr_truncated': session.stderr_offset > 0, 'error': session.error,
                'gpio_available': True, 'gpio': gpio,
                'simulation_scope': 'Native classic ESP32 firmware execution with UART0 and sampled digital GPIO. ADC, I2C, SPI, RMT, LEDC and networking are not yet connected to canvas components.'}

    async def run(self, service, project_id, payload):
        from backend.board_flash import current_artifact
        async with self.lock:
            project = authorize(service, project_id, payload.runtime_token, payload.expected_revision, payload.source_revision)
            if project['board'] not in CLASSIC_ESP32_BOARDS:
                fail(400, 'Native QEMU runtime supports classic ESP32 boards only.')
            if self.active(project_id):
                fail(409, 'Native ESP32 is already running for this project. Stop it before restarting.')
            if sum(self.active(key) for key in self.sessions) >= self.max_running:
                fail(429, 'Native ESP32 runtime concurrency limit reached.')
            project, board, artifact = current_artifact(service, project_id, payload)
            if payload.artifact_id is not None and payload.artifact_id != artifact['id']:
                fail(409, 'Only the current successfully compiled artifact can run.')
            binary = merged_flash(board, artifact)
            dll, rom = qemu_runtime()
            directory = tempfile.TemporaryDirectory(prefix='wireup-esp32-')
            session = Session(project_id, project['board'], artifact['id'], project['revision'],
                              project['firmware']['revision'], directory=directory)
            self.sessions[project_id] = session
            flash = Path(directory.name) / 'flash.bin'
            try:
                flash.write_bytes(binary)
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'backend.native_esp32_worker', '--dll', dll,
                    '--rom', rom, '--flash', str(flash.resolve()), cwd=str(ROOT),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ))
                try:
                    session.process = await asyncio.wait_for(asyncio.shield(spawn), self.startup_timeout)
                except (TimeoutError, asyncio.CancelledError):
                    async def reap_launch():
                        try:
                            session.process = await asyncio.wait_for(spawn, self.startup_timeout)
                            await self._terminate(session.process)
                            session.returncode = session.process.returncode
                        except (TimeoutError, OSError):
                            session.error = 'Native ESP32 worker launch cleanup timed out or failed.'
                        finally:
                            directory.cleanup()
                            session.directory = None
                            session.status = 'error' if session.error else 'stopped'
                    session.status = 'stopping'
                    session.task = asyncio.create_task(reap_launch())
                    raise
                session.task = asyncio.create_task(self._watch(session))
                await asyncio.wait_for(session.ready.wait(), self.startup_timeout)
                if session.status == 'error' or session.process.returncode is not None:
                    fail(502, session.error or 'Native ESP32 worker exited during startup.')
                authorize(service, project_id, payload.runtime_token, payload.expected_revision, payload.source_revision)
                _, _, latest_artifact = current_artifact(service, project_id, payload)
                if latest_artifact['id'] != session.artifact_id:
                    fail(409, 'Compiled artifact changed while QEMU was starting.')
                session.status = 'running'
                return self._snapshot(session, service.get_project(project_id))
            except BaseException as error:
                if session.process is None and session.task is None:
                    directory.cleanup()
                    session.directory = None
                    session.status = 'error'
                elif session.status in ('starting', 'running'):
                    session.status = 'stopping'
                    await asyncio.shield(self._stop_session(session))
                if isinstance(error, TimeoutError):
                    fail(504, 'Native ESP32 QEMU startup timed out.')
                if isinstance(error, OSError):
                    fail(503, 'Native ESP32 worker could not be started.')
                raise
            finally:
                completed = [key for key in self.sessions if not self.active(key)]
                for key in completed[:-self.history_limit]:
                    self.sessions.pop(key, None)

    async def _stop_session(self, session):
        if session.process is not None and session.process.returncode is None:
            try:
                await self._send(session, {'type': 'stop'})
                if session.task is not None:
                    await asyncio.wait_for(asyncio.shield(session.task), self.stop_timeout)
            except (HTTPException, TimeoutError):
                await self._terminate(session.process)
        if session.task is not None and not session.task.done():
            await asyncio.shield(session.task)

    def _session(self, project_id, run_id=None):
        session = self.sessions.get(project_id)
        if session is None:
            fail(404, 'No native ESP32 run exists for this project.')
        if run_id is not None and run_id != session.run_id:
            fail(409, 'Native ESP32 run changed. Reload runtime results.')
        return session

    async def stop(self, service, project_id, payload):
        async with self.lock:
            project = authorize(service, project_id, payload.runtime_token, payload.expected_revision, payload.source_revision)
            session = self._session(project_id, payload.run_id)
            if self.active(project_id):
                session.status = 'stopping'
                await asyncio.shield(self._stop_session(session))
            return self._snapshot(session, project)

    async def results(self, service, project_id, token, expected_revision, source_revision, cursor=None, run_id=None):
        async with self.lock:
            project = authorize(service, project_id, token, expected_revision, source_revision)
            session = self._session(project_id, run_id)
            return self._snapshot(session, project, cursor)

    async def serial(self, service, project_id, payload):
        try:
            data = payload.data.encode('utf-8')
        except UnicodeError:
            fail(400, 'UART input must be valid UTF-8 text.')
        if len(data) > SERIAL_INPUT_LIMIT:
            fail(400, 'UART input must not exceed 4096 UTF-8 bytes.')
        async with self.lock:
            project = authorize(service, project_id, payload.runtime_token, payload.expected_revision, payload.source_revision)
            session = self._session(project_id, payload.run_id)
            self._require_current_run(project_id, project, session)
            await self._send(session, {'type': 'uart_input', 'port': 0,
                                       'data': base64.b64encode(data).decode('ascii')})
            return {'project_id': project_id, 'run_id': session.run_id,
                    'bytes_written': len(data), 'gpio_available': True}

    async def gpio_input(self, service, project_id, payload):
        async with self.lock:
            project = authorize(service, project_id, payload.runtime_token, payload.expected_revision, payload.source_revision)
            session = self._session(project_id, payload.run_id)
            self._require_current_run(project_id, project, session)
            await self._send(session, {'type': 'gpio_input', 'pin': payload.pin, 'level': payload.level})
            return {'project_id': project_id, 'run_id': session.run_id,
                    'pin': payload.pin, 'level': payload.level, 'gpio_available': True}

    def _require_current_run(self, project_id, project, session):
        if (not self.active(project_id) or session.status != 'running'
                or session.process is None or session.process.returncode is not None):
            fail(409, 'Native ESP32 is not running.')
        if project['revision'] != session.project_revision or project['firmware']['revision'] != session.source_revision:
            fail(409, 'Native ESP32 run is stale. Stop and recompile before sending input.')

    async def close(self):
        async with self.lock:
            for session in list(self.sessions.values()):
                if self.active(session.project_id):
                    session.status = 'stopping'
                await asyncio.shield(self._stop_session(session))


def create_router(service, *, prefix=''):
    from backend.board_flash import until_disconnect
    from backend.hardware import local_connection

    async def local_only(connection: HTTPConnection, response: Response):
        if not local_connection(connection):
            fail(403, 'Native ESP32 API is local-only.')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'

    api = APIRouter(prefix=prefix, tags=['hardware'], dependencies=[Depends(local_only)])

    @api.post('/projects/{project_id}/native/run')
    async def run(project_id: str, payload: RunRequest, request: Request):
        return await until_disconnect(request, service.native.run(service, project_id, payload))

    @api.post('/projects/{project_id}/native/stop')
    async def stop(project_id: str, payload: StopRequest, request: Request):
        return await until_disconnect(request, service.native.stop(service, project_id, payload))

    @api.get('/projects/{project_id}/native/results')
    @api.get('/projects/{project_id}/native/serial')
    async def results(project_id: str, request: Request, expected_revision: int, source_revision: int,
                      cursor: int | None = None, run_id: str | None = None):
        if expected_revision < 1 or source_revision < 1 or (cursor is not None and cursor < 0):
            fail(400, 'Revisions must be positive and the UART cursor nonnegative.')
        return await service.native.results(service, project_id, request.headers.get('X-Runtime-Token'),
                                            expected_revision, source_revision, cursor, run_id)

    @api.post('/projects/{project_id}/native/serial')
    async def serial(project_id: str, payload: SerialRequest, request: Request):
        return await until_disconnect(request, service.native.serial(service, project_id, payload))

    @api.post('/projects/{project_id}/native/gpio')
    async def gpio(project_id: str, payload: GPIORequest, request: Request):
        return await until_disconnect(request, service.native.gpio_input(service, project_id, payload))

    api.add_event_handler('shutdown', service.native.close)
    return api
