"""
audio_monitor.py — FM Audio Monitor for EAS-SAMEmon

Provides three simultaneous monitoring layers:
  1. Real-time playback via sounddevice
  2. Signal metrics: RMS level, estimated SNR, FM deviation, squelch
  3. Optional recording to WAV

Usage:
    monitor = AudioMonitor(device=None, record_path=None)
    monitor.start()
    monitor.feed(audio_float32)    # call with each chunk from fm_demod
    metrics = monitor.get_metrics()
    monitor.stop()

Anti-clipping:
    - Lockless ring buffer (circular array + atomic indices) instead of Queue
    - Pre-buffering: sounddevice waits for PREBUFFER_BLOCKS blocks before
      starting output to avoid initial buffer underruns
    - Soft tanh limiter: compresses peaks without hard clipping
    - Larger BLOCK_SIZE (4096) to reduce callback jitter
"""

import wave
import os
import datetime
import threading
import logging
import numpy as np
from collections import deque
from math import gcd
from scipy.signal import resample_poly

log = logging.getLogger(__name__)

AUDIO_RATE     = 25000       # Hz — EAS demodulator input (must match fm_demod.py)
PLAYBACK_RATE  = 48000       # Hz — sounddevice output (standard rate supported by all hardware)
BLOCK_SIZE     = 4096        # samples per sounddevice block (higher → less jitter)
METRICS_WINDOW = 10          # blocks to average metrics (~0.5 s)

# On low-power ARM devices (OrangePi, RPi) the FFT inside _compute_metrics is
# expensive (non-power-of-2 input, ~65 ms period).  Running it every N chunks
# keeps the signal meter responsive (~325 ms update at N=5) while freeing the
# DSP thread for audio streaming.  RMS and deviation still run every chunk.
METRICS_FFT_EVERY = 5        # run the FFT part of metrics every N feed() calls

# Squelch threshold calibrated for NWR signals with ×4 gain in FMDemod:
# - Silent carrier:  rms ~0.009  → -41 dBFS  → squelch CLOSED ✓
# - Active EAS message:    rms ~0.105  → -20 dBFS  → squelch OPEN ✓
# Threshold at -28 dBFS: 13 dB above silence floor → sufficient margin.
SQUELCH_THRESHOLD_DBFS = -28.0

# Ring buffer: block capacity and pre-buffering
RING_CAPACITY   = 64        # max blocks in ring (~12 s @ 4096 samples)
PREBUFFER_BLOCKS = 4        # blocks to accumulate before starting playback

# Soft limiter: tanh(audio * DRIVE) / tanh(DRIVE) → ±0.95 headroom
_TANH_DRIVE = 3.0
_TANH_NORM  = float(np.tanh(_TANH_DRIVE))


def _soft_limit(audio: np.ndarray) -> np.ndarray:
    """Soft limiter based on tanh. Does not clip, smoothly compresses peaks."""
    return (np.tanh(audio * _TANH_DRIVE) / _TANH_NORM).astype(np.float32)


class _RingBuffer:
    """
    Ring buffer SPSC (single-producer / single-consumer) for float32.

    The producer (DSP thread) calls put(); the consumer (sounddevice callback)
    calls get(). Uses two lockless integer indices (CPython's GIL ensures
    that assignments to ints are atomic in the CPython implementation).
    """

    def __init__(self, capacity: int, block_size: int):
        self._cap   = capacity
        self._bsize = block_size
        self._buf   = np.zeros((capacity, block_size), dtype=np.float32)
        self._write = 0    # write index (advanced by producer)
        self._read  = 0    # read index (advanced by consumer)

    def _used(self) -> int:
        return (self._write - self._read) % self._cap

    def full(self) -> bool:
        return self._used() >= self._cap - 1

    def empty(self) -> bool:
        return self._write == self._read

    def available(self) -> int:
        return self._used()

    def put(self, block: np.ndarray) -> bool:
        """Writes a block. Returns False if buffer is full (discards)."""
        if self.full():
            return False
        self._buf[self._write % self._cap] = block[: self._bsize]
        self._write += 1
        return True

    def get(self, out: np.ndarray) -> bool:
        """Reads a block into out. Returns False if empty (silence)."""
        if self.empty():
            return False
        out[:] = self._buf[self._read % self._cap]
        self._read += 1
        return True


class AudioMonitor:
    """
    FM Audio Monitor.

    Parameters
    ----------
    device : int | str | None
        Sounddevice output device. None = system default.
    record_path : str | None
        WAV file path for recording. None = do not record.
    volume : float
        Playback volume 0.0–1.0 (default 0.8).
    squelch : float
        Squelch threshold in dBFS (default -28). Weaker signals are muted.
    squelch_enabled : bool
        If False, ignores squelch and always plays.
    """

    def __init__(self,
                 device=None,
                 record_path: str | None = None,
                 volume: float = 0.8,
                 enable_playback: bool = True):
        self.device          = device
        self.record_path     = record_path
        self.volume          = max(0.0, min(1.0, volume))
        self.enable_playback = enable_playback   # False → no local output (metrics + web streaming only)

        self._stream       = None
        self._wav_file     = None
        self._ring         = _RingBuffer(RING_CAPACITY, BLOCK_SIZE)
        self._prebuffering = True   # True until PREBUFFER_BLOCKS accumulate
        self._running      = False

        # Callback for audio streaming to browser: callable(bytes) | None
        # Receives limited PCM float32 (without local volume) when squelch is open.
        self._audio_stream_cb = None

        # Metrics history (deque for sliding window)
        self._rms_history  = deque(maxlen=METRICS_WINDOW)
        self._dev_history  = deque(maxlen=METRICS_WINDOW)
        self._snr_history  = deque(maxlen=METRICS_WINDOW)
        self._metrics_lock = threading.Lock()

        # Current metrics (read by display)
        self.level_dbfs   = -90.0
        self.snr_db       = 0.0
        self.deviation    = 0.0

        # ARM optimisation: throttle the expensive FFT part of metrics.
        # RMS and deviation still update every chunk; SNR updates every N chunks.
        self._metrics_feed_count = 0
        self._hanning_cache: dict[int, np.ndarray] = {}  # keyed by chunk length

        # Accumulator fragment: DSP delivers variable-sized chunks;
        # here we assemble them into blocks of exactly BLOCK_SIZE for the ring.
        self._accum = np.zeros(0, dtype=np.float32)

        # Resampling parameters AUDIO_RATE → PLAYBACK_RATE (playback only)
        _g = gcd(PLAYBACK_RATE, AUDIO_RATE)
        self._rs_up   = PLAYBACK_RATE // _g
        self._rs_down = AUDIO_RATE    // _g

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Initialize audio stream and recording file."""
        self._running      = True
        self._prebuffering = True

        # Open sounddevice stream (only if enable_playback=True)
        if self.enable_playback:
            try:
                import sounddevice as sd
                self._stream = sd.OutputStream(
                    samplerate=PLAYBACK_RATE,
                    channels=1,
                    dtype='float32',
                    blocksize=BLOCK_SIZE,
                    device=self.device,
                    callback=self._audio_callback,
                    finished_callback=self._stream_finished,
                )
                self._stream.start()
                dev_info = sd.query_devices(self.device or sd.default.device[1])
                log.info(f'Audio → {dev_info["name"]}  @ {PLAYBACK_RATE} Hz  block={BLOCK_SIZE}')
            except Exception as e:
                log.warning(f'Could not open audio device: {e}')
                self._stream = None

        # Open WAV file for recording
        if self.record_path:
            try:
                self._wav_file = wave.open(self.record_path, 'wb')
                self._wav_file.setnchannels(1)
                self._wav_file.setsampwidth(2)        # 16-bit
                self._wav_file.setframerate(AUDIO_RATE)
                log.info(f'Recording audio → {self.record_path}')
            except Exception as e:
                log.warning(f'Could not open recording file: {e}')
                self._wav_file = None

    def stop(self):
        """Stop stream and close files."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        if self._wav_file:
            try:
                self._wav_file.close()
                log.info(f'Recording saved: {self.record_path}')
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Web audio streaming
    # ------------------------------------------------------------------

    def set_audio_stream_callback(self, cb) -> None:
        """
        Register function that receives PCM frames for browser streaming.

        The callback receives `bytes` (float32 LE, mono 25000 Hz, post-limiter,
        without local volume applied) each time the squelch is open.
        Call with cb=None to unregister.
        """
        self._audio_stream_cb = cb

    # ------------------------------------------------------------------
    # Feed audio (called from DSPWorker)
    # ------------------------------------------------------------------

    def feed(self, audio: np.ndarray):
        """
        Receive float32 @ 25000 Hz PCM audio chunk from fm_demod.
        Computes metrics, applies squelch + soft limiter, enqueues for
        playback, and records.
        """
        if not self._running:
            return

        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) == 0:
            return

        # --- Compute metrics (throttled FFT on ARM, RMS always) ---
        self._metrics_feed_count += 1
        run_fft = (self._metrics_feed_count % METRICS_FFT_EVERY == 0)
        self._compute_metrics(audio, run_fft=run_fft)

        # --- Soft limiter (computed once to reuse) ---
        limited = _soft_limit(audio)

        # --- Web audio streaming (pre-local volume) ---
        if self._audio_stream_cb:
            self._audio_stream_cb(limited.tobytes())

        # --- Apply volume for local output ---
        out = (limited * self.volume).astype(np.float32)

        # --- Accumulate and slice into BLOCK_SIZE blocks for the ring ---
        if self._stream:
            # Resample from AUDIO_RATE (25000) → PLAYBACK_RATE (48000) for hardware
            out_pb = resample_poly(out, self._rs_up, self._rs_down).astype(np.float32)
            self._accum = np.concatenate([self._accum, out_pb])
            while len(self._accum) >= BLOCK_SIZE:
                block = self._accum[:BLOCK_SIZE]
                self._accum = self._accum[BLOCK_SIZE:]

                if self._ring.full():
                    log.debug('Ring buffer full — discarding block')
                else:
                    self._ring.put(block)

                # End pre-buffering when sufficient blocks are available
                if self._prebuffering and self._ring.available() >= PREBUFFER_BLOCKS:
                    self._prebuffering = False
                    log.debug(f'Pre-buffering complete ({PREBUFFER_BLOCKS} blocks)')

        # --- Record to WAV (int16) — record unlimited audio for maximum fidelity ---
        if self._wav_file:
            try:
                pcm16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
                self._wav_file.writeframes(pcm16.tobytes())
            except Exception as e:
                log.warning(f'Recording error: {e}')

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, audio: np.ndarray, run_fft: bool = True):
        """Compute RMS level, SNR, and FM deviation of the chunk.

        run_fft=False skips the expensive FFT/SNR computation (ARM optimisation).
        RMS and deviation are always updated every call.
        """

        # 1. RMS level in dBFS (always computed — cheap O(N))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        rms_dbfs = 20 * np.log10(rms + 1e-10)

        # 2. Normalized FM deviation (always computed — cheap O(N))
        deviation = float(np.percentile(np.abs(audio), 95))

        snr_db = None
        if run_fft:
            # 3. Estimated SNR (frequency domain) — throttled on ARM
            N = len(audio)
            # Cache hanning window by length to avoid recomputing sin/cos every call
            if N not in self._hanning_cache:
                self._hanning_cache[N] = np.hanning(N).astype(np.float32)
            win = self._hanning_cache[N]
            fft = np.abs(np.fft.rfft(audio * win)) ** 2
            freqs = np.fft.rfftfreq(N, 1.0 / AUDIO_RATE)

            signal_mask = (freqs >= 300)  & (freqs <= 3400)
            noise_mask  = (freqs >= 5000) & (freqs <= 8000)

            p_signal = float(np.mean(fft[signal_mask])) if signal_mask.any() else 1e-10
            p_noise  = float(np.mean(fft[noise_mask]))  if noise_mask.any() else 1e-10
            snr_db = 10 * np.log10(max(p_signal / (p_noise + 1e-10), 1e-10))

        with self._metrics_lock:
            self._rms_history.append(rms_dbfs)
            self._dev_history.append(deviation)
            if snr_db is not None:
                self._snr_history.append(snr_db)

            self.level_dbfs = float(np.mean(self._rms_history))
            self.deviation  = float(np.mean(self._dev_history))
            self.snr_db     = float(np.mean(self._snr_history)) if self._snr_history else 0.0

    def get_metrics(self) -> dict:
        """Returns snapshot of current metrics (thread-safe)."""
        with self._metrics_lock:
            return {
                'level_dbfs':   round(self.level_dbfs, 1),
                'snr_db':       round(self.snr_db, 1),
                'deviation':    round(self.deviation, 3),
            }

    # ------------------------------------------------------------------
    # Internal sounddevice callback
    # ------------------------------------------------------------------

    def _audio_callback(self, outdata, frames, time_info, status):
        """
        Called by sounddevice in its internal thread. Must be very fast.

        If pre-buffering has not finished, or ring is empty, delivers
        silence (avoids underrun clicks).
        """
        if self._prebuffering or not self._ring.get(outdata[:, 0]):
            outdata[:, 0] = 0.0

    def _stream_finished(self):
        log.debug('Audio stream finished')

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict]:
        """Returns list of available output devices."""
        try:
            import sounddevice as sd
            devs = sd.query_devices()
            default_out = sd.default.device[1]
            result = []
            for i, d in enumerate(devs):
                if d['max_output_channels'] > 0:
                    result.append({
                        'index':   i,
                        'name':    d['name'],
                        'default': i == default_out,
                    })
            return result
        except Exception as e:
            log.warning(f'sounddevice not available: {e}')
            return []

    @staticmethod
    def level_bar(dbfs: float, width: int = 20) -> str:
        """
        Converts dBFS level to ASCII bar for display.
        Range: -60 dBFS (silence) to 0 dBFS (maximum).
        """
        normalized = max(0.0, min(1.0, (dbfs + 60) / 60))
        filled = int(normalized * width)
        bar    = '█' * filled + '░' * (width - filled)

        if dbfs > -10:   color = 'red'
        elif dbfs > -20: color = 'yellow'
        elif dbfs > -40: color = 'green'
        else:            color = 'dim'
        return f'[{color}]{bar}[/]  {dbfs:+.1f} dBFS'

    @staticmethod
    def snr_bar(snr_db: float, width: int = 20) -> str:
        """ASCII bar for SNR. Range: 0–40 dB."""
        normalized = max(0.0, min(1.0, snr_db / 40))
        filled = int(normalized * width)
        bar    = '█' * filled + '░' * (width - filled)

        if snr_db > 25:   color = 'green'
        elif snr_db > 12: color = 'yellow'
        else:             color = 'red'
        return f'[{color}]{bar}[/]  {snr_db:.1f} dB'


# ---------------------------------------------------------------------------
# MessageAudioRecorder — records audio around each EAS message
# ---------------------------------------------------------------------------

class MessageAudioRecorder:
    """
    Records a WAV file for each EAS message received.

    Maintains a circular pre-buffer with the last `pre_seconds` seconds
    of audio. When `trigger()` is called, it dumps that pre-buffer to
    the file and continues recording for `post_seconds` additional seconds,
    then closes the file automatically.

    Filename: {save_dir}/{EEE}_{YYYYMMDD}_{HHMMSS}.wav
      - EEE       : event code (e.g., EQW, RWT)
      - YYYYMMDD  : SYSTEM date when message was received
      - HHMMSS    : SYSTEM time when message was received

    Output format: PCM 16-bit, mono, 25000 Hz.

    Usage from pipeline:
        recorder = MessageAudioRecorder(save_dir='/path/messages')
        # In DSPWorker, after each audio chunk:
        recorder.feed(audio)
        # In decoder_loop, when a message is decoded:
        recorder.trigger(event_code='EQW', received_at=datetime.datetime.now())
    """

    # Seconds of audio before and after detection point
    DEFAULT_PRE_SECONDS  = 15.0   # captures full EAS header (3 repetitions)
    DEFAULT_POST_SECONDS = 90.0   # captures attention signal + voice + EOM

    def __init__(self,
                 save_dir:    str,
                 pre_seconds:  float = DEFAULT_PRE_SECONDS,
                 post_seconds: float = DEFAULT_POST_SECONDS):
        self.save_dir     = save_dir
        self._pre_len     = int(pre_seconds  * AUDIO_RATE)   # samples in pre-buffer
        self._post_len    = int(post_seconds * AUDIO_RATE)   # samples to record post-trigger

        # Circular pre-buffer: deque of float32 arrays
        # We store full chunks; when trimming we remove the oldest one.
        self._prebuf      : deque[np.ndarray] = deque()
        self._prebuf_size : int = 0             # total samples in pre-buffer

        # Recording state (protected by _lock)
        self._lock             = threading.Lock()
        self._wav              : wave.Wave_write | None = None
        self._post_remaining   : int = 0        # remaining post-trigger samples
        self._current_path     : str = ''

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, audio: np.ndarray):
        """
        Feed with a float32 @ 25000 Hz audio chunk.
        Call from DSPWorker for each chunk, before or after EASDemod.
        Thread-safe.
        """
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) == 0:
            return

        with self._lock:
            # 1. Update pre-buffer (always, even if recording)
            self._prebuf.append(audio)
            self._prebuf_size += len(audio)
            # Trim from left if maximum size exceeded
            while self._prebuf and self._prebuf_size - len(self._prebuf[0]) >= self._pre_len:
                self._prebuf_size -= len(self._prebuf.popleft())

            # 2. If recording is active, write post-trigger samples
            if self._wav and self._post_remaining > 0:
                n   = min(len(audio), self._post_remaining)
                pcm = np.clip(audio[:n] * 32767.0, -32768, 32767).astype(np.int16)
                self._wav.writeframes(pcm.tobytes())
                self._post_remaining -= n
                if self._post_remaining <= 0:
                    self._close_wav_locked()

    def trigger(self, event_code: str, received_at: datetime.datetime):
        """
        Start recording a new message.
        Call from decoder_loop when `_decode_frame()` returns a result.

        Parameters
        ----------
        event_code  : EEE event code (e.g., 'EQW', 'RWT')
        received_at : system datetime at detection point
        """
        os.makedirs(self.save_dir, exist_ok=True)

        ts  = received_at.strftime('%Y%m%d_%H%M%S')
        # Sanitize event_code in case it contains invalid filename characters
        safe_eee = ''.join(c for c in event_code if c.isalnum() or c in '-_')
        filename = f'{safe_eee}_{ts}.wav'
        path     = os.path.join(self.save_dir, filename)

        with self._lock:
            # Close previous recording if still in progress
            if self._wav:
                log.warning(f'New trigger before closing {self._current_path!r} — closing')
                self._close_wav_locked()

            # Open new WAV file
            try:
                wav = wave.open(path, 'wb')
                wav.setnchannels(1)
                wav.setsampwidth(2)           # 16-bit
                wav.setframerate(AUDIO_RATE)  # 25000 Hz
            except OSError as e:
                log.error(f'Could not create {path}: {e}')
                return

            # Dump pre-buffer to file
            pre_written = 0
            for chunk in self._prebuf:
                pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                wav.writeframes(pcm.tobytes())
                pre_written += len(chunk)

            self._wav            = wav
            self._post_remaining = self._post_len
            self._current_path   = path

        log.info(
            f'Recording message {event_code} → {path}  '
            f'(pre={pre_written/AUDIO_RATE:.1f}s + post={self._post_len/AUDIO_RATE:.0f}s)'
        )

    def stop(self):
        """Close any ongoing recording. Call when pipeline ends."""
        with self._lock:
            if self._wav:
                self._close_wav_locked()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _close_wav_locked(self):
        """Close the WAV. Must be called with _lock acquired."""
        try:
            self._wav.close()
            log.info(f'Recording saved: {self._current_path}')
        except Exception as e:
            log.warning(f'Error closing WAV: {e}')
        finally:
            self._wav          = None
            self._post_remaining = 0
