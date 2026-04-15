"""
fm_demod.py — Narrowband FM Demodulator (NBFM)

Converts complex IQ samples (complex64) from RTL-SDR
to normalized float32 PCM audio @ 25000 Hz.

Internal Pipeline:
    IQ complex64 @ fs_in
        → COMPLEX low-pass filter (channel filter, rejects adjacent carriers)
        → FM Discriminator:  angle(x[n] * conj(x[n-1]))
        → Decimation/resampling → PCM float32 @ 25000 Hz

Implementation Notes:
    - All state buffers are strictly maintained in float32 / complex64
      to avoid silent upcasting to float64/complex128 by SciPy, which produces
      incorrect results in continuous multi-chunk operation.
    - The filter is applied using lfilter (FIR) for numerical stability
       and well-defined state type (float32).
    - The FM discriminator operates in pure complex64 without promotion.
"""

import numpy as np
from scipy.signal import firwin, sosfilt, sosfilt_zi, resample_poly
from math import gcd

# Output rate required by EAS demodulator
AUDIO_RATE = 25000


class FMDemod:
    """
    Narrowband FM demodulator with complex channel filter.

    Parameters
    ----------
    fs_in : int
        Input IQ sample rate (e.g., 250_000 Hz).
    channel_bw : int
        FM channel bandwidth in Hz.
        Default 10_000 Hz — enough for EAS (AFSK ±2 kHz + deviation ±5 kHz)
        and strict enough to reject adjacent NWR channels at 25 kHz.
    gain : float
        Post-demodulation gain applied before resampling.
        Calibrated for NWR signals (ch_amp=0.114):
          - Silent carrier: rms ~0.009 → -41 dBFS (squelch closes)
          - Active EAS message: rms ~0.105 → -20 dBFS (squelch opens)
    """

    def __init__(self, fs_in: int = 250_000, channel_bw: int = 16_000,
                 gain: float = 4.0):
        self.fs_in      = fs_in
        self.channel_bw = channel_bw
        self.gain       = gain

        # --- Low-pass filter designed as FIR ---
        # We design a FIR and apply it via lfilter to maintain
        # internal state correctly typed as float32.
        cutoff_norm = channel_bw / fs_in
        ntaps       = 63
        self._b_lp  = firwin(ntaps, cutoff_norm).astype(np.float32)

        # Filter state: lfilter with b_lp, a=1.0
        # zi has shape (len(b)-1,)
        n_zi = len(self._b_lp) - 1
        self._zi_i = np.zeros(n_zi, dtype=np.float32)
        self._zi_q = np.zeros(n_zi, dtype=np.float32)

        # --- Stateful decimator fs_in → AUDIO_RATE ---
        g            = gcd(AUDIO_RATE, fs_in)
        self._up     = AUDIO_RATE // g
        self._down   = fs_in      // g
        
        # Anti-Aliasing filter for audio decimation
        # For maximum speed on ARM, a simple FIR is more optimizable than a Butter IIR
        # but for now we'll keep Butter with lfilter (faster than sosfilt for low order)
        from scipy.signal import butter
        nyq = fs_in / 2.0
        # Cutoff at 12 kHz
        self._b_audio, self._a_audio = butter(4, 12000.0 / nyq, btype='low')
        self._b_audio = self._b_audio.astype(np.float32)
        self._a_audio = self._a_audio.astype(np.float32)
        self._zi_audio = np.zeros(max(len(self._b_audio), len(self._a_audio)) - 1, dtype=np.float32)
        self._decimate_idx = 0

        # Last IQ sample from previous chunk (differential discriminator)
        # Maintained in float32 components to avoid upcast
        self._prev_i = np.float32(0.0)
        self._prev_q = np.float32(0.0)

    # ------------------------------------------------------------------

    def process(self, iq: np.ndarray) -> np.ndarray:
        """
        Demodulate a chunk of IQ samples.

        Parameters
        ----------
        iq : np.ndarray complex64
            IQ samples from RTL-TCP, range approx ±1.0.

        Returns
        -------
        np.ndarray float32
            Normalized PCM audio @ 25000 Hz, range ±1.0.
        """
        iq = np.asarray(iq, dtype=np.complex64)

        # Separate I and Q channels for lfilter (float32)
        i_in = iq.real.astype(np.float32)
        q_in = iq.imag.astype(np.float32)

        from scipy.signal import lfilter

        # 1. Channel Filter: lfilter is faster for FIR than sosfilt in many SciPy versions
        i_filt, self._zi_i = lfilter(self._b_lp, [1.0], i_in, zi=self._zi_i)
        q_filt, self._zi_q = lfilter(self._b_lp, [1.0], q_in, zi=self._zi_q)

        # 2. Optimized Differential FM Discriminator:
        #    We avoid heavy concatenations by treating the first sample separately
        #    IQ[n] * conj(IQ[n-1])
        
        # First calculate for n=1..N-1 (vectorized)
        diff_re = i_filt[1:] * i_filt[:-1] + q_filt[1:] * q_filt[:-1]
        diff_im = q_filt[1:] * i_filt[:-1] - i_filt[1:] * q_filt[:-1]
        
        # Then insert the first sample using previous state
        first_re = i_filt[0] * self._prev_i + q_filt[0] * self._prev_q
        first_im = q_filt[0] * self._prev_i - i_filt[0] * self._prev_q
        
        # Reassemble (much faster than concatenating full shifted array)
        diff_re = np.insert(diff_re, 0, first_re)
        diff_im = np.insert(diff_im, 0, first_im)

        audio_fs = np.arctan2(diff_im, diff_re).astype(np.float32)  # range [-π, π]

        # Save last samples for next chunk
        self._prev_i = i_filt[-1]
        self._prev_q = q_filt[-1]

        # Normalize to ±1 (divide by π) and apply post-demodulation gain
        audio_fs *= (self.gain / np.float32(np.pi))

        # 3. Decimation fs_in → AUDIO_RATE with phase preservation between chunks
        # lfilter here as well for speed
        audio_filt, self._zi_audio = lfilter(self._b_audio, self._a_audio, audio_fs, zi=self._zi_audio)
        
        # Decimate array respecting mathematical offset from previous block
        audio = audio_filt[self._decimate_idx :: self._down]
        # Calculate how much the last decimated sample "overshot" the buffer end.
        # Last sample taken at: _decimate_idx + (len(audio) - 1) * _down
        last_taken = self._decimate_idx + (len(audio) - 1) * self._down if len(audio) > 0 else self._decimate_idx - self._down
        # Next start index = last_taken + _down - len(audio_fs)
        self._decimate_idx = (last_taken + self._down) - len(audio_filt)

        return audio
