"""
Module for spotWave device.

All functions are exposed by the `SpotWave` class.
"""


import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional, Union

import numpy as np
from serial import EIGHTBITS, Serial
from serial.tools import list_ports

logger = logging.getLogger(__name__)


@dataclass
class Status:
    device_id: str
    firmware_version: str
    temperature: int
    data_size: int
    datetime: datetime


@dataclass
class Setup:
    acq_enabled: bool
    cont_enabled: bool
    log_enabled: bool
    adc_to_volts: float
    threshold_volts: float
    ddt_seconds: float
    status_interval_seconds: float
    filter_highpass_hz: float
    filter_lowpass_hz: float
    filter_order: int
    tr_enabled: bool
    tr_decimation: int
    tr_pretrigger_samples: int
    tr_postduration_samples: int
    cct_seconds: float


@dataclass
class AERecord:
    type_: str  #: Record type (hit or status data)
    time: float  #: Time in seconds
    amplitude: float  #: Peak amplitude in volts
    rise_time: float  #: Rise time in seconds
    duration: float  #: Duration in seconds
    counts: int  #: Number of positive threshold crossings
    energy: float  #: Energy (EN 1330-9) in eu (1e-14 V²s)
    trai: int  #: Transient recorder index (key between `HitRecord` and `TRRecord`)
    flags: int  #: Hit flags (TODO: list available flags)


@dataclass
class TRRecord:
    trai: int  #: Transient recorder index (key between `HitRecord` and `TRRecord`)
    time: float  #: Time in seconds
    samples: int  #: Number of samples
    data: np.ndarray  #: Array of transient data in volts


def _as_int(string):
    """Remove non-numeric characters and return as int."""
    return int(re.sub(r"[^\-\d]", "", string))


def _as_float(string):
    """Remove non-numeric characters and return as float."""
    return float(re.sub(r"[^\-\d\.]", "", string))


def _multiline_output_to_dict(lines: List[bytes]):
    """Helper function to parse output from get_info, get_status and get_setup."""
    def key_value_generator(lines: List[str]):
        for line in lines:
            try:
                key, value = line.split("=", maxsplit=1)
                yield key.strip(), value.strip()
            except ValueError:
                ...

    lines_decoded = [line.strip().decode(errors="replace") for line in lines]
    return dict(key_value_generator(lines_decoded))


class SpotWave:
    """
    Interface for spotWave devices.

    The USB-connected device exposes a virtual serial port for communication.
    """

    VENDOR_ID = 8849  #: USB vendor id of Vallen Systeme GmbH
    PRODUCT_ID = 272  #: USB product id of SpotWave device
    CLOCK = 2_000_000  #: Internal clock in Hz = 2 MHz

    def __init__(self, port: Union[str, Serial]):
        """
        Initialize device.

        Args:
            port: Either the serial port id (e.g. 'COM6') or a `serial.Serial` port instance.
                Please us the method `discover` to get a list of ports with connected devices.
        Returns:
            Instance of `SpotWave`
        """
        if isinstance(port, str):
            self._ser = Serial(port=port)
        elif isinstance(port, Serial):
            self._ser = port
        else:
            raise ValueError("Either pass a port id or a Serial instance")

        self._ser.baudrate = 115200  # doesn't matter
        self._ser.bytesize = EIGHTBITS
        self._ser.timeout = 1  # seconds
        self._ser.exclusive = True
        if not self._ser.is_open:
            self._ser.open()

        # stop acquisition if running
        self.stop_acquisition()
        # get and save adc conversion factor
        self._adc_to_volts = self._get_adc_to_volts()

    def _get_adc_to_volts(self):
        return self.get_setup().adc_to_volts

    def _close(self):
        if not hasattr(self, "_ser"):
            return
        if self._ser.is_open:
            self._ser.close()

    def __del__(self):
        self._close()

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self._close()

    @contextmanager
    def _timeout_context(self, timeout_seconds: float):
        """Temporary set serial read/write timeout within context."""
        old_timeout = self._ser.timeout
        self._ser.timeout = timeout_seconds
        yield None
        self._ser.timeout = old_timeout

    @classmethod
    def discover(cls) -> List[str]:
        """
        Discover connected spotWave devices.

        Returns:
            List of port names
        """
        ports = list_ports.comports()
        ports_spotwave = filter(
            lambda p: (p.vid == cls.VENDOR_ID) & (p.pid == cls.PRODUCT_ID),
            ports,
        )
        return [port.name for port in ports_spotwave]

    def clear_buffer(self):
        """Clear input and output buffer."""
        with self._timeout_context(0.1):
            self._ser.readlines()
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def _send_command(self, command: str):
        command_bytes = command.encode("utf-8") + b"\n"  # str -> bytes
        logger.debug("Send command: %a", command_bytes)
        self._ser.write(command_bytes)

    def get_setup(self) -> Setup:
        """
        Get setup.

        Returns:
            Dataclass with setup information
        """
        self._send_command("get_setup")
        with self._timeout_context(0.1):
            lines = self._ser.readlines()
        if not lines:
            raise RuntimeError("Could not get setup")

        setup_dict = _multiline_output_to_dict(lines)
        def get_filter_settings():
            """
            Parse special filter setting row.

            Example:
                dig.filter: none
                dig.filter:  38-350 kHz, order=4, stages=4
                dig.filter:  10-max kHz, order=4, stages=2
            """
            match_filter = re.search(
                rb"dig\.filter:\s+(?P<none>none)?((?P<hipass>\d+)-(?P<lopass>\d+|max) kHz, order=(?P<order>\d))?",
                b"".join(lines),
            )
            if match_filter.group("none"):
                return 0, self.CLOCK / 2, 0
            match_hipass = match_filter.group("hipass")
            match_lopass = match_filter.group("lopass")

            hipass_hz = float(match_hipass) * 1e3
            lopass_hz = float(match_lopass) * 1e3 if match_lopass != b"max" else self.CLOCK / 2
            order = int(match_filter.group("order"))
            return hipass_hz, lopass_hz, order

        filter_settings = get_filter_settings()

        return Setup(
            acq_enabled=_as_int(setup_dict["acq_enabled"]) == 1,
            cont_enabled=_as_int(setup_dict["cont"]) == 1,
            log_enabled=_as_int(setup_dict["log_enabled"]) == 1,
            adc_to_volts=_as_float(setup_dict["adc2uv"]) / 1e6,
            threshold_volts=_as_float(setup_dict["thr"]) / 1e6,
            ddt_seconds=_as_float(setup_dict["ddt"]) / 1e6,
            status_interval_seconds=_as_float(setup_dict["status_interval"]) / 1e3,
            filter_highpass_hz=filter_settings[0],
            filter_lowpass_hz=filter_settings[1],
            filter_order=filter_settings[2],
            tr_enabled=_as_int(setup_dict["tr_enabled"]) == 1,
            tr_decimation=_as_int(setup_dict["tr_decimation"]),
            tr_pretrigger_samples=_as_int(setup_dict["tr_pre_trig"]),
            tr_postduration_samples=_as_int(setup_dict["tr_post_dur"]),
            cct_seconds=_as_float(setup_dict["cct"]),
        )

    def get_status(self) -> Status:
        """
        Get status.

        Returns:
            Dataclass with status information
        """
        self._send_command("get_status")
        with self._timeout_context(0.1):
            lines = self._ser.readlines()
        if not lines:
            raise RuntimeError("Could not get status")

        status_dict = _multiline_output_to_dict(lines)
        return Status(
            device_id=status_dict["dev_id"],
            firmware_version=status_dict["fw_version"],
            temperature=_as_int(status_dict["temp"]),
            data_size=_as_int(status_dict["data size"]),
            datetime=datetime.strptime(status_dict["date"], "%Y-%m-%d %H:%M:%S.%f"),
        )

    def set_continuous_mode(self, enabled: bool):
        """
        Enable/disable continuous mode.

        Threshold will be ignored.
        The length of the records is determined by `ddt` with `set_ddt`.

        Args:
            enabled: Set to `True` to enable continuous mode
        """
        self._send_command(f"set_acq cont {int(enabled)}")

    def set_ddt(self, microseconds: int):
        """
        Set duration discrimination time (DDT).

        Args:
            microseconds: DDT in µs
        """
        self._send_command(f"set_acq ddt {int(microseconds)}")

    def set_status_interval(self, milliseconds: int):
        """
        Set status interval.

        Args:
            milliseconds: Status interval in ms
        """
        self._send_command(f"set_acq status_interval {int(milliseconds)}")

    def set_tr_enabled(self, enabled: bool):
        """
        Enable/disable recording of transient data.

        Args:
            enabled: Set to `True` to enable transient data
        """
        self._send_command(f"set_acq tr_enabled {int(enabled)}")

    def set_tr_decimation(self, factor: int):
        """
        Set decimation factor of transient data.

        The sampling rate of transient data will be 2 MHz / `factor`.

        Args:
            factor: Decimation factor
        """
        self._send_command(f"set_acq tr_decimation {int(factor)}")

    def set_tr_pretrigger(self, samples: int):
        """
        Set pre-trigger samples for transient data.

        Args:
            samples: Pre-trigger samples
        """
        self._send_command(f"set_acq tr_pre_trig {int(samples)}")

    def set_tr_postduration(self, samples: int):
        """
        Set post-duration samples for transient data.

        Args:
            samples: Post-duration samples
        """
        self._send_command(f"set_acq tr_post_dur {int(samples)}")

    def set_cct(self, interval_seconds: int, sync: bool = False):
        """
        Set coupling check ransmitter (CCT) / pulser interval.

        The pulser amplitude is 3.3 V.

        Args:
            interval_seconds: Pulser interval in seconds
            sync: Synchronize the pulser with the first sample of the `get_data` command
        """
        if sync and interval_seconds > 0:
            interval_seconds *= -1
        self._send_command(f"set_cct {interval_seconds}")

    def set_filter(self, highpass: float, lowpass: float, order: int = 4):
        """
        Set IIR filter frequencies and order.

        Args:
            highpass: Highpass frequency in kHz
            lowpass: Lowpass frequency in kHz
            order: Filter order
        """
        self._send_command(f"set_filter {int(highpass)} {int(lowpass)} {int(order)}")

    def set_datetime(self, timestamp: Optional[datetime] = None):
        """
        Set current date and time.

        Args:
            timestamp: `datetime.datetime` object, current time if `None`
        """
        if not timestamp:
            timestamp = datetime.now()
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        self._send_command(f"set_datetime {timestamp_str}")

    def set_threshold(self, microvolts: float):
        """
        Set threshold for hit-based acquisition.

        Args:
            microvolts: Threshold in µV
        """
        self._send_command(f"set_acq thr {microvolts}")

    def start_acquisition(self):
        """Start acquisition."""
        self._send_command("set_acq enabled 1")

    def stop_acquisition(self):
        """Stop acquisition."""
        self._send_command("set_acq enabled 0")

    def get_ae_data(self) -> Iterator[AERecord]:
        """
        Get AE data records.

        Yields:
            AE data records (either status or hit data)
        """
        self._send_command("get_ae_data")
        headerline = self._ser.readline()
        number_lines = int(headerline)

        for _ in range(number_lines):
            line = self._ser.readline().decode(errors="replace")
            logger.debug(f"Received AE data: {line}")

            record_type = line[:1]
            if record_type in ("H", "S"):  # hit or status data
                matches = re.match(
                    (
                        r"(?P<type>H|S) .* "
                        r"T=(?P<T>\d+) "
                        r"A=(?P<A>\d+) "
                        r"R=(?P<R>\d+) "
                        r"D=(?P<D>\d+) "
                        r"C=(?P<C>\d+) "
                        r"E=(?P<E>\d+) "
                        r"TRAI=(?P<TRAI>\d+) "
                        r"flags=(?P<flags>\d+)"
                    ),
                    line,
                )
                if not matches:
                    logger.error(f"Could not parse AE data: {line}")
                    break

                yield AERecord(
                    type_=matches.group("type"),
                    time=int(matches.group("T")) / self.CLOCK,
                    amplitude=int(matches.group("A")) * self._adc_to_volts,
                    rise_time=int(matches.group("R")) / self.CLOCK,
                    duration=int(matches.group("D")) / self.CLOCK,
                    counts=int(matches.group("C")),
                    energy=int(matches.group("E")) * self._adc_to_volts ** 2 * 1e14 / self.CLOCK,
                    trai=int(matches.group("TRAI")),
                    flags=int(matches.group("flags")),
                )
            else:
                logger.warning(f"Unknown AE data record: {line}")

    def get_tr_data(self) -> Iterator[TRRecord]:
        """
        Get transient data records.

        Yields:
            Transient data records
        """
        self._send_command("get_tr_data b")

        while True:
            headerline = self._ser.readline().decode(errors="replace")

            # parse header
            matches = re.match(
                r"TRAI=(?P<TRAI>\d+) T=(?P<T>\d+) NS=(?P<NS>\d+).*",
                headerline.upper(),
            )
            if not matches:  # last line is empty or 0
                logger.debug(f"Last TR headerline {headerline}")
                break

            trai = int(matches.group("TRAI"))
            time = int(matches.group("T"))
            samples = int(matches.group("NS"))
            data_adc = np.frombuffer(self._ser.read(2 * samples), dtype=np.int16)
            data_volts = np.multiply(data_adc, self._adc_to_volts, dtype=np.float32)

            if len(data_volts) != samples:
                raise RuntimeError(
                    f"TR data samples ({len(data_volts)}) do not match expected number ({samples})"
                )

            yield TRRecord(
                trai=trai,
                time=time / self.CLOCK,
                samples=samples,
                data=data_volts,
            )

    def get_data(self, samples: int) -> np.ndarray:
        """
        Read snapshot of transient data with maximum sampling rate (2 MHz).

        Args:
            samples: Number of samples to read

        Returns:
            Array with amplitudes in volts
        """
        samples = int(samples)
        self._send_command(f"get_data b {samples}")
        adc_values = np.frombuffer(self._ser.read(2 * samples), dtype=np.int16)
        return np.multiply(adc_values, self._adc_to_volts, dtype=np.float32)
