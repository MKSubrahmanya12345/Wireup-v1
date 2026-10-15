import argparse
import base64
import ctypes
import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

SHUTDOWN_CAUSE_HOST_SIGNAL = 4
GPIO_COUNT = 40


WriteCallback = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int)
I2CCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16)
SPICallback = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16)
UARTCallback = ctypes.CFUNCTYPE(None, ctypes.c_uint8, ctypes.c_uint8)
RMTCallback = ctypes.CFUNCTYPE(None, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32)


class Callbacks(ctypes.Structure):
    _fields_ = [
        ('write_pin', WriteCallback),
        ('dir_pin', WriteCallback),
        ('i2c_event', I2CCallback),
        ('spi_event', SPICallback),
        ('uart_tx_event', UARTCallback),
        ('pinmap', ctypes.POINTER(ctypes.c_short)),
        ('rmt_event', RMTCallback),
    ]


class Worker:
    def __init__(self, dll_path: Path, rom_path: Path, flash_path: Path):
        self.dll_path = dll_path
        self.rom_path = rom_path
        self.flash_path = flash_path
        self.events = queue.Queue()
        self.writer_done = threading.Event()
        self.control_done = threading.Event()
        self.dll = ctypes.CDLL(str(dll_path))
        self._bind()
        self._retain_callbacks()

    def _bind(self):
        self.dll.qemu_init.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
        self.dll.qemu_init.restype = None
        self.dll.qemu_main_loop.argtypes = []
        self.dll.qemu_main_loop.restype = ctypes.c_int
        self.dll.qemu_cleanup.argtypes = [ctypes.c_int]
        self.dll.qemu_cleanup.restype = None
        self.dll.qemu_system_shutdown_request.argtypes = [ctypes.c_int]
        self.dll.qemu_system_shutdown_request.restype = None
        self.dll.qemu_picsimlab_register_callbacks.argtypes = [ctypes.c_void_p]
        self.dll.qemu_picsimlab_register_callbacks.restype = None
        self.dll.qemu_picsimlab_set_pin.argtypes = [ctypes.c_int, ctypes.c_int]
        self.dll.qemu_picsimlab_set_pin.restype = None
        self.dll.qemu_picsimlab_uart_receive.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
        self.dll.qemu_picsimlab_uart_receive.restype = None

    def _retain_callbacks(self):
        @WriteCallback
        def write_pin(index, value):
            self.events.put({'type': 'gpio', 'pin': index - 1, 'level': bool(value)})

        @WriteCallback
        def dir_pin(index, value):
            if index > 0:
                self.events.put({'type': 'gpio_direction', 'pin': index - 1, 'output': bool(value)})

        @I2CCallback
        def i2c_event(_bus, _address, _event):
            return 0

        @SPICallback
        def spi_event(_bus, _event):
            return 0

        @UARTCallback
        def uart_event(port, value):
            self.events.put(('uart', int(port), int(value)))

        @RMTCallback
        def rmt_event(_channel, _config, _value):
            return None

        self.pinmap = (ctypes.c_short * (GPIO_COUNT + 1))(GPIO_COUNT, *range(GPIO_COUNT))
        self.callback_refs = (write_pin, dir_pin, i2c_event, spi_event, uart_event, rmt_event)
        self.callbacks = Callbacks(write_pin, dir_pin, i2c_event, spi_event, uart_event, self.pinmap, rmt_event)
        self.dll.qemu_picsimlab_register_callbacks(ctypes.byref(self.callbacks))

    @staticmethod
    def _write_event(event):
        sys.stdout.write(json.dumps(event, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    def _writer(self):
        uart = bytearray()
        uart_port = 0

        def flush_uart():
            nonlocal uart
            if uart:
                self._write_event({'type': 'uart', 'port': uart_port,
                                   'data': base64.b64encode(uart).decode('ascii')})
                uart = bytearray()

        while not self.writer_done.is_set() or not self.events.empty():
            try:
                event = self.events.get(timeout=0.02)
            except queue.Empty:
                flush_uart()
                continue
            if isinstance(event, tuple) and event[0] == 'uart':
                _, port, value = event
                if uart and port != uart_port:
                    flush_uart()
                uart_port = port
                uart.append(value)
                if len(uart) >= 1024:
                    flush_uart()
            else:
                flush_uart()
                self._write_event(event)
        flush_uart()

    def _control(self):
        try:
            for line in sys.stdin:
                if self.control_done.is_set():
                    break
                try:
                    command = json.loads(line)
                    if not isinstance(command, dict):
                        raise ValueError('Worker command must be an object.')
                    kind = command.get('type')
                    if kind == 'stop':
                        self.dll.qemu_system_shutdown_request(SHUTDOWN_CAUSE_HOST_SIGNAL)
                        return
                    if kind == 'gpio_input':
                        pin = command.get('pin')
                        level = command.get('level')
                        if type(pin) is not int or not 0 <= pin < GPIO_COUNT or type(level) is not bool:
                            raise ValueError('Invalid GPIO input command.')
                        self.dll.qemu_picsimlab_set_pin(pin + 1, int(level))
                    elif kind == 'uart_input':
                        port = command.get('port', 0)
                        encoded = command.get('data')
                        if type(port) is not int or not 0 <= port <= 2 or not isinstance(encoded, str):
                            raise ValueError('Invalid UART input command.')
                        data = base64.b64decode(encoded, validate=True)
                        if not data or len(data) > 4096:
                            raise ValueError('UART input must contain 1 to 4096 bytes.')
                        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
                        self.dll.qemu_picsimlab_uart_receive(port, buffer, len(data))
                    elif kind != 'gpio_input':
                        raise ValueError('Unknown worker command.')
                except Exception as error:
                    self.events.put({'type': 'command_error', 'message': str(error)[:500]})
        finally:
            if not self.control_done.is_set():
                self.dll.qemu_system_shutdown_request(SHUTDOWN_CAUSE_HOST_SIGNAL)

    def run(self):
        writer = threading.Thread(target=self._writer, name='esp32-event-writer')
        control = threading.Thread(target=self._control, name='esp32-control-reader', daemon=True)
        writer.start()
        status = 1
        initialized = False
        try:
            args = [
                'wireup-qemu', '-machine', 'esp32-picsimlab', '-display', 'none',
                '-serial', 'null', '-serial', 'null', '-serial', 'null', '-monitor', 'none',
                '-nic', 'none', '-drive', f'file={self.flash_path.name},if=mtd,format=raw',
                '-L', str(self.rom_path),
            ]
            encoded = [value.encode('utf-8') for value in args]
            argv = (ctypes.c_char_p * len(encoded))(*encoded)
            os.chdir(self.flash_path.parent)
            self.dll.qemu_init(len(encoded), argv)
            initialized = True
            self.events.put({'type': 'ready', 'gpio_available': True})
            control.start()
            status = self.dll.qemu_main_loop()
            self.events.put({'type': 'exit', 'returncode': status})
            return status
        except BaseException as error:
            self.events.put({'type': 'fatal', 'message': str(error)[:1000],
                             'traceback': traceback.format_exc(limit=8)[:4000]})
            return 1
        finally:
            self.control_done.set()
            if initialized:
                try:
                    self.dll.qemu_cleanup(status)
                except BaseException as error:
                    self.events.put({'type': 'fatal', 'message': 'QEMU cleanup failed: ' + str(error)[:500]})
            self.writer_done.set()
            writer.join(timeout=5)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dll', required=True, type=Path)
    parser.add_argument('--rom', required=True, type=Path)
    parser.add_argument('--flash', required=True, type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.dll.is_file() or not args.rom.is_dir() or not args.flash.is_file():
        print(json.dumps({'type': 'fatal', 'message': 'Required emulator file is missing.'}), flush=True)
        return 2
    return Worker(args.dll.resolve(), args.rom.resolve(), args.flash.resolve()).run()


if __name__ == '__main__':
    raise SystemExit(main())
