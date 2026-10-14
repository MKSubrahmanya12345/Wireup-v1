import asyncio
import pytest
from fastapi import HTTPException
from backend.hardware import HardwareService, BOARD_CONFIG


def test_esp32_runtimes_are_chip_specific():
    for board in ('esp32-c3','esp32-s3'):
        assert BOARD_CONFIG[board]['simulation']=='browser'
        assert BOARD_CONFIG[board]['runtime']=='esp-emu-wasm'
    for board in ('esp32-devkit-v1','esp32-devkit-c-v4'):
        assert BOARD_CONFIG[board]['simulation']=='native'
        assert BOARD_CONFIG[board]['runtime']=='qemu-native'
        assert 'GPIO' in BOARD_CONFIG[board]['simulation_scope']


@pytest.mark.parametrize('board',['esp32-c3','esp32-s3'])
def test_wasm_targets_require_actual_compilation_before_run(tmp_path,board):
    service=HardwareService(tmp_path)
    project=service.create_project(board=board)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.command(project['id'],'run_simulation',runtime_token=project['runtime_token']))
    assert error.value.status_code==409
