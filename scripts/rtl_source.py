"""
rtl_source.py — Native RTL-TCP client (no local librtlsdr required)

Implements the RTL-TCP protocol directly over TCP sockets.
Allows connection to a remote or local RTL-TCP server without needing
the RTL-SDR dongle to be physically connected to this machine.

RTL-TCP Protocol (binary, big-endian):
    Server → client: 12 bytes of "magic" upon connection
        [0:4]  = b'RTL0'
        [4:8]  = tuner_type  (uint32 BE)
        [8:12] = gain_count  (uint32 BE)
    Then: continuous stream of uint8 pairs [I, Q, I, Q, ...]

    Client → server: 5-byte commands
        [0]   = cmd_id (uint8)
        [1:5] = argument (uint32 BE)

Main Commands:
    0x01 SET_FREQUENCY      (Hz)
    0x02 SET_SAMPLE_RATE    (Hz)
    0x03 SET_GAIN_MODE      (0=auto/Tuner-AGC, 1=manual)
    0x04 SET_GAIN           (tenths of dB, e.g., 400 = 40.0 dB)
    0x05 SET_FREQ_CORRECTION (ppm, int32)
    0x08 SET_AGC_MODE       (0=off, 1=on — RTL2832U digital AGC)
    0x0d SET_BIAS_TEE       (0/1)
"""

import socket
import struct
import numpy as np
import threading
import queue
import logging
import time

log = logging.getLogger(__name__)

# RTL-TCP Commands
CMD_SET_FREQ       = 0x01
CMD_SET_SAMP_RATE  = 0x02
CMD_SET_GAIN_MODE  = 0x03   # Tuner AGC: 0=auto, 1=manual
CMD_SET_GAIN       = 0x04
CMD_SET_FREQ_CORR  = 0x05
CMD_SET_AGC_MODE   = 0x08   # RTL2832U digital AGC: 0=off, 1=on
CMD_SET_BIAS_TEE   = 0x0d


class RtlTcpSource:
    """
    RTL-TCP Client. Reads IQ samples and puts them in a queue for
    the DSP thread.

    Usage
    ---
        src = RtlTcpSource(host='192.168.1.10', port=1234,
                           freq=162_450_000, sample_rate=250_000, gain=40)
        src.start(iq_queue)   # starts filling the queue
        ...
        src.stop()
    """

    # Chunk size in bytes (multiple of 2 for complete IQ pair)
    # 32768 bytes = 16384 IQ samples ≈ 65 ms @ 250 kHz
    CHUNK_BYTES = 32_768

    def __init__(self,
                 host:        str = 'localhost',
                 port:        int = 1234,
                 freq:        int = 162_450_000,  # Hz — Teuhitl SASMEX
                 sample_rate: int = 250_000,       # Hz
                 gain:        float = 40.0,        # dB (0 = Automatic Tuner AGC)
                 ppm:         int = 0,
                 tuner_agc:   bool = False,        # True → SET_GAIN_MODE=0 (auto)
                 rtl_agc:     bool = False):       # True → SET_AGC_MODE=1 (RTL digital AGC)
        self.host        = host
        self.port        = port
        self.freq        = freq
        self.sample_rate = sample_rate
        self.gain        = gain
        self.ppm         = ppm
        self.tuner_agc   = tuner_agc
        self.rtl_agc     = rtl_agc

        self._sock    = None
        self._thread  = None
        self._running = False

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self, iq_queue: queue.Queue):
        """Connect to server and start reading IQ in a background thread."""
        self._queue   = iq_queue
        self._running = True
        
        gain_str = ('auto/Tuner-AGC' if (self.tuner_agc or self.gain == 0)
                    else f'{self.gain} dB')
        rtl_str  = 'RTL-AGC=ON' if self.rtl_agc else 'RTL-AGC=OFF'
        log.info(f'RTL-TCP reader starting connection thread → {self.host}:{self.port} '
                 f'freq={self.freq/1e6:.3f} MHz  sr={self.sample_rate} Hz  '
                 f'gain={gain_str}  {rtl_str}')
                 
        self._thread = threading.Thread(target=self._reader_loop,
                                        name='rtltcp-reader', daemon=True)
        self._thread.start()

    def stop(self):
        """Stop reading and close the socket."""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Hot-swapping control (thread-safe while reader is running)
    # ------------------------------------------------------------------

    def set_freq(self, hz: int) -> None:
        """Change center frequency without stopping the stream."""
        self.freq = hz
        if self._sock:
            try:
                self._send_cmd(CMD_SET_FREQ, hz)
                log.info(f'Frequency → {hz / 1e6:.3f} MHz')
            except Exception as e:
                log.warning(f'set_freq: {e}')

    def set_gain(self, db: float) -> None:
        """Change gain without stopping the stream. 0.0 is a valid manual value."""
        self.gain = db
        if self._sock:
            try:
                # Always ensure manual mode (1) when setting a discrete gain
                self._send_cmd(CMD_SET_GAIN_MODE, 1)
                self._send_cmd(CMD_SET_GAIN, int(db * 10))
                log.info(f'Gain → {db} dB')
            except Exception as e:
                log.warning(f'set_gain: {e}')

    def set_tuner_agc(self, enabled: bool) -> None:
        """Enable or disable Tuner AGC without stopping the stream."""
        self.tuner_agc = enabled
        if self._sock:
            try:
                if enabled:
                    self._send_cmd(CMD_SET_GAIN_MODE, 0)
                    log.info('Tuner AGC → ON')
                else:
                    self._send_cmd(CMD_SET_GAIN_MODE, 1)
                    self._send_cmd(CMD_SET_GAIN, int(self.gain * 10))
                    log.info(f'Tuner AGC → OFF gain = {self.gain} dB')
            except Exception as e:
                log.warning(f'set_tuner_agc: {e}')

    def set_ppm(self, ppm: int) -> None:
        """Change PPM correction without stopping the stream."""
        self.ppm = ppm
        if self._sock:
            try:
                self._send_cmd(CMD_SET_FREQ_CORR, ppm)
                log.info(f'PPM → {ppm}')
            except Exception as e:
                log.warning(f'set_ppm: {e}')

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Timeout on connect + recv so a silent server hang is detected and
        # triggers the reconnect loop rather than blocking forever.
        self._sock.settimeout(15.0)
        # TCP keep-alive so the OS detects a dead connection even when no
        # data is in flight (helps on long-running sessions).
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock.connect((self.host, self.port))
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Read magic header (12 bytes)
        magic = self._recv_exact(12)
        if magic[:4] != b'RTL0':
            raise ConnectionError(f'Invalid RTL-TCP magic: {magic[:4]}')
        tuner_type  = struct.unpack('>I', magic[4:8])[0]
        gain_count  = struct.unpack('>I', magic[8:12])[0]
        log.info(f'RTL-TCP connected: tuner_type={tuner_type}  gain_count={gain_count}')

        # Configure dongle
        self._send_cmd(CMD_SET_SAMP_RATE, self.sample_rate)
        self._send_cmd(CMD_SET_FREQ,      self.freq)
        self._send_cmd(CMD_SET_FREQ_CORR, self.ppm)

        # ── Tuner AGC ──────────────────────────────────────────────────────────
        # tuner_agc=True  OR  gain==0  →  automatic mode (tuner chooses)
        # Any other combination        →  manual mode + gain value
        if self.tuner_agc or self.gain == 0:
            self._send_cmd(CMD_SET_GAIN_MODE, 0)   # 0 = auto / Tuner-AGC on
            log.info('Tuner AGC: ON (automatic)')
        else:
            self._send_cmd(CMD_SET_GAIN_MODE, 1)               # 1 = manual
            self._send_cmd(CMD_SET_GAIN, int(self.gain * 10))  # tenths of dB
            log.info(f'Tuner AGC: OFF manual gain = {self.gain} dB')

        # ── RTL AGC (digital, RTL2832U) ────────────────────────────────────────
        if self.rtl_agc:
            self._send_cmd(CMD_SET_AGC_MODE, 1)   # 1 = RTL AGC on
            log.info('RTL AGC: ON')
        else:
            self._send_cmd(CMD_SET_AGC_MODE, 0)   # 0 = RTL AGC off
            log.info('RTL AGC: OFF')

    def _send_cmd(self, cmd: int, arg: int):
        """Send 5-byte RTL-TCP command."""
        # arg as uint32 big-endian (handle negative as int32→uint32)
        if arg < 0:
            arg = arg & 0xFFFFFFFF
        pkt = struct.pack('>BI', cmd, arg)
        self._sock.sendall(pkt)

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes from socket."""
        buf = b''
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('RTL-TCP: connection closed by server')
            buf += chunk
        return buf

    def _reader_loop(self):
        """Reader thread: reads IQ continuously, if fails, tries to reconnect."""
        while self._running:
            try:
                if self._sock is None:
                    self._connect()

                raw = self._recv_exact(self.CHUNK_BYTES)

                # Convert uint8 → complex64
                # RTL-TCP protocol sends unsigned bytes centered at 127.5
                arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
                arr = (arr - 127.5) / 127.5          # normalize to ±1.0
                iq  = arr[0::2] + 1j * arr[1::2]     # interleaved I/Q → complex

                # Put in queue (blocks if full → natural back-pressure)
                try:
                    self._queue.put_nowait(iq.astype(np.complex64))
                except queue.Full:
                    # Catch-up policy: discard oldest to maintain real-time
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(iq.astype(np.complex64))
                    except:
                        pass

            except Exception as e:
                if self._running:
                    log.error(f'RTL-TCP reader error: {e}. Retrying in 5s...')
                    if self._sock:
                        try:
                            self._sock.close()
                        except:
                            pass
                        self._sock = None
                    
                    # Wait 5 seconds, checking frequently if program stopped
                    for _ in range(50):
                        if not self._running:
                            break
                        time.sleep(0.1)

        # End of thread
        if self._sock:
            try:
                self._sock.close()
            except:
                pass
            self._sock = None
