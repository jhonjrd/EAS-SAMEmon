"""
local_source.py — IQ Source from local USB RTL-SDR

Uses pyrtlsdr (librtlsdr wrapper) to read IQ samples directly
from the USB-connected RTL-SDR dongle, without needing rtl_tcp or any
external process.

Requirements:
    pip install pyrtlsdr
    + librtlsdr.dll in PATH (included with SDR# driver package
      or the rtl-sdr-blog release pack)

Driver Installation (Windows):
    1. Download https://github.com/rtlsdrblog/rtl-sdr-blog/releases
       → extract rtl_sdr.dll / librtlsdr.dll to project directory
         or to C:\\Windows\\System32
    2. Connect dongle and use Zadig to install WinUSB

Public Interface — same as RtlTcpSource:
    src = PyRtlSdrSource(device_index=0, freq=162_450_000, ...)
    src.start(iq_queue)
    ...
    src.stop()
"""

import queue
import threading
import logging
import time
import numpy as np

log = logging.getLogger(__name__)

# Complex samples per chunk read from dongle
# 16,384 complex → 32,768 uint8 → ~65 ms @ 250 kHz
CHUNK_SAMPLES = 16_384


class PyRtlSdrSource:
    """
    Reads IQ samples from local USB RTL-SDR using pyrtlsdr.

    Parameters
    ----------
    device_index : int
        Dongle index (0 if only one connected). Ignored when *serial* is set.
    serial : str or None
        EEPROM serial number of the dongle (e.g. ``"00000001"``). When provided,
        the dongle is opened by serial instead of by index, which is useful in
        multi-dongle setups where USB enumeration order may change across reboots.
    freq : int
        Center frequency in Hz (e.g., 162_450_000).
    sample_rate : int
        Sample rate in Hz (e.g., 250_000).
    gain : float
        Gain in dB. 0.0 = Automatic Tuner AGC.
    ppm : int
        Frequency error correction in ppm.
    tuner_agc : bool
        If True, activates tuner AGC (same as gain=0).
    rtl_agc : bool
        If True, activates internal RTL2832U digital AGC.
    """

    def __init__(self,
                 device_index: int = 0,
                 serial:       str | None = None,
                 freq:         int   = 162_450_000,
                 sample_rate:  int   = 250_000,
                 gain:         float = 40.0,
                 ppm:          int   = 0,
                 tuner_agc:    bool  = False,
                 rtl_agc:      bool  = False):
        self.device_index = device_index
        self.serial       = serial
        self.freq         = freq
        self.sample_rate  = sample_rate
        self.gain         = gain
        self.ppm          = ppm
        self.tuner_agc    = tuner_agc
        self.rtl_agc      = rtl_agc

        self._sdr     = None
        self._thread  = None
        self._running = False

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self, iq_queue: queue.Queue):
        """Open dongle and start reading IQ in a background thread."""
        try:
            from rtlsdr import RtlSdr
        except ImportError as e:
            import platform
            is_linux = platform.system() == 'Linux'
            
            # Try to diagnose if it's the package, system library, or setuptools issue
            msg_extra = ""
            try:
                import rtlsdr
                pkg_installed = True
            except ImportError as ie:
                pkg_installed = False
                # Capture specific pkg_resources error (common in Python 3.12+)
                if "pkg_resources" in str(ie) or "ModuleNotFoundError" in str(ie):
                    msg_extra = ("\n[DEPENDENCY ERROR] Missing 'setuptools'. Legacy pyrtlsdr versions require 'pkg_resources'.\n"
                                 "  → Solution: pip install setuptools\n")

            if msg_extra:
                msg = msg_extra
            elif not pkg_installed:
                msg = ("\n[ERROR] The 'pyrtlsdr' package is not installed in this Python environment.\n"
                       "  → Command: pip install pyrtlsdr==0.2.93\n")
            else:
                if is_linux:
                    msg = ("\n[ERROR] 'pyrtlsdr' package was found, but system library 'librtlsdr.so' could not be loaded.\n"
                           "  → Cause: Base driver is not installed or not accessible.\n"
                           "  → Solution: Ask administrator to install 'librtlsdr-dev' or 'librtlsdr0'.\n"
                           "  → Additional diagnostics: python3 -c \"import ctypes; print(ctypes.util.find_library('rtlsdr'))\"\n")
                else:
                    msg = ("\n[ERROR] 'librtlsdr.dll' library is missing from PATH or project directory.\n"
                           "  → Solution: Download drivers from rtl-sdr.blog and copy .dll here.\n")
            
            raise ImportError(msg) from e

        try:
            if self.serial:
                self._sdr = RtlSdr(serial_number=self.serial)
            else:
                self._sdr = RtlSdr(device_index=self.device_index)
        except Exception as e:
            err_msg = str(e).lower()
            if "undefined symbol" in err_msg or "rtlsdr_set_dithering" in err_msg or "entry point" in err_msg or "procedure not found" in err_msg:
                raise RuntimeError(
                    "\n[COMPATIBILITY ERROR] Your librtlsdr driver is too old for this pyrtlsdr version.\n"
                    "  → Cause: System library lacks 'rtlsdr_set_dithering'.\n"
                    "  → Solution (Managed Linux): Ensure you use pyrtlsdr==0.2.93 in your venv.\n"
                    "  → Command: pip install pyrtlsdr==0.2.93\n"
                ) from e
            raise e

        # Configure dongle
        self._sdr.sample_rate  = self.sample_rate
        self._sdr.center_freq  = self.freq
        
        if self.ppm != 0:
            try:
                self._sdr.freq_correction = self.ppm
            except Exception as e:
                log.warning(f"Could not configure freq_correction ({self.ppm} PPM): {e}")
        else:
            log.info("PPM is 0, skipping freq_correction config (stability)")

        # Gain / Tuner AGC
        if self.tuner_agc or self.gain == 0:
            self._sdr.gain = 'auto'
            log.info('Tuner AGC: ON (automatic)')
        else:
            self._sdr.gain = self.gain
            log.info(f'Tuner AGC: OFF manual gain = {self.gain} dB')

        # RTL AGC (internal digital RTL2832U)
        # Some legacy drivers fail here too
        try:
            self._sdr.set_agc_mode(1 if self.rtl_agc else 0)
        except Exception:
            log.warning("Could not configure RTL AGC (possible driver incompatibility)")

        log.info(f'RTL AGC: {"ON" if self.rtl_agc else "OFF"}')

        gain_str = ('auto/Tuner-AGC' if (self.tuner_agc or self.gain == 0)
                    else f'{self.gain} dB')
        rtl_str  = 'RTL-AGC=ON' if self.rtl_agc else 'RTL-AGC=OFF'
        device_label = (f'serial={self.serial}' if self.serial
                        else f'index={self.device_index}')
        log.info(
            f'PyRtlSdrSource started — device [{device_label}]  '
            f'freq={self.freq/1e6:.3f} MHz  sr={self.sample_rate:,} Hz  '
            f'gain={gain_str}  {rtl_str}'
        )

        self._queue   = iq_queue
        self._running = True
        self._thread  = threading.Thread(target=self._reader_loop,
                                         name='pyrtlsdr-reader', daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the reader thread to stop and wait for it to exit cleanly.

        IMPORTANT: Do NOT call self._sdr.close() here.
        libusb is not thread-safe when closing a device from a different thread
        while a transfer (read_samples) is in progress on the reader thread —
        this triggers a pthread_mutex_lock assertion crash inside libusb.
        The reader thread's cleanup block already calls close() safely from
        its own context once _running is False and the current read finishes.
        """
        self._running = False
        if self._thread and self._thread.is_alive():
            # At 250 kHz / 16 384 samples per chunk ≈ 65 ms per read.
            # The reader thread will notice _running=False within one chunk
            # and close the device itself.  3 s timeout is a generous safety net.
            self._thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    # Hot-swapping control
    # ------------------------------------------------------------------

    def set_freq(self, hz: int) -> None:
        """Change center frequency without stopping the stream."""
        self.freq = hz
        if self._sdr:
            try:
                self._sdr.center_freq = hz
                log.info(f'Frequency → {hz / 1e6:.3f} MHz')
            except Exception as e:
                log.warning(f'set_freq: {e}')

    def set_gain(self, db: float) -> None:
        """Change gain without stopping the stream. 0.0 is a valid manual value."""
        self.gain = db
        if self._sdr:
            try:
                # Explicitly disable tuner AGC (enable manual gain)
                self._sdr.set_manual_gain_enabled(True)
                self._sdr.gain = db
                log.info(f'Gain → {db} dB (manual)')
            except Exception as e:
                log.warning(f'set_gain: {e}')

    def set_tuner_agc(self, enabled: bool) -> None:
        """Enable or disable Tuner AGC without stopping the stream."""
        self.tuner_agc = enabled
        if self._sdr:
            try:
                if enabled:
                    self._sdr.gain = 'auto'
                    log.info('Tuner AGC → ON')
                else:
                    self._sdr.gain = self.gain
                    log.info(f'Tuner AGC → OFF gain = {self.gain} dB')
            except Exception as e:
                log.warning(f'set_tuner_agc: {e}')

    def set_ppm(self, ppm: int) -> None:
        """Change frequency correction in PPM without stopping the stream."""
        self.ppm = ppm
        if self._sdr:
            try:
                self._sdr.freq_correction = ppm
                log.info(f'PPM → {ppm}')
            except Exception as e:
                log.warning(f'set_ppm: {e}')

    # ------------------------------------------------------------------
    # Reader Thread
    # ------------------------------------------------------------------

    def _reopen(self):
        """
        Re-open and reconfigure the dongle after a dropout.
        Called automatically by _reader_loop when self._sdr is None.
        The full import-error diagnostics live in start(); here we just
        reconnect assuming the package is already available.
        """
        from rtlsdr import RtlSdr
        device_label = (f'serial={self.serial}' if self.serial
                        else f'index={self.device_index}')
        log.info(f'PyRtlSdrSource: reopening device [{device_label}]...')
        if self.serial:
            self._sdr = RtlSdr(serial_number=self.serial)
        else:
            self._sdr = RtlSdr(device_index=self.device_index)
        self._sdr.sample_rate = self.sample_rate
        self._sdr.center_freq = self.freq
        if self.ppm != 0:
            try:
                self._sdr.freq_correction = self.ppm
            except Exception as e:
                log.warning(f'freq_correction ({self.ppm} PPM): {e}')
        if self.tuner_agc or self.gain == 0:
            self._sdr.gain = 'auto'
        else:
            self._sdr.gain = self.gain
        try:
            self._sdr.set_agc_mode(1 if self.rtl_agc else 0)
        except Exception:
            pass
        log.info(f'PyRtlSdrSource: device [{device_label}] reopened successfully')

    def _reader_loop(self):
        """
        Reads CHUNK_SAMPLES complex samples from the dongle
        and puts them in the IQ queue.

        pyrtlsdr returns normalized complex128 ≈ ±1.0 (already converts
        uint8 → float which RtlTcpSource does manually).
        We cast it to complex64 for uniformity with the rest of the pipeline.

        On error (USB dropout, buffer overflow, driver hiccup) the dongle is
        closed, and the loop retries after 5 s — mirroring RtlTcpSource.
        """
        while self._running:
            try:
                if self._sdr is None:
                    self._reopen()

                samples = self._sdr.read_samples(CHUNK_SAMPLES)
                iq = np.asarray(samples, dtype=np.complex64)
                try:
                    self._queue.put_nowait(iq)
                except queue.Full:
                    # Catch-up policy: discard oldest to maintain real-time
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(iq)
                    except Exception:
                        pass

            except Exception as e:
                if self._running:
                    log.error(f'PyRtlSdrSource error: {e}. Retrying in 5s...')
                    if self._sdr:
                        try:
                            self._sdr.close()
                        except Exception:
                            pass
                        self._sdr = None

                    # Wait 5 s, bailing out immediately if stop() is called
                    for _ in range(50):
                        if not self._running:
                            break
                        time.sleep(0.1)

        # Thread exit — ensure dongle is released
        if self._sdr:
            try:
                self._sdr.close()
            except Exception:
                pass
            self._sdr = None

    # ------------------------------------------------------------------
    # Static Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _load_librtlsdr():
        """
        Return a ctypes handle to librtlsdr.

        Tries three paths in order:
          1. The ctypes object already loaded inside pyrtlsdr (avoids double-load).
          2. ctypes.util.find_library() system search.
          3. Hard-coded platform-specific names as a last resort.
        """
        import ctypes, ctypes.util, platform

        # Path 1 — grab the already-loaded ctypes object from pyrtlsdr internals
        try:
            import rtlsdr.rtlsdr as _mod
            lib = getattr(_mod, 'librtlsdr', None)
            if lib is not None:
                return lib
        except Exception:
            pass

        # Path 2 / 3 — load the shared library directly
        sys_name = platform.system()
        candidates: list[str] = []
        found = ctypes.util.find_library('rtlsdr')
        if found:
            candidates.append(found)
        if sys_name == 'Windows':
            candidates += ['rtlsdr', 'librtlsdr', 'rtl_sdr']
        else:
            candidates += ['librtlsdr.so.0', 'librtlsdr.so']

        for name in candidates:
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue

        return None

    @staticmethod
    def list_devices() -> list[dict]:
        """List connected RTL-SDR dongles with index, name, and EEPROM serial.

        Loads librtlsdr via ctypes directly so it works regardless of which
        Python class (RtlSdr / RtlSdrAio) the installed pyrtlsdr version exposes.
        """
        try:
            import ctypes
            from ctypes import c_char_p, c_ubyte

            lib = PyRtlSdrSource._load_librtlsdr()
            if lib is None:
                log.warning('librtlsdr shared library not found')
                return []

            lib.rtlsdr_get_device_name.restype = c_char_p
            UBuf = c_ubyte * 256
            count = lib.rtlsdr_get_device_count()
            result = []
            for i in range(count):
                raw = lib.rtlsdr_get_device_name(i)
                name = raw.decode('utf-8', errors='replace') if raw else f'Device {i}'
                manufact = UBuf()
                product  = UBuf()
                serial   = UBuf()
                lib.rtlsdr_get_device_usb_strings(i, manufact, product, serial)
                serial_str = bytes(serial).rstrip(b'\x00').decode('utf-8', errors='replace')
                result.append({'index': i, 'name': name, 'serial': serial_str})
            return result
        except ImportError:
            log.warning('pyrtlsdr not available')
            return []
        except Exception as e:
            log.warning(f'Error listing devices: {e}')
            return []
