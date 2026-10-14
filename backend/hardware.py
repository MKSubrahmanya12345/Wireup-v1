import ast
import base64
import ipaddress
import asyncio
import copy
import hmac
import json
import math
import operator
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = '''void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);
  delay(500);
}
'''
TOOLS = (
    'read_project', 'search_components', 'add_component', 'remove_component',
    'modify_component', 'connect_wire', 'remove_wire', 'check_connections', 'generate_firmware',
    'read_firmware', 'edit_firmware', 'compile_firmware', 'run_simulation',
    'stop_simulation', 'read_simulation_results', 'read_compiler_errors', 'calculator',
)
BOARDS = [
    {'id': 'arduino-uno', 'name': 'Arduino Uno', 'fqbn': 'arduino:avr:uno',
     'compile': True, 'simulation': 'browser', 'format': 'hex', 'runtime': 'avr8js', 'tagName': 'wokwi-arduino-uno'},
    {'id': 'arduino-nano', 'name': 'Arduino Nano (ATmega328P)', 'fqbn': 'arduino:avr:nano:cpu=atmega328',
     'compile': True, 'simulation': 'browser', 'format': 'hex', 'runtime': 'avr8js', 'tagName': 'wokwi-arduino-nano'},
    {'id': 'arduino-mega', 'name': 'Arduino Mega 2560', 'fqbn': 'arduino:avr:mega:cpu=atmega2560',
     'compile': True, 'simulation': 'browser', 'format': 'hex', 'runtime': 'avr8js-mega', 'tagName': 'wokwi-arduino-mega'},
    {'id': 'pi-pico', 'name': 'Raspberry Pi Pico', 'fqbn': 'rp2040:rp2040:rpipico',
     'compile': True, 'simulation': 'browser', 'format': 'bin', 'runtime': 'rp2040js', 'tagName': 'wokwi-pi-pico',
     'compile_timeout_seconds': 300},
]
BOARDS.extend([
    {'id': 'pi-pico-w', 'name': 'Raspberry Pi Pico W', 'fqbn': 'rp2040:rp2040:rpipicow',
     'compile': True, 'simulation': 'browser', 'format': 'bin', 'runtime': 'rp2040js',
     'simulation_scope': 'RP2040 CPU, external GPIO, UART0; no CYW43 radio or onboard LED',
     'tagName': 'wokwi-pi-pico-w', 'compile_timeout_seconds': 300, 'wifi': False, 'bluetooth': False,
     'unavailable_reason': 'Pico W CPU and external GPIO/UART0 are supported; CYW43 WiFi/Bluetooth and its onboard LED are not emulated.'},
])
for board_id, name, target, chip, bootloader_offset in [
    ('esp32-devkit-v1', 'ESP32 DevKit V1', 'esp32doit-devkit-v1', 'esp32', 0x1000),
    ('esp32-devkit-c-v4', 'ESP32 DevKit C V4', 'esp32', 'esp32', 0x1000),
    ('esp32-s3', 'ESP32-S3 DevKitC-1', 'esp32s3', 'esp32s3', 0),
    ('esp32-c3', 'ESP32-C3 DevKitM-1', 'esp32c3', 'esp32c3', 0),
]:
    classic = chip == 'esp32'
    BOARDS.append({'id': board_id, 'name': name, 'fqbn': f'esp32:esp32:{target}',
                   'compile': True, 'simulation': 'native' if classic else 'browser', 'format': 'bin',
                   'runtime': 'qemu-native' if classic else 'esp-emu-wasm',
                   'tagName': 'wokwi-' + board_id, 'chip': chip, 'bootloader_offset': bootloader_offset,
                   'flash_size_bytes': 4 * 1024 * 1024, 'compile_timeout_seconds': 600,
                   'wifi': False, 'bluetooth': False,
                   'simulation_scope': 'Native classic ESP32 CPU and UART0; GPIO, connected components, sensor buses and networking are not bridged.' if classic else 'Official Espressif WASM CPU, UART0 and sampled external GPIO; no connected sensor buses or real networking',
                   'unavailable_reason': None})
for model in (3, 4, 5):
    BOARDS.append({'id': f'raspberry-pi-{model}', 'name': f'Raspberry Pi {model}', 'fqbn': None,
                   'compile': False, 'simulation': 'unavailable', 'format': None, 'runtime': 'qemu-linux',
                   'tagName': f'velxio-raspberry-pi-{model}',
                   'unavailable_reason': 'Raspberry Pi Linux boards require a native runtime and guest Linux image; Arduino firmware compilation is not supported.'})
BOARD_CONFIG = {board['id']: board for board in BOARDS}
BOARD_ALIASES = {'raspberry-pi-pico': 'pi-pico', 'raspberry-pi-pico-w': 'pi-pico-w',
                 'esp32': 'esp32-devkit-v1'}
PIN_LAYOUTS = {
    'arduino-uno': [str(i) for i in range(14)] + [f'A{i}' for i in range(6)]
                   + ['GND.1', 'GND.2', 'GND.3', '5V', '3.3V', 'VIN', 'AREF', 'RESET', 'IOREF', 'A4.2', 'A5.2'],
    'arduino-nano': [str(i) for i in range(14)] + [f'A{i}' for i in range(8)]
                    + ['3.3V', 'AREF', '5V', 'VIN', 'RESET', 'RESET.2', 'RESET.3',
                       'GND.1', 'GND.2', 'GND.3', '12.2', '13.2', '11.2', '5V.2'],
    'arduino-mega': [str(i) for i in range(54)] + [f'A{i}' for i in range(16)]
                    + ['SCL', 'SDA', 'AREF', 'IOREF', 'RESET', '3.3V', '5V', '5V.1', '5V.2', 'VIN']
                    + [f'GND.{i}' for i in range(1, 6)],
    'pi-pico': [f'GP{i}' for i in list(range(23)) + [26, 27, 28]]
               + [f'GND.{i}' for i in range(1, 9)]
               + ['VBUS', 'VSYS', '3V3_EN', '3V3', 'ADC_VREF', 'RUN'],
    'source-dc-voltage': ['+', '-'], 'source-dc-current': ['+', '-'],
    'source-sine-voltage': ['+', '-'], 'source-pulse-voltage': ['+', '-'],
    'source-pwl-voltage': ['+', '-'], 'source-ac-voltage': ['+', '-'], 'ground': ['GND'],
    'led': ['A', 'C'], 'resistor': ['1', '2'], 'capacitor': ['1', '2'], 'inductor': ['1', '2'],
    'pushbutton': ['1.l', '1.r', '2.l', '2.r'], 'potentiometer': ['GND', 'SIG', 'VCC'],
    'buzzer': ['1', '2'], 'servo': ['GND', 'V+', 'PWM'],
    'dht22': ['VCC', 'SDA', 'NC', 'GND'], 'hc-sr04': ['VCC', 'TRIG', 'ECHO', 'GND'],
    'rgb-led': ['R', 'G', 'B', 'COM'], 'neopixel': ['VDD', 'VSS', 'DIN', 'DOUT'],
}
# Pin names match the installed Wokwi DevKit V1 element and vendored Esp32Element wrappers.
PIN_LAYOUTS['slide-potentiometer'] = ['VCC', 'SIG', 'GND']
PIN_LAYOUTS['ssd1306'] = ['DATA', 'CLK', 'DC', 'RST', 'CS', '3V3', 'VIN', 'GND']
PIN_LAYOUTS['ssd1306-i2c-4pin'] = ['GND', 'VCC', 'SCL', 'SDA']
PIN_LAYOUTS['lcd1602-i2c'] = ['GND', 'VCC', 'SDA', 'SCL']
PIN_LAYOUTS['lcd2004-i2c'] = ['GND', 'VCC', 'SDA', 'SCL']
PIN_LAYOUTS['ntc-temperature-sensor'] = ['GND', 'VCC', 'OUT']
PIN_LAYOUTS['photoresistor-sensor'] = ['VCC', 'GND', 'DO', 'AO']
PIN_LAYOUTS['pir-motion-sensor'] = ['VCC', 'OUT', 'GND']
PIN_LAYOUTS['mpu6050'] = ['INT', 'AD0', 'XCL', 'XDA', 'SDA', 'SCL', 'GND', 'VCC']
PIN_LAYOUTS['ds1307'] = ['GND', '5V', 'SDA', 'SCL', 'SQW']
PIN_LAYOUTS['ds3231'] = ['GND', 'VCC', 'SDA', 'SCL']
PIN_LAYOUTS['bmp280'] = ['SDA', 'SCL', 'GND', 'VCC']
PIN_LAYOUTS.update({
    'esp32-devkit-v1': ['VIN', 'GND.2', 'D13', 'D12', 'D14', 'D27', 'D26', 'D25', 'D33', 'D32',
                         'D35', 'D34', 'VN', 'VP', 'EN', '3V3', 'GND.1', 'D15', 'D2', 'D4',
                         'RX2', 'TX2', 'D5', 'D18', 'D19', 'D21', 'RX0', 'TX0', 'D22', 'D23'],
    'esp32-devkit-c-v4': ['3V3', 'EN', 'VP', 'VN', '34', '35', '32', '33', '25', '26', '27',
                           '14', '12', 'GND.1', '13', 'D2', 'D3', 'CMD', '5V', 'GND.2', '23',
                           '22', 'TX', 'RX', '21', 'GND.3', '19', '18', '5', '17', '16', '4',
                           '0', '2', '15', 'D1', 'D0', 'CLK'],
    'esp32-s3': ['3V3.1', '3V3.2', 'RST', '4', '5', '6', '7', '15', '16', '17', '18', '8',
                  '3', '46', '9', '10', '11', '12', '13', '14', '5V', 'GND.1', 'GND.2',
                  'TX', 'RX', '1', '2', '42', '41', '40', '39', '38', '37', '36', '35',
                  '0', '45', '48', '47', '21', '20', '19', 'GND.3', 'GND.4'],
    'esp32-c3': ['GND.1', '3V3.1', '3V3.2', '2', '3', 'GND.2', 'RST', 'GND.3', '0', '1',
                  '10', 'GND.4', '5V.1', '5V.2', 'GND.5', 'GND.6', '19', '18', 'GND.7',
                  '4', '5', '6', '7', 'GND.8', '8', '9', 'GND.9', 'RX', 'TX', 'GND.10'],
    'pi-pico-w': PIN_LAYOUTS['pi-pico'][:],
})
# Linux Pi wrappers share BCM header names, including repeated power/ground labels.
for model in (3, 4, 5):
    PIN_LAYOUTS[f'raspberry-pi-{model}'] = ['3V3', '5V', 'GND', 'ID_SD', 'ID_SC'] + [
        f'GPIO{i}' for i in range(2, 28)]
PIN_ALIASES = {
    'arduino-uno': {**{f'D{i}': str(i) for i in range(14)}, 'GND': 'GND.1', 'TX': '1', 'RX': '0'},
    'pi-pico': {'A0': 'GP26', 'A1': 'GP27', 'A2': 'GP28'},
    'pi-pico-w': {'A0': 'GP26', 'A1': 'GP27', 'A2': 'GP28'},
    'esp32-devkit-v1': {**{pin[1:]: pin for pin in PIN_LAYOUTS['esp32-devkit-v1'] if pin.startswith('D')},
                        '16': 'RX2', '17': 'TX2', '1': 'TX0', '3': 'RX0', '36': 'VP', '39': 'VN',
                        'GND': 'GND.1'},
}


def load_component_pins(path=None):
    path = Path(path) if path else ROOT / 'backend/catalog/component-pins.json'
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or not isinstance(data.get('components'), dict):
        raise ValueError('Invalid component pin catalog schema.')
    verified = {}
    for kind, record in data['components'].items():
        if not isinstance(record, dict) or record.get('defined') is not True or record.get('status') != 'verified':
            continue
        pins = record.get('pins')
        if not isinstance(pins, list) or not pins or record.get('pinCount') != len(pins):
            continue
        if any(not isinstance(pin, dict) or not isinstance(pin.get('name'), str) or not pin['name']
               or any(type(pin.get(axis)) not in (int, float) or not math.isfinite(pin[axis]) for axis in ('x', 'y'))
               for pin in pins):
            continue
        names = [pin['name'] for pin in pins]
        if len(set(names)) != len(names):
            continue
        verified[kind] = record
    return verified


EXPLICIT_PIN_LAYOUTS = frozenset(PIN_LAYOUTS)
COMPONENT_PINS = load_component_pins()
# Explicit layouts retain runtime pin names and board aliases.
for _kind, _record in COMPONENT_PINS.items():
    if not PIN_LAYOUTS.get(_kind):
        PIN_LAYOUTS[_kind] = [pin['name'] for pin in _record['pins']]


DEFAULT_BOARD_SOURCES = {
    'esp32-devkit-c-v4': 'void setup() {\n  Serial.begin(115200);\n}\n\nvoid loop() {\n  delay(500);\n}\n',
}
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def fail(status, message):
    raise HTTPException(status, message)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail(400, 'Invalid identifier.')
    return value


def public(project):
    return copy.deepcopy({key: value for key, value in project.items() if not key.startswith('_')})


def numeric(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1000000:
        fail(400, 'Coordinates must be finite numbers within +/-1000000.')
    return value


def source_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 100000:
        fail(400, 'Firmware must contain 1 to 100000 bytes of source.')
    if '\x00' in value:
        fail(400, 'Firmware cannot contain NUL bytes.')
    # Headers may name installed libraries, but never host files outside the sketch.
    for match in re.finditer(r'^\s*#\s*include\s*([<"])([^>"\n]+)[>"]', value, re.MULTILINE):
        header = match.group(2)
        if '..' in header or '\\' in header or header.startswith('/') or ':' in header:
            fail(400, 'Unsafe firmware include path.')
    return value


def calculator(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 256:
        fail(400, 'Calculator expression must contain 1 to 256 characters.')
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
              ast.Mod: operator.mod, ast.Pow: operator.pow}
    try:
        tree = ast.parse(expression, mode='eval')
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError()
        def evaluate(node, depth=0):
            if depth > 16:
                raise ValueError()
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                result = node.value
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = evaluate(node.operand, depth + 1) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and type(node.op) in binary:
                left, right = evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ValueError()
                result = binary[type(node.op)](left, right)
            else:
                raise ValueError()
            if type(result) not in (int, float) or not math.isfinite(result) or abs(result) > 1e100:
                raise ValueError()
            return result
        return {'expression': expression, 'result': evaluate(tree.body)}
    except (ValueError, SyntaxError, TypeError, ArithmeticError, RecursionError):
        fail(400, 'Only bounded finite numeric arithmetic is supported.')


SPICE_SOURCE_SCHEMAS = {
    'source-dc-voltage': ('DC Voltage Source', [('voltage', 5, -1000000, 1000000, 'V')]),
    'source-dc-current': ('DC Current Source', [('current', 0.001, -1000000, 1000000, 'A')]),
    'source-sine-voltage': ('Sine Voltage Source', [
        ('offset', 0, -1000000, 1000000, 'V'), ('amplitude', 5, 0, 1000000, 'V'),
        ('frequency', 1000, 0.000001, 1000000, 'Hz'), ('delay', 0, 0, 1000000, 's'),
        ('damping', 0, 0, 1000000, '1/s'), ('phase', 0, -360, 360, 'degrees')]),
    'source-pulse-voltage': ('Pulse Voltage Source', [
        ('low', 0, -1000000, 1000000, 'V'), ('high', 5, -1000000, 1000000, 'V'),
        ('delay', 0, 0, 1000000, 's'), ('rise', 0.000001, 0.000000000001, 1000000, 's'),
        ('fall', 0.000001, 0.000000000001, 1000000, 's'), ('width', 0.0005, 0.000000000001, 1000000, 's'),
        ('period', 0.001, 0.000000000001, 1000000, 's')]),
    'source-pwl-voltage': ('Piecewise Linear Voltage Source', []),
    'source-ac-voltage': ('AC Voltage Source', [
        ('voltage', 0, -1000000, 1000000, 'V'), ('magnitude', 1, 0, 1000000, 'V'),
        ('phase', 0, -360, 360, 'degrees')]),
    'ground': ('SPICE Ground', []),
}
SPICE_PASSIVE_DEFAULTS = {'resistor': '1000', 'capacitor': '1u', 'inductor': '1m'}
SPICE_VALUE_PATTERN = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:[TtGgKkMmUuNnPpFf]|[Mm][Ee][Gg])?'
SPICE_VALUE_TOKEN = re.compile(SPICE_VALUE_PATTERN + r'\Z')


def spice_catalog_entries():
    entries = []
    for kind, (name, fields) in SPICE_SOURCE_SCHEMAS.items():
        properties = [{'name': key, 'type': 'number', 'defaultValue': default,
                       'min': minimum, 'max': maximum, 'unit': unit, 'control': 'number'}
                      for key, default, minimum, maximum, unit in fields]
        if kind == 'source-pwl-voltage':
            properties.append({'name': 'points', 'type': 'number-pairs',
                               'defaultValue': [[0, 0], [0.001, 5], [0.002, 0]],
                               'minItems': 2, 'maxItems': 256, 'timeMin': 0, 'timeMax': 1000000,
                               'valueMin': -1000000, 'valueMax': 1000000})
        entries.append({'id': kind, 'name': name, 'category': 'analog', 'tagName': '',
                        'tags': ['spice', 'schematic', 'source' if kind != 'ground' else 'ground'],
                        'properties': properties, 'defaultValues': {field['name']: copy.deepcopy(field['defaultValue']) for field in properties},
                        'pinCount': len(PIN_LAYOUTS[kind]), 'schematic_only': True,
                        'spice_model': kind, 'schema_version': 1})
    return entries


def validate_spice_properties(entry, supplied, existing=None):
    descriptors = {field['name']: field for field in entry['properties']}
    values = copy.deepcopy(entry.get('defaultValues', {}))
    values.update(existing or {})
    values.update(supplied)
    for key, value in supplied.items():
        descriptor = descriptors.get(key)
        if descriptor is None:
            fail(400, f'Unknown property {key} for {entry["id"]}.')
        if descriptor['type'] == 'number':
            if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                fail(400, f'Property {key} must be a finite JSON number.')
            if not descriptor['min'] <= value <= descriptor['max']:
                fail(400, f'Property {key} is outside its allowed range.')
        elif descriptor['type'] == 'number-pairs':
            if not isinstance(value, list) or not 2 <= len(value) <= 256:
                fail(400, 'PWL points must contain 2 to 256 numeric [time, voltage] pairs.')
            previous = -1
            for point in value:
                if not isinstance(point, list) or len(point) != 2 or any(type(number) not in (int, float) or (type(number) is float and not math.isfinite(number)) for number in point):
                    fail(400, 'PWL points must contain finite numeric [time, voltage] pairs.')
                timestamp, voltage = point
                if not 0 <= timestamp <= 1000000 or not -1000000 <= voltage <= 1000000 or timestamp <= previous:
                    fail(400, 'PWL times must increase strictly within bounds; voltages must be within bounds.')
                previous = timestamp
        elif descriptor.get('format') == 'spice-value':
            if not isinstance(value, str) or len(value) > 48 or not SPICE_VALUE_TOKEN.fullmatch(value):
                fail(400, 'Passive value must be a strict positive SPICE engineering-number string.')
            suffix_match = re.search(r'(?i)(meg|[tgkmunpf])\Z', value)
            suffix = suffix_match.group().lower() if suffix_match else ''
            number = float(value[:suffix_match.start()] if suffix_match else value)
            scale = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3,
                     'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15}.get(suffix, 1)
            magnitude = number * scale
            if not math.isfinite(magnitude) or not descriptor['min'] <= magnitude <= descriptor['max']:
                fail(400, 'Passive value is outside its allowed positive range.')
    if entry['id'] == 'source-pulse-voltage' and values['rise'] + values['width'] + values['fall'] > values['period']:
        fail(400, 'Pulse rise + width + fall must not exceed period.')
    return copy.deepcopy(supplied)


class ComponentCatalog:
    def __init__(self, path=None):
        path = Path(path) if path else ROOT / 'vendor/velxio/frontend/public/components-metadata.json'
        self.origin = str(path) if path.is_file() else 'builtin-fallback'
        if path.is_file():
            items = json.loads(path.read_text(encoding='utf-8')).get('components', [])
        else:
            items = [{'id': key, 'name': key.replace('-', ' ').title(), 'category': 'boards' if key in BOARD_CONFIG else 'other'} for key in PIN_LAYOUTS]
        present = {item['id'] for item in items}
        items.extend({**board, 'category': 'boards', 'properties': [], 'defaultValues': {}}
                     for board in BOARDS if board['id'] not in present)
        source_entries = {item['id']: item for item in spice_catalog_entries()}
        for item in items:
            if item['id'] in source_entries:
                item.update(copy.deepcopy(source_entries.pop(item['id'])))
            if item['id'] in SPICE_PASSIVE_DEFAULTS:
                default = item.get('defaultValues', {}).get('value', SPICE_PASSIVE_DEFAULTS[item['id']])
                bounds = (1e-6, 1e12) if item['id'] == 'resistor' else (1e-15, 1e6)
                item.update({'properties': [{'name': 'value', 'type': 'string', 'format': 'spice-value',
                                              'pattern': SPICE_VALUE_PATTERN, 'min': bounds[0], 'max': bounds[1],
                                              'defaultValue': default, 'control': 'text'}],
                             'defaultValues': {'value': default}, 'spice_model': item['id'], 'schema_version': 1,
                             'pinCount': 2})
        items.extend(source_entries.values())
        present = {item['id'] for item in items}
        for kind, default in SPICE_PASSIVE_DEFAULTS.items():
            if kind not in present:
                minimum, maximum = (1e-6, 1e12) if kind == 'resistor' else (1e-15, 1e6)
                items.append({'id': kind, 'name': kind.title(), 'tagName': 'wokwi-' + kind,
                              'category': 'passive', 'pinCount': 2, 'spice_model': kind, 'schema_version': 1,
                              'defaultValues': {'value': default},
                              'properties': [{'name': 'value', 'type': 'string', 'format': 'spice-value',
                                              'pattern': SPICE_VALUE_PATTERN, 'min': minimum, 'max': maximum,
                                              'defaultValue': default, 'control': 'text'}]})
        self.components = {}
        for item in items:
            if item['id'] in BOARD_ALIASES:
                continue
            entry = copy.deepcopy(item)
            from backend.sensor_controls import add_sensor_controls
            add_sensor_controls(entry)
            if 'preset' in entry.get('tags', []) and entry.get('tagName') in ('wokwi-resistor','wokwi-capacitor','velxio-capacitor-electrolytic','wokwi-inductor'):
                entry['simulation_supported'] = True
                entry['simulation_scope'] = 'Existing ngspice passive model for DC/transient electrical analysis; not a digital MCU peripheral.'
                entry['spice_model'] = 'resistor' if entry['tagName'] == 'wokwi-resistor' else 'inductor' if entry['tagName'] == 'wokwi-inductor' else 'capacitor'
            # Generated decorators sometimes label numeric and boolean defaults as strings.
            for descriptor in entry.get('properties', []):
                default = descriptor.get('defaultValue')
                if descriptor.get('type') == 'string' and not descriptor.get('options'):
                    if type(default) is bool:
                        descriptor['type'] = 'boolean'
                    elif type(default) in (int, float):
                        descriptor['type'] = 'number'
            if item['id'] in ('ntc-temperature-sensor', 'photoresistor-sensor'):
                name, default, minimum, maximum = ('temperature', 25, -40, 125) if item['id'] == 'ntc-temperature-sensor' else ('lux', 500, 0, 1000)
                entry.setdefault('defaultValues', {})[name] = default
                if not any(prop['name'] == name for prop in entry.setdefault('properties', [])):
                    entry['properties'].append({'name':name,'type':'number','min':minimum,'max':maximum,'defaultValue':default,'control':'range'})
            if item['id'] == 'ssd1306' and not any(prop['name'] == 'protocol' for prop in entry.setdefault('properties', [])):
                entry['properties'].append({'name':'protocol','type':'select','options':['i2c','spi'],'defaultValue':'i2c','control':'select'})
            if item['id'] == 'hc-sr04':
                entry.setdefault('defaultValues', {})['distance'] = 10
                if not any(prop['name'] == 'distance' for prop in entry.setdefault('properties', [])):
                    entry['properties'].append({'name': 'distance', 'type': 'number', 'min': 2, 'max': 400, 'defaultValue': 10, 'control': 'range', 'unit': 'cm'})
            if item['id'] == 'bmp280':
                for name, default, minimum, maximum in [('temperature', 24, -40, 85), ('pressure', 1013.25, 300, 1100)]:
                    entry.setdefault('defaultValues', {})[name] = default
                    if not any(prop['name'] == name for prop in entry.setdefault('properties', [])):
                        entry['properties'].append({'name':name,'type':'number','min':minimum,'max':maximum,'defaultValue':default,'control':'range'})
                entry['properties'].append({'name':'i2cAddress','type':'select','options':['0x76','0x77'],'defaultValue':'0x76','control':'select'})
                entry['defaultValues']['i2cAddress'] = '0x76'
            if item['id'] == 'dht22':
                for name, default, minimum, maximum in [('temperature', 25, -40, 80), ('humidity', 50, 0, 100)]:
                    entry.setdefault('defaultValues', {})[name] = default
                    if not any(prop['name'] == name for prop in entry.setdefault('properties', [])):
                        entry['properties'].append({'name': name, 'type': 'number', 'min': minimum, 'max': maximum, 'defaultValue': default, 'control': 'range'})
            if item['id'] in ('neopixel', 'led-ring', 'neopixel-matrix'):
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = 'Original WS2812 pulse decoder with 5V/ground and Uno DIN GPIO; direct single/ring/matrix frames. DOUT chaining and power/current analysis are not implemented.'
            if item['id'] in ('membrane-keypad', 'ili9341', 'led-bar-graph'):
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = {
                    'membrane-keypad': 'Original passive 4x4 matrix model; eight distinct Uno D2-D13 GPIOs. No extra supply pins. Other column counts are unavailable.',
                    'ili9341': 'Original write-only SPI RGB565 display model with rotation; Uno MOSI11/SCK13 and distinct CS/D-C, 3.3V supply/backlight. Readback, touch and RST electrical behavior are not modeled.',
                    'led-bar-graph': 'Original ten-channel digital indicator; every used anode requires a distinct GPIO through a current-limiting resistor and its cathode to ground. No analog-current model.',
                }[item['id']]
            if item['id'] == 'dip-switch-8':
                entry['simulation_boards'] = ['arduino-uno', 'arduino-nano', 'arduino-mega']
                entry['simulation_scope'] = 'Independent grounded contacts: each used A pin to a distinct AVR GPIO and matching B pin to GND, with INPUT_PULLUP firmware. Contact bounce and other wiring modes are not modeled.'
            if item['id'] == 'slide-switch':
                entry['simulation_boards'] = ['arduino-uno', 'arduino-nano', 'arduino-mega']
                entry['simulation_scope'] = 'Rail-selected digital input: pin 1 to GND, pin 3 to 5V, pin 2 to a digital GPIO. Open-contact modes and contact bounce are not modeled.'
            if item['id'] == 'ky-040':
                entry['simulation_boards'] = ['arduino-uno', 'arduino-nano']
                entry['simulation_scope'] = 'Original rotary encoder digital pulses and active-low switch; requires distinct CLK/DT/SW GPIO and 5V/GND. Pulse spacing uses browser timers, not mechanical contact physics.'
            if item['id'] == 'analog-joystick':
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = 'Existing dual ADC axes and active-low pushbutton model; requires distinct VERT/HORZ ADC pins and SEL GPIO.'
            if item['id'] in ('gas-sensor','flame-sensor','big-sound-sensor','small-sound-sensor'):
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = 'Existing simplified ADC sensor model on Uno A0-A5; DOUT is only a GPIO-driven indicator, not a modeled sensor comparator.'
            if item['id'] in ('ntc-temperature-sensor', 'photoresistor-sensor', 'pir-motion-sensor'):
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = 'Existing Uno ADC/input model. NTC uses the original beta divider; photoresistor AO uses a simplified light-to-voltage mapping; PIR pulse uses browser wall time.'
            if item['id'] in ('ssd1306', 'ssd1306-i2c-4pin', 'lcd1602-i2c', 'lcd2004-i2c', 'mpu6050', 'ds1307', 'ds3231', 'bmp280'):
                entry['simulation_boards'] = ['arduino-uno']
                entry['simulation_scope'] = ('Existing Velxio SSD1306 I2C or hardware SPI model on Uno. SPI uses MOSI11/SCK13 plus distinct CS/DC GPIO; correct supply/ground are required.' if item['id'] == 'ssd1306' else 'Existing Velxio I2C model on Uno A4/SDA and A5/SCL; requires correct power, ground and address. Other interfaces and chip features are not certified.')
            extracted = COMPONENT_PINS.get(item['id'])
            matching_pins = extracted if extracted and extracted['tagName'] == entry.get('tagName') else None
            if matching_pins:
                entry['pinInfo'] = copy.deepcopy(matching_pins['pins'])
                entry['pin_provenance'] = 'backend/catalog/component-pins.json'
                for mode in ('pins', 'protocol'):
                    if mode in matching_pins.get('properties', {}):
                        entry.setdefault('defaultValues', {})[mode] = copy.deepcopy(matching_pins['properties'][mode])
            explicit = item['id'] in EXPLICIT_PIN_LAYOUTS
            entry['pins'] = copy.deepcopy(item.get('pins') or PIN_LAYOUTS.get(item['id'], [])) if explicit or matching_pins or item.get('pins') else []
            entry['connectable'] = bool(entry['pins'])
            if matching_pins and not explicit:
                entry['pinCount'] = len(entry['pins'])
            if item['id'] in ('potentiometer', 'servo', 'hc-sr04', 'slide-potentiometer', 'rgb-led', 'dht22'):
                entry['simulation_boards'] = ['arduino-uno', 'arduino-nano', 'arduino-mega', 'pi-pico', 'pi-pico-w'] if item['id'] == 'potentiometer' else ['arduino-uno'] if item['id'] == 'slide-potentiometer' else ['arduino-uno', 'arduino-nano', 'arduino-mega']
                entry['simulation_scope'] = {'potentiometer': 'ADC input from the Wokwi control; Nano A6/A7 are analog-only', 'slide-potentiometer': 'ADC input from the original slider control', 'rgb-led': 'Common-cathode digital/PWM channels; each channel requires a current-limiting resistor; no analog current model', 'dht22': 'Timed DHT22 protocol read by the Adafruit library; temperature/humidity controls, no physical environment model', 'servo': 'Angle from GPIO pulse widths; no torque or supply-current model', 'hc-sr04': 'Timed trigger/echo distance response; no acoustic interference model'}[item['id']]
            entry['pin_aliases'] = PIN_ALIASES.get(item['id'], {})
            entry['supported_board'] = item['id'] in BOARD_CONFIG if item.get('category') == 'boards' else None
            if item['id'] in BOARD_CONFIG:
                entry.update(BOARD_CONFIG[item['id']])
                entry['category'] = 'boards'
                entry['supported_board'] = True
                entry['aliases'] = [alias for alias, canonical in BOARD_ALIASES.items() if canonical == item['id']]
                entry['pinCount'] = len(entry['pins'])
            elif item.get('category') == 'boards':
                entry['compile'] = False
                entry['simulation'] = 'unavailable'
                entry['unavailable_reason'] = 'ESP32 requires QEMU, which is not configured.' if 'esp32' in item['id'] else 'This board runtime is not supported.'
            self.components[item['id']] = entry

    def search(self, query='', limit=50):
        if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 200:
            fail(400, 'Invalid component search query or limit.')
        needle = query.strip().casefold().removeprefix('wokwi-')
        def score(item):
            identity = str(item.get('id', '')).casefold()
            name = str(item.get('name', '')).casefold()
            tags = ' '.join(str(tag) for tag in item.get('tags', [])).casefold()
            if not needle:
                return 1
            if needle == identity or needle == name or needle in item.get('aliases', []):
                return 100
            if identity.startswith(needle) or name.startswith(needle):
                return 50
            if needle in identity or needle in name:
                return 20
            words = re.findall(r'[a-z]+', needle)
            if words and any(word == identity or word == name for word in words):
                return 30
            if needle in tags or needle in str(item.get('description', '')).casefold():
                return 5
            return 0
        matches = sorted((item for item in self.components.values() if score(item)), key=lambda item: (-score(item), str(item.get('name', item['id']))))
        return {'components': copy.deepcopy(matches[:limit]), 'boards': copy.deepcopy(BOARDS), 'origin': self.origin}


class ArduinoCompiler:
    def __init__(self, executable=None, timeout=None, output_limit=256000,
                 classic_esp32_config=None, classic_esp32_boot_app=None):
        self.executable = executable or os.getenv('ARDUINO_CLI_PATH') or shutil.which('arduino-cli')
        if not self.executable:
            candidate = Path('C:/Program Files/Arduino CLI/arduino-cli.exe')
            self.executable = str(candidate) if candidate.is_file() else None
        self.timeout = timeout
        self.output_limit = output_limit
        self.classic_esp32_config = Path(classic_esp32_config) if classic_esp32_config else ROOT / 'vendor/qemu/arduino-cli.yaml'
        self.classic_esp32_boot_app = Path(classic_esp32_boot_app) if classic_esp32_boot_app else ROOT / 'vendor/qemu/arduino-data/packages/esp32/hardware/esp32/2.0.17/tools/partitions/boot_app0.bin'

    def _classic_esp32_flash(self, output, build, board):
        if not self.classic_esp32_boot_app.is_file():
            fail(503, 'Classic ESP32 simulation requires the pinned Arduino ESP32 2.0.17 profile. Install it in vendor/qemu before compiling.')
        flash = bytearray(b'\xff' * board['flash_size_bytes'])
        for offset, filename, source in [
            (board['bootloader_offset'], 'sketch.ino.bootloader.bin', output / 'sketch.ino.bootloader.bin'),
            (0x8000, 'sketch.ino.partitions.bin', output / 'sketch.ino.partitions.bin'),
            (0xe000, 'boot_app0.bin', self.classic_esp32_boot_app),
            (0x10000, 'sketch.ino.bin', output / 'sketch.ino.bin'),
        ]:
            if not source.is_file():
                fail(502, f'Compiler produced no classic ESP32 {filename} segment.')
            data = source.read_bytes()
            if not data or offset + len(data) > len(flash):
                fail(502, f'Compiler produced an invalid classic ESP32 {filename} segment.')
            flash[offset:offset + len(data)] = data
            if filename == 'boot_app0.bin':
                (build / filename).write_bytes(data)
        merged = output / 'sketch.ino.merged.bin'
        merged.write_bytes(flash)
        return merged

    async def _terminate(self, process):
        if process.returncode is not None:
            return
        if os.name == 'nt':
            killer = await asyncio.create_subprocess_exec(
                str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/taskkill.exe'),
                '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except TimeoutError:
                killer.kill()
                await killer.wait()
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), 5)

    def _esp32_artifact(self, output, build, board, binary):
        if len(binary) != board['flash_size_bytes']:
            fail(502, 'Compiler did not produce a complete ESP32 merged flash image.')
        segments = []
        for name, offset, filename in [
            ('bootloader', board['bootloader_offset'], 'sketch.ino.bootloader.bin'),
            ('partitions', 0x8000, 'sketch.ino.partitions.bin'),
            ('boot_app0', 0xe000, 'boot_app0.bin'),
            ('application', 0x10000, 'sketch.ino.bin'),
        ]:
            path = output / filename
            if not path.is_file():
                path = build / filename
            if not path.is_file() or not 0 < path.stat().st_size <= len(binary) - offset:
                fail(502, f'Compiler produced no valid ESP32 {name} segment.')
            data = path.read_bytes()
            if binary[offset:offset + len(data)] != data:
                fail(502, f'ESP32 merged flash image does not match its {name} segment.')
            if name in ('bootloader', 'application'):
                chip_id = {'esp32': 0, 'esp32s3': 9, 'esp32c3': 5}[board['chip']]
                if len(data) < 24 or data[0] != 0xe9 or int.from_bytes(data[12:14], 'little') != chip_id:
                    fail(502, f'Compiler produced an invalid {board["chip"]} {name} image.')
            if name == 'partitions' and not data.startswith(b'\xaa\x50'):
                fail(502, 'Compiler produced an invalid ESP32 partition table.')
            segments.append({'name': name, 'filename': filename, 'offset': offset, 'size_bytes': len(data)})
        if any(first['offset'] + first['size_bytes'] > second['offset'] for first, second in zip(segments, segments[1:])):
            fail(502, 'Compiler produced overlapping ESP32 flash segments.')
        return {'bin': base64.b64encode(binary).decode('ascii'), 'encoding': 'base64',
                'load_address': 0, 'size_bytes': len(binary), 'image_kind': 'merged-flash',
                'chip': board['chip'], 'flash_size_bytes': len(binary), 'flash_segments': segments}

    async def compile(self, project):
        board = BOARD_CONFIG.get(project['board'])
        if board is None:
            fail(400, 'Unsupported compiler board.')
        if not board['compile']:
            fail(400, board['unavailable_reason'])
        if not self.executable or not Path(self.executable).is_file():
            fail(503, 'Arduino CLI is unavailable. Install it or configure ARDUINO_CLI_PATH.')
        source = source_text(project['firmware']['source'])
        compile_source = '#define Serial Serial1\n' + source if board['runtime'] == 'rp2040js' else source
        with tempfile.TemporaryDirectory(prefix='wireup-compile-') as directory:
            sketch = Path(directory) / 'sketch'
            sketch.mkdir()
            (sketch / 'sketch.ino').write_text(compile_source, encoding='utf-8')
            output = Path(directory) / 'output'
            build = Path(directory) / 'build'
            command = [str(self.executable), 'compile']
            if board['runtime'] == 'qemu-native':
                if not self.classic_esp32_config.is_file():
                    fail(503, 'Classic ESP32 simulation requires the pinned Arduino ESP32 2.0.17 profile. Install it in vendor/qemu before compiling.')
                command.extend(['--config-file', str(self.classic_esp32_config)])
            command.extend(['--fqbn', board['fqbn'], '--output-dir', str(output),
                            '--build-path', str(build), str(sketch)])
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    **({'start_new_session': True} if os.name != 'nt' else {}),
                )
            except OSError:
                fail(503, 'Arduino CLI could not be started.')
            async def drain(stream):
                data = bytearray()
                while chunk := await stream.read(8192):
                    if len(data) + len(chunk) > self.output_limit:
                        raise ValueError('Compiler output exceeded the safe limit.')
                    data.extend(chunk)
                return data.decode('utf-8', errors='replace')
            tasks = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr)),
                     asyncio.create_task(process.wait())]
            try:
                deadline = self.timeout if self.timeout is not None else board.get('compile_timeout_seconds', 90)
                stdout, stderr, code = await asyncio.wait_for(asyncio.gather(*tasks), deadline)
            except (TimeoutError, ValueError, asyncio.CancelledError) as error:
                await asyncio.shield(self._terminate(process))
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, TimeoutError):
                    fail(504, 'Arduino compilation timed out.')
                fail(502, str(error))
            artifact_format = board['format']
            artifact_path = output / ('sketch.ino.merged.bin' if board.get('chip') else f'sketch.ino.{artifact_format}')
            if code == 0 and board['runtime'] == 'qemu-native' and not artifact_path.is_file():
                artifact_path = self._classic_esp32_flash(output, build, board)
            successful = code == 0 and artifact_path.is_file()
            result = {'status': ('simulation_ready' if board['simulation'] in ('browser', 'native') else 'compilation_complete') if successful else 'error',
                      'simulation': board['simulation'], 'runtime': board['runtime'],
                      'unavailable_reason': board.get('unavailable_reason'),
                      'stdout': stdout, 'stderr': stderr, 'errors': [] if code == 0 else stderr.splitlines(),
                      'source_revision': project['firmware']['revision'], 'project_revision': project['revision'],
                      'source': source, 'compile_source': compile_source, 'fqbn': board['fqbn'],
                      'board': project['board'], 'artifact': None, 'compiled_at': now()}
            if successful:
                if artifact_path.stat().st_size > board.get('flash_size_bytes', 2000000):
                    fail(502, 'Compiled artifact exceeded the safe limit.')
                artifact = {'id': uuid.uuid4().hex, 'format': artifact_format, 'board': project['board'],
                            'fqbn': board['fqbn'], 'source_revision': project['firmware']['revision'],
                            'project_revision': project['revision']}
                if artifact_format == 'hex':
                    hex_text = artifact_path.read_text(encoding='ascii')
                    if not hex_text.startswith(':') or ':00000001FF' not in hex_text:
                        fail(502, 'Compiler did not produce a valid Intel HEX artifact.')
                    artifact['hex'] = hex_text
                elif board.get('chip'):
                    artifact.update(self._esp32_artifact(output, build, board, artifact_path.read_bytes()))
                else:
                    binary = artifact_path.read_bytes()
                    if len(binary) < 264:
                        fail(502, 'Compiler produced an incomplete RP2040 flash image.')
                    stack = int.from_bytes(binary[256:260], 'little')
                    entry = int.from_bytes(binary[260:264], 'little')
                    if not (0x20000000 <= stack <= 0x20042000 and entry & 1 and 0x10000100 <= entry < 0x10200000):
                        fail(502, 'Compiler produced an invalid RP2040 flash vector table.')
                    artifact.update({'bin': base64.b64encode(binary).decode('ascii'), 'encoding': 'base64',
                                     'load_address': 0x10000000, 'size_bytes': len(binary)})
                result['artifact'] = artifact
            elif code == 0:
                result['errors'] = [f'Arduino CLI produced no {artifact_path.name} artifact.']
            return result


class RuntimeBridge:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self.sessions = {}
        self.pending = {}

    async def attach(self, project_id, websocket):
        if project_id in self.sessions:
            await websocket.close(code=4409)
            return False
        self.sessions[project_id] = websocket
        self.pending[project_id] = {}
        return True

    def detach(self, project_id, websocket):
        if self.sessions.get(project_id) is not websocket:
            return
        self.sessions.pop(project_id, None)
        for future in self.pending.pop(project_id, {}).values():
            if not future.done():
                future.set_exception(HTTPException(503, 'Browser runtime disconnected.'))

    def acknowledge(self, project_id, message):
        if not isinstance(message, dict) or message.get('type') != 'ack':
            return
        correlation = message.get('id')
        if not isinstance(correlation, str):
            return
        future = self.pending.get(project_id, {}).get(correlation)
        if future and not future.done():
            if message.get('ok') is True and isinstance(message.get('result'), dict):
                future.set_result(message['result'])
            else:
                error = str(message.get('error', 'Browser runtime rejected the command.'))[:2000]
                future.set_exception(HTTPException(502, error))

    async def command(self, project_id, name, args):
        if len(self.pending.get(project_id, {})) >= 32:
            fail(429, 'Too many pending browser runtime commands.')
        websocket = self.sessions.get(project_id)
        if not websocket:
            fail(503, 'No browser runtime is connected to this project.')
        correlation = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[project_id][correlation] = future
        try:
            await asyncio.wait_for(websocket.send_json({'type': 'command', 'id': correlation, 'name': name,
                                                        'args': args, 'project_id': project_id}), self.timeout)
            result = await asyncio.wait_for(future, self.timeout)
            return {'status': 'acknowledged', 'command': name, 'id': correlation, 'result': result}
        except TimeoutError:
            fail(504, 'Browser runtime acknowledgement timed out.')
        except (WebSocketDisconnect, RuntimeError, OSError):
            fail(503, 'Browser runtime disconnected.')
        finally:
            self.pending.get(project_id, {}).pop(correlation, None)
            if not future.done():
                future.cancel()


class HardwareService:
    def __init__(self, data_dir=None, compiler=None, runtime=None, catalog=None, native=None):
        self.data_dir = Path(data_dir) if data_dir else ROOT / 'backend/data/hardware'
        self.compiler = compiler or ArduinoCompiler()
        self.runtime = runtime or RuntimeBridge()
        from backend.native_esp32 import NativeESP32
        self.native = native or NativeESP32()
        self.catalog = catalog or ComponentCatalog()
        self.lock = threading.RLock()
        self.compile_gate = asyncio.Semaphore(1)

    def _path(self, project_id):
        return self.data_dir / f'{identifier(project_id)}.json'

    def _load(self, project_id):
        path = self._path(project_id)
        if not path.is_file():
            fail(404, 'Project not found.')
        return json.loads(path.read_text(encoding='utf-8'))

    def _save(self, project):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        destination = self._path(project['id'])
        temporary = destination.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        try:
            temporary.write_text(json.dumps(project, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            for attempt in range(4):
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError:
                    if os.name != 'nt' or attempt == 3:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

    def create_project(self, name='Untitled circuit', board='unselected'):
        if not isinstance(board, str):
            fail(400, 'Unsupported board. Select a board from the hardware catalog.')
        board = BOARD_ALIASES.get(board.removeprefix('wokwi-'), board.removeprefix('wokwi-'))
        if board != 'unselected' and board not in BOARD_CONFIG:
            fail(400, 'Unsupported board. Select a board from the hardware catalog.')
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            fail(400, 'Project name must contain 1 to 120 characters.')
        project = {'id': uuid.uuid4().hex, 'schema_version': 1, 'revision': 1, 'name': name.strip(),
                   'board': board, 'components': [] if board == 'unselected' else [{'id': 'board', 'type': board, 'x': 120, 'y': 100,
                                                   'rotation': 0, 'properties': {}}], 'wires': [],
                   'firmware': {'filename': 'main.py' if board.startswith('raspberry-pi-') else 'sketch.ino', 'source': '' if board == 'unselected' or board.startswith('raspberry-pi-') else DEFAULT_BOARD_SOURCES.get(board, DEFAULT_SOURCE), 'revision': 1},
                   'requirements': {'status': 'unconfirmed', 'answers': {}},
                   'history': [], '_undo': [], 'compiler': None, '_artifacts': {},
                   'runtime_token': secrets.token_urlsafe(32), 'created_at': now(), 'updated_at': now()}
        with self.lock:
            self._save(project)
        return public(project)

    def get_project(self, project_id):
        with self.lock:
            return public(self._load(project_id))

    def list_projects(self):
        with self.lock:
            projects = []
            if self.data_dir.is_dir():
                for path in sorted(self.data_dir.glob('*.json')):
                    project = self._load(path.stem)
                    projects.append({key: project[key] for key in ('id', 'name', 'board', 'revision', 'created_at', 'updated_at')})
            return {'projects': projects}

    def delete_project(self, project_id):
        with self.lock:
            project = self._load(project_id)
            if self.native.active(project_id):
                fail(409, 'Stop the native ESP32 runtime before deleting this project.')
            if project_id in self.runtime.sessions:
                fail(409, 'This project is open in a hardware workspace. Close its workspace before deleting it.')
            self._path(project_id).unlink()
            return {'deleted': project['id']}

    def authorize_runtime(self, project, token):
        if not isinstance(token, str) or not hmac.compare_digest(project['runtime_token'], token):
            fail(403, 'A matching project runtime token is required.')

    def _check_revision(self, project, args):
        revision = args.get('expected_revision')
        if revision is not None and (type(revision) is not int or revision != project['revision']):
            fail(409, {'code': 'stale_project_revision',
                       'message': f'Project revision conflict; current revision is {project["revision"]}.',
                       'expected_revision': revision, 'actual_revision': project['revision'],
                       'recovery': 'Reload the project and retry the change against the current revision.'})

    def confirm_requirements(self, project_id, answers):
        if not isinstance(answers, dict) or not 1 <= len(answers) <= 20:
            fail(400, 'Confirmed requirements must be a nonempty bounded object.')
        if any(not isinstance(key, str) or not key or len(key) > 80
               or not isinstance(value, str) or not value or len(value) > 1000
               for key, value in answers.items()):
            fail(400, 'Confirmed requirements contain an invalid answer.')
        with self.lock:
            project = self._load(project_id)
            confirmed = {'status': 'confirmed', 'answers': copy.deepcopy(answers),
                         'project_revision': project['revision'], 'updated_at': now()}
            existing = project.get('requirements')
            if isinstance(existing, dict) and existing.get('status') == 'confirmed' and existing.get('answers') == answers:
                return copy.deepcopy(existing)
            project['requirements'] = confirmed
            self._save(project)
            return copy.deepcopy(confirmed)

    def _properties(self, kind, supplied, existing=None):
        if not isinstance(supplied, dict) or len(json.dumps(supplied, allow_nan=False)) > 16000:
            fail(400, 'Component properties must be a bounded JSON object.')
        entry = self.catalog.components[kind]
        if entry.get('spice_model'):
            return validate_spice_properties(entry, supplied, existing)
        descriptors = {item['name']: item for item in entry.get('properties', [])}
        for key, value in supplied.items():
            descriptor = descriptors.get(key)
            if descriptor is None:
                fail(400, f'Unknown property {key} for {kind}.')
            typ = descriptor.get('type')
            if typ == 'number':
                numeric(value)
                if value < descriptor.get('min', -1000000) or value > descriptor.get('max', 1000000):
                    fail(400, f'Property {key} is outside its allowed range.')
            elif typ == 'boolean' and type(value) is not bool:
                fail(400, f'Property {key} must be boolean.')
            elif typ in ('string', 'color', 'select') and not isinstance(value, str):
                fail(400, f'Property {key} must be a string.')
            if descriptor.get('options') and value not in descriptor['options']:
                fail(400, f'Invalid option for {key}.')
        return copy.deepcopy(supplied)

    def _component(self, project, component_id):
        identifier(component_id)
        for item in project['components']:
            if item['id'] == component_id:
                return item
        fail(404, 'Component not found.')

    def _endpoint(self, project, endpoint):
        if not isinstance(endpoint, dict) or set(endpoint) != {'component', 'pin'}:
            fail(400, 'Wire endpoint must contain component and pin.')
        component = self._component(project, endpoint['component'])
        pin = endpoint['pin']
        if not isinstance(pin, str):
            fail(400, 'Pin names must be strings.')
        pin = PIN_ALIASES.get(component['type'], {}).get(pin, pin)
        available = self.catalog.components[component['type']]['pins']
        if component['type'] in BOARD_CONFIG and pin not in available:
            candidates = []
            digital = re.fullmatch(r'D(\d+)', pin)
            if digital:
                candidates.append(digital.group(1))
            elif pin.isdecimal():
                candidates.append('D' + pin)
            if pin == 'GND':
                candidates.append('GND.1')
            if pin == '3V3':
                candidates.append('3.3V')
            elif pin == '3.3V':
                candidates.append('3V3')
            pin = next((candidate for candidate in candidates if candidate in available), pin)
        if pin not in available:
            fail(400, 'Unknown component pin or unavailable pin layout.')
        return {'component': endpoint['component'], 'pin': pin}

    def _mutate(self, project_id, name, args):
        with self.lock:
            project = self._load(project_id)
            self._check_revision(project, args)
            snapshot = {key: copy.deepcopy(project[key]) for key in ('name', 'board', 'components', 'wires', 'firmware')}
            if name == 'add_component':
                kind = args.get('type')
                if isinstance(kind, str):
                    kind = BOARD_ALIASES.get(kind.removeprefix('wokwi-'), kind.removeprefix('wokwi-'))
                if not isinstance(kind, str) or kind not in self.catalog.components:
                    fail(400, 'Unknown component type.')
                if self.catalog.components[kind].get('category') == 'boards' or kind in BOARD_CONFIG:
                    if kind not in BOARD_CONFIG:
                        fail(400, 'This board is not available for placement. Select a board from the hardware catalog.')
                    board = next((part for part in project['components'] if part['id'] == 'board'), None)
                    if board is None:
                        board = {'id': 'board', 'type': kind, 'x': numeric(args.get('x', 120)), 'y': numeric(args.get('y', 100)), 'rotation': numeric(args.get('rotation', 0)), 'properties': {}}
                        project['components'].append(board)
                    if kind == project['board']:
                        fail(409, 'This microcontroller is already placed in the project.')
                    if any(wire['from']['component'] == 'board' or wire['to']['component'] == 'board' for wire in project['wires']):
                        fail(409, 'Remove wires attached to the current board before replacing the microcontroller.')
                    if not project['firmware']['source'] or project['firmware']['source'] == DEFAULT_BOARD_SOURCES.get(project['board'], DEFAULT_SOURCE):
                        project['firmware']['source'] = '' if kind.startswith('raspberry-pi-') else DEFAULT_BOARD_SOURCES.get(kind, DEFAULT_SOURCE)
                        project['firmware']['filename'] = 'main.py' if kind.startswith('raspberry-pi-') else 'sketch.ino'
                    placement = {key: numeric(args[key]) for key in ('x', 'y', 'rotation') if key in args}
                    board.update(type=kind, properties={}, **placement)
                    project['board'] = kind
                    project['firmware']['revision'] += 1
                    project['_undo'] = (project['_undo'] + [snapshot])[-50:]
                    project['revision'] += 1
                    if project['compiler']:
                        project['compiler']['stale'] = True
                    project['updated_at'] = now()
                    project['history'] = (project['history'] + [{'revision': project['revision'], 'operation': 'replace_board', 'timestamp': project['updated_at']}])[-200:]
                    self._save(project)
                    return public(project)
                if len(project['components']) >= 200:
                    fail(400, 'Project component limit reached.')
                component_id = identifier(args.get('id', uuid.uuid4().hex))
                if any(item['id'] == component_id for item in project['components']):
                    fail(409, 'Component identifier already exists.')
                properties = copy.deepcopy(self.catalog.components[kind].get('defaultValues', {}))
                properties.update(self._properties(kind, args.get('properties', {})))
                placement_index = max(0, len(project['components']) - 1)
                project['components'].append({'id': component_id, 'type': kind, 'x': numeric(args.get('x', 380 + (placement_index % 3) * 180)),
                                               'y': numeric(args.get('y', 100 + (placement_index // 3) * 140)), 'rotation': numeric(args.get('rotation', 0)),
                                               'properties': properties})
            elif name in ('remove_component', 'modify_component'):
                component = self._component(project, args.get('id'))
                if name == 'remove_component':
                    if component['id'] == 'board':
                        fail(400, 'The project board cannot be removed.')
                    project['components'].remove(component)
                    project['wires'] = [wire for wire in project['wires'] if all(wire[end]['component'] != component['id'] for end in ('from', 'to'))]
                else:
                    for key in ('x', 'y', 'rotation'):
                        if key in args:
                            component[key] = numeric(args[key])
                    if 'properties' in args:
                        component['properties'].update(self._properties(component['type'], args['properties'], component['properties']))
            elif name == 'connect_wire':
                if len(project['wires']) >= 500:
                    fail(400, 'Project wire limit reached.')
                start, end = self._endpoint(project, args.get('from')), self._endpoint(project, args.get('to'))
                if start == end:
                    fail(400, 'A wire must connect two distinct pins.')
                if any((wire['from'] == start and wire['to'] == end) or (wire['from'] == end and wire['to'] == start) for wire in project['wires']):
                    fail(409, 'This wire connection already exists.')
                wire_id = identifier(args.get('id', uuid.uuid4().hex))
                if any(wire['id'] == wire_id for wire in project['wires']):
                    fail(409, 'Wire identifier already exists.')
                color = args.get('color', '#22c55e')
                if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}|[a-zA-Z]{1,20}', color):
                    fail(400, 'Invalid wire color.')
                project['wires'].append({'id': wire_id, 'from': start, 'to': end, 'color': color})
            elif name == 'remove_wire':
                wire_id = identifier(args.get('id'))
                wire = next((item for item in project['wires'] if item['id'] == wire_id), None)
                if wire is None:
                    fail(404, 'Wire not found.')
                project['wires'].remove(wire)
            elif name in ('generate_firmware', 'edit_firmware'):
                source = args.get('source')
                if source is None and name == 'edit_firmware':
                    old, new = args.get('old'), args.get('new')
                    current = project['firmware']['source']
                    if not isinstance(old, str) or not old or not isinstance(new, str) or current.count(old) != 1:
                        fail(400, 'Firmware edit must match exactly one nonempty source segment.')
                    source = current.replace(old, new, 1)
                source = source_text(source)
                if source == project['firmware']['source']:
                    return public(project)
                project['firmware']['source'] = source
                project['firmware']['revision'] += 1
            elif name == 'undo':
                if not project['_undo']:
                    fail(409, 'There is no mutation to undo.')
                restored = project['_undo'].pop()
                source_revision = project['firmware']['revision']
                changed_source = (restored['firmware']['source'] != project['firmware']['source'] or restored['firmware']['filename'] != project['firmware']['filename'] or restored['board'] != project['board'])
                project.update(restored)
                project['firmware']['revision'] = source_revision + int(changed_source)
            else:
                fail(400, 'Unknown mutation.')
            if name != 'undo':
                project['_undo'] = (project['_undo'] + [snapshot])[-50:]
            project['revision'] += 1
            if project['compiler']:
                project['compiler']['stale'] = True
            project['updated_at'] = now()
            project['history'] = (project['history'] + [{'revision': project['revision'], 'operation': name,
                                                       'timestamp': project['updated_at']}])[-200:]
            self._save(project)
            return public(project)

    def get_artifact(self, project_id, artifact_id):
        identifier(artifact_id)
        with self.lock:
            artifact = self._load(project_id)['_artifacts'].get(artifact_id)
            if artifact is None:
                fail(404, 'Artifact not found.')
            return copy.deepcopy(artifact)

    async def command(self, project_id, name, args=None, runtime_token=None):
        args = {} if args is None else args
        if not isinstance(args, dict):
            fail(400, 'Command args must be an object.')
        try:
            encoded = json.dumps(args, allow_nan=False)
        except (ValueError, TypeError):
            fail(400, 'Command args must contain finite JSON values.')
        if len(encoded) > 200000:
            fail(400, 'Command arguments are too large.')
        if name not in TOOLS and name not in ('undo', 'wire_circuit', 'validate_circuit', 'route_breadboard'):
            fail(400, 'Unknown hardware command.')
        if name == 'route_breadboard':
            from backend.breadboard_routing import route_breadboard
            return await route_breadboard(self, project_id, args)
        project = self.get_project(project_id)
        if name == 'wire_circuit':
            from backend.circuit_wiring import wire_circuit
            return await wire_circuit(self, project_id, args)
        if name == 'check_connections':
            from backend.circuit_wiring import check_connections
            return await check_connections(self, project_id, args)
        if name == 'validate_circuit':
            from backend.circuit_wiring import validate_circuit
            return await validate_circuit(self, project_id)
        if name == 'read_project':
            return project
        if name == 'search_components':
            return self.catalog.search(args.get('query', ''), args.get('limit', 50))
        if name == 'read_firmware':
            return project['firmware']
        if name == 'calculator':
            return calculator(args.get('expression'))
        if name == 'read_compiler_errors':
            return project['compiler'] or {'status': 'not_compiled', 'errors': []}
        if name == 'compile_firmware':
            self._check_revision(project, args)
            compilation_error = None
            try:
                try:
                    await asyncio.wait_for(self.compile_gate.acquire(), 10)
                except TimeoutError:
                    fail(503, 'Compiler is busy. Retry shortly.')
                try:
                    result = await self.compiler.compile(project)
                finally:
                    self.compile_gate.release()
            except HTTPException as error:
                if error.status_code == 503 and 'busy' in str(error.detail).lower():
                    raise
                compilation_error = error
                result = {'status': 'error', 'stdout': '', 'stderr': str(error.detail),
                          'errors': [str(error.detail)], 'source': project['firmware']['source'],
                          'source_revision': project['firmware']['revision'],
                          'project_revision': project['revision'], 'board': project['board'],
                          'artifact': None, 'compiled_at': now()}
            with self.lock:
                latest = self._load(project_id)
                result['stale'] = latest['revision'] != project['revision'] or latest['firmware']['revision'] != project['firmware']['revision']
                if result.get('artifact'):
                    artifact = result['artifact']
                    artifact['source'] = project['firmware']['source']
                    artifact['url'] = f'/api/hardware/project/{project_id}/artifacts/{artifact["id"]}'
                    latest['_artifacts'][artifact['id']] = copy.deepcopy(artifact)
                    # Retain a bounded set of original-revision artifacts.
                    latest['_artifacts'] = dict(list(latest['_artifacts'].items())[-10:])
                latest['compiler'] = result
                self._save(latest)
            if compilation_error:
                raise compilation_error
            return copy.deepcopy(result)
        if name in ('run_simulation', 'stop_simulation', 'read_simulation_results'):
            self.authorize_runtime(project, runtime_token)
            board = BOARD_CONFIG.get(project['board'])
            if board and board['runtime'] == 'qemu-native':
                from backend.native_esp32 import RunRequest, StopRequest
                revision_args = {'runtime_token': runtime_token, 'expected_revision': args.get('expected_revision', project['revision']),
                                 'source_revision': args.get('source_revision', project['firmware']['revision'])}
                if type(revision_args['expected_revision']) is not int:
                    fail(409, 'Project revision conflict. Reload the current project.')
                self._check_revision(project, revision_args)
                if type(revision_args['source_revision']) is not int or revision_args['source_revision'] != project['firmware']['revision']:
                    fail(409, 'Firmware revision conflict. Reload the current project.')
                for key in ('artifact_id', 'run_id'):
                    if args.get(key) is not None:
                        identifier(args[key])
                if name == 'run_simulation':
                    result = await self.native.run(self, project_id, RunRequest(**revision_args, artifact_id=args.get('artifact_id')))
                elif name == 'stop_simulation':
                    result = await self.native.stop(self, project_id, StopRequest(**revision_args, run_id=args.get('run_id')))
                else:
                    result = await self.native.results(self, project_id, runtime_token,
                                                       revision_args['expected_revision'], revision_args['source_revision'],
                                                       run_id=args.get('run_id'))
                return {'status': 'acknowledged', 'command': name, 'result': result}
            remote = project['board'] in ('esp32-devkit-v1', 'esp32-devkit-c-v4', 'esp32-s3', 'esp32-c3') and bool(os.getenv('REMOTE_SIMULATION_URL'))
            native_adapter = project['board'] in ('esp32-devkit-v1', 'esp32-devkit-c-v4') and project_id in getattr(self.runtime, 'sessions', {})
            if not board or (board['simulation'] != 'browser' and not remote and not native_adapter):
                fail(503, board['unavailable_reason'] if board else 'This board has no configured runtime.')
            runtime_args = {}
            if name == 'run_simulation':
                if project['board'] == 'pi-pico-w' and re.search(r'\b(?:WiFi|Bluetooth|cyw43|LED_BUILTIN)\b', project['firmware']['source'], re.I):
                    fail(503, 'Pico W browser simulation supports external GPIO and UART0 only. This firmware uses the unimplemented CYW43 radio or onboard LED; use external GPIO firmware or real hardware.')
                compiled = project['compiler']
                artifact_id = args.get('artifact_id')
                if artifact_id:
                    artifact = self.get_artifact(project_id, artifact_id)
                elif compiled and compiled.get('artifact'):
                    artifact = compiled['artifact']
                else:
                    fail(409, 'Compile firmware successfully before running the browser simulator.')
                if artifact['source_revision'] != project['firmware']['revision']:
                    fail(409, 'Compiled artifact is stale. Recompile the current project.')
                if artifact['board'] != project['board'] or artifact['format'] != BOARD_CONFIG[project['board']]['format']:
                    fail(409, 'Compiled artifact does not match the project board.')
                runtime_project = copy.deepcopy(project)
                runtime_project.pop('runtime_token', None)
                runtime_args = {'project': runtime_project, 'artifact': artifact}
            if remote:
                from backend.remote_projects import remote_projects
                return await remote_projects.command(project, name, runtime_args.get('artifact'))
            return await self.runtime.command(project_id, name, runtime_args)
        return self._mutate(project_id, name, args)


class CreateProject(BaseModel):
    name: str = Field(default='Untitled circuit', min_length=1, max_length=120)
    board: str = 'unselected'


class HardwareCommand(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    args: dict = Field(default_factory=dict)
    runtime_token: str | None = Field(default=None, max_length=200)


def local_connection(connection):
    security = connection.scope.get('wireup.security', {})
    if security.get('production'):
        return bool(security.get('authentication_required') and security.get('authenticated') and (not security.get('origin_present') or security.get('trusted_origin')))
    def loopback(host):
        if host in ('localhost', 'testclient', 'testserver'):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    if connection.client is None or not loopback(connection.client.host):
        return False
    try:
        host = urlsplit('http://' + connection.headers.get('host', '')).hostname
        origin = connection.headers.get('origin')
        if not host or not loopback(host):
            return False
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or not loopback(parsed.hostname):
                return False
    except ValueError:
        return False
    return True


def create_router(hardware_service):
    async def local_only(connection: HTTPConnection, response: Response):
        if not local_connection(connection):
            if connection.scope['type'] == 'websocket':
                await connection.close(code=4403)
            fail(403, 'Hardware API is local-only; loopback client, Host, and Origin are required.')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
    api = APIRouter(prefix='/api/hardware', tags=['hardware'], dependencies=[Depends(local_only)])

    @api.post('/projects')
    async def create_project(payload: CreateProject):
        return hardware_service.create_project(payload.name, payload.board)

    @api.get('/projects')
    async def list_projects():
        return hardware_service.list_projects()

    @api.get('/projects/{project_id}')
    async def get_project(project_id: str):
        return hardware_service.get_project(project_id)

    @api.delete('/projects/{project_id}')
    async def delete_project(project_id: str):
        return hardware_service.delete_project(project_id)

    @api.get('/catalog')
    async def catalog(q: str = '', limit: int = 50):
        return hardware_service.catalog.search(q, limit)

    @api.post('/project/{project_id}/command')
    async def command(project_id: str, payload: HardwareCommand):
        return await hardware_service.command(project_id, payload.name, payload.args, payload.runtime_token)

    @api.get('/project/{project_id}/artifacts/{artifact_id}')
    async def artifact(project_id: str, artifact_id: str):
        result = hardware_service.get_artifact(project_id, artifact_id)
        headers = {'Content-Disposition': f'attachment; filename="sketch.ino.{result["format"]}"',
                   'X-Source-Revision': str(result['source_revision']),
                   'X-Project-Revision': str(result['project_revision']),
                   'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}
        if result['format'] == 'bin':
            return Response(base64.b64decode(result['bin'], validate=True), media_type='application/octet-stream', headers=headers)
        return PlainTextResponse(result['hex'], headers=headers)

    @api.websocket('/project/{project_id}/runtime')
    async def runtime(websocket: WebSocket, project_id: str):
        try:
            project = hardware_service.get_project(project_id)
            hardware_service.authorize_runtime(project, websocket.query_params.get('token'))
        except HTTPException as error:
            await websocket.close(code=4403 if error.status_code == 403 else 4404)
            return
        await websocket.accept()
        if not await hardware_service.runtime.attach(project_id, websocket):
            return
        try:
            await websocket.send_json({'type': 'runtime_connected', 'project_id': project_id})
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 256000:
                    await websocket.close(code=4400)
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    await websocket.close(code=4400)
                    break
                hardware_service.runtime.acknowledge(project_id, message)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            hardware_service.runtime.detach(project_id, websocket)

    from backend.build_guide import create_router as create_build_guide_router
    api.include_router(create_build_guide_router(hardware_service))
    from backend.board_flash import create_router as create_flash_router
    api.include_router(create_flash_router(hardware_service, prefix=''))
    from backend.native_esp32 import create_router as create_native_router
    api.include_router(create_native_router(hardware_service))
    return api


service = HardwareService()
router = create_router(service)
