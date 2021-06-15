import pytest

from waveline import SpotWave


@pytest.fixture(scope="module")
def sw():
    devices = SpotWave.discover()
    if not devices:
        raise RuntimeError(
            "No spotWave devices found. Please connect a device to run the system tests"
        )
    with SpotWave(devices[0]) as sw:
        sw.set_datetime()  # set current date/time
        sw.clear_data_log()
        sw.clear_buffer()
        yield sw
