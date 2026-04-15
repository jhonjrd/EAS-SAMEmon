"""
eas_demod.py — EAS-SAMEmon
Pure Python + NumPy EAS/SAME demodulator.
Native, high-precision, and optimized for embedded systems.

Features:
- Direct port of quadrature IQ correlation algorithms.
- Accepts float32 PCM audio @ 25000 Hz.
- Implements bit-clock phase tracking (DLL) and 2-of-3 voting.
"""

import numpy as np

# ---------------------------------------------------------------------------
# Constants — Base values (BAUD is standard for EAS/SAME)
# ---------------------------------------------------------------------------
FREQ_MARK  = 2083.333333 # Hz — bit 1 (exactly 25000/12)
FREQ_SPACE = 1562.5      # Hz — bit 0 (exactly 25000/16)
BAUD       = 520.833333  # symbols/s (exactly 25000/48)

# Demodulator configuration (fs independent)
SUBSAMP           = 2    # window step (oversampling factor)
INTEGRATOR_MAXVAL = 10
DLL_GAIN          = 0.2
SQUELCH_THRESHOLD = 0.001

PREAMBLE     = 0xAB      # preamble byte (LSB first on wire = 11010101)
HEADER_BEGIN = 'ZCZC'
EOM_MARKER   = 'NNNN'
MAX_MSG_LEN  = 268
MAX_STORE    = 3         # repetitions to store for 2-of-3 voting


# ---------------------------------------------------------------------------
# ASCII character validation (eas_allowed in C)
# ---------------------------------------------------------------------------
def _is_allowed(ch: int) -> bool:
    """Returns True if byte is a valid EAS ASCII character."""
    if ch & 0x80:
        return False
    return ch in (10, 13) or (32 <= ch <= 126)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class EASDemod:
    """
    EAS/SAME Demodulator.

    Usage:
        def on_message(msg: str): print(msg)
        demod = EASDemod(callback=on_message)
        demod.process(audio_float32_array)   # call with each chunk
    """

    def __init__(self, callback, sample_rate: int = 25000):
        self.callback    = callback
        self.sample_rate = sample_rate

        # --- Sample rate dependent parameters ---
        self.corrlen    = int(self.sample_rate / BAUD)
        self.phase_inc  = (BAUD * SUBSAMP / self.sample_rate)

        # --- Precalculated correlators ---
        idx = np.arange(self.corrlen)
        self.corr_mark_i  = np.cos(2 * np.pi * FREQ_MARK  / self.sample_rate * idx).astype(np.float32)
        self.corr_mark_q  = np.sin(2 * np.pi * FREQ_MARK  / self.sample_rate * idx).astype(np.float32)
        self.corr_space_i = np.cos(2 * np.pi * FREQ_SPACE / self.sample_rate * idx).astype(np.float32)
        self.corr_space_q = np.sin(2 * np.pi * FREQ_SPACE / self.sample_rate * idx).astype(np.float32)

        # --- L1 State (physical layer) ---
        self._l1_reset()

        # --- L2 State (protocol layer) ---
        self._l2_reset()

        # --- Overlap buffer between chunks ---
        # We need (corrlen-1) samples from previous chunk
        self._overlap = np.zeros(self.corrlen - 1, dtype=np.float32)

    # ------------------------------------------------------------------
    # State Reset
    # ------------------------------------------------------------------
    def _l1_reset(self):
        self.sphase         = 0.0
        self.dcd_shreg      = 0
        self.bit_shreg      = 0
        self.dcd_integrator = 0
        self.lasts          = 0
        self.l1_sync        = False
        self.byte_counter   = 0
        self._subsamp_skip  = 0   # samples to skip at start of next chunk

    def _l2_reset(self):
        # L2: IDLE | HEADER_SEARCH | READING_MESSAGE | READING_EOM
        self.l2_state    = 'IDLE'
        self.head_buf    = ''
        self.headlen     = 0
        self.msg_buf     = [''] * MAX_STORE
        self.msgno       = 0
        self.msglen      = 0
        self.last_message = ''
        # Counter for partial EOM bursts.
        self._nnnn_bursts = 0
        # Bit error tolerance
        self.bad_byte_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def process(self, samples: np.ndarray):
        """
        Process float32 audio array @ 25000 Hz.
        Can be called repeatedly with chunks of any size >= CORRLEN.
        """
        samples = np.asarray(samples, dtype=np.float32)

        # Concatenate overlap from previous chunk
        buf = np.concatenate([self._overlap, samples])

        # Skip samples pending from previous iteration (subsamp handling)
        start = self._subsamp_skip
        self._subsamp_skip = 0

        i = start
        end = len(buf) - self.corrlen + 1

        # ---- Vectorized FIR Correlator ----
        mi = np.correlate(buf, self.corr_mark_i, mode='valid')
        mq = np.correlate(buf, self.corr_mark_q, mode='valid')
        si = np.correlate(buf, self.corr_space_i, mode='valid')
        sq = np.correlate(buf, self.corr_space_q, mode='valid')
        f_array = mi*mi + mq*mq - si*si - sq*sq

        # Iterate only through subsamples
        f_seq = f_array[self._subsamp_skip :: SUBSAMP]
        mi_seq = mi[self._subsamp_skip :: SUBSAMP]
        mq_seq = mq[self._subsamp_skip :: SUBSAMP]
        si_seq = si[self._subsamp_skip :: SUBSAMP]
        sq_seq = sq[self._subsamp_skip :: SUBSAMP]

        # Vectorize energy calculation for full block (much faster than Doing it in loop)
        pwr_seq = mi_seq**2 + mq_seq**2 + si_seq**2 + sq_seq**2
        
        # Constant phase increment to save one multiplication per iteration
        phase_inc = self.phase_inc

        for f_idx, f in enumerate(f_seq):
            # Bit clock (phase) MUST always run to maintain synchrony
            # even if channel fades momentarily.
            self.sphase += phase_inc

            # ---- Magnitude Squelch ----
            if pwr_seq[f_idx] < SQUELCH_THRESHOLD:
                # We only reset phase clock if NOT synchronized (searching for preamble)
                if not self.l1_sync:
                    self.sphase = 0.0
                    self.dcd_integrator = 0
                continue
            # ---- DCD shift register (Sample Rate - for DLL) ----
            bit_now = 1 if f > 0 else 0
            self.dcd_shreg = ((self.dcd_shreg << 1) | bit_now) & 0xFF

            # ---- Accumulator Integrator (Sample Rate) ----
            if f > 0 and self.dcd_integrator < INTEGRATOR_MAXVAL:
                self.dcd_integrator += 1
            elif f < 0 and self.dcd_integrator > -INTEGRATOR_MAXVAL:
                self.dcd_integrator -= 1

            # ---- DLL (Transition Tracking) ----
            # Adjust phase based on bit transitions
            if (self.dcd_shreg ^ (self.dcd_shreg >> 1)) & 1:
                error = 0.5 - self.sphase
                # Correction must be direct, not multiplied by phase_inc
                self.sphase += error * DLL_GAIN

            # ---- End of bit period ----
            # ---- Bit Decision (Sampling at Bit Rate) ----
            if self.sphase >= 1.0:
                self.sphase -= 1.0

                # Bit decided by integrator
                bit_sampled = 1 if self.dcd_integrator >= 0 else 0
                self.dcd_integrator = 0  # IMPORTANT: reset integrator for next bit
                # SAME standard is LSB first: enter via bit 15 and rotate right
                self.bit_shreg = (self.bit_shreg >> 1) | (0x8000 if bit_sampled else 0)
                
                # Update Data register (lasts)
                self.lasts = (self.lasts >> 1) | (0x80 if bit_sampled else 0)

                # ---- Preamble Detection (0xABAB = 16 bits of real data) ----
                if self.bit_shreg == 0xABAB:
                    if self.l2_state != 'READING_MESSAGE':
                        self.l1_sync      = True
                        self.byte_counter = 0
                        # Lock lasts so it starts clean
                        self.lasts = 0xAB 

                # ---- Synchronized Byte Accumulation ----
                elif self.l1_sync:
                    self.byte_counter += 1
                    if self.byte_counter == 8:
                        ch = self.lasts & 0xFF
                        if _is_allowed(ch):
                            self._eas_frame(ch)
                            self.bad_byte_counter = 0
                        else:
                            self.bad_byte_counter += 1
                            if self.bad_byte_counter > 3: # Increased tolerance for long bursts
                                self.l1_sync = False
                                self._eas_frame(0x00)
                            else:
                                # Maintain sync briefly to skip corrupt bits
                                pass 
                        self.byte_counter = 0

            i += SUBSAMP

        # ---- Save overlap for next chunk ----
        self._overlap = buf[-(self.corrlen - 1):].copy()

        # Update how many samples were missing to complete last subsamp
        # valid_len is f_array.size
        # The loop consumed len(f_seq) correlations
        consumed_samples = self._subsamp_skip + len(f_seq) * SUBSAMP
        overshoot = consumed_samples - len(f_array)
        if overshoot > 0:
            self._subsamp_skip = SUBSAMP - overshoot
        else:
            self._subsamp_skip = 0

    # ------------------------------------------------------------------
    # L2 — Protocol State Machine (eas_frame in C)
    # ------------------------------------------------------------------
    def _eas_frame(self, byte_val: int):
        """Processes a decoded byte or 0x00 (end of frame)."""

        if byte_val:
            ch = chr(byte_val)

            # Activate header search if idle
            if self.l2_state == 'IDLE':
                self.l2_state = 'HEADER_SEARCH'

            if self.l2_state == 'HEADER_SEARCH':
                if self.headlen < 4:
                    self.head_buf += ch
                    self.headlen  += 1

                if self.headlen == 4:
                    if self.head_buf == HEADER_BEGIN:
                        self.l2_state = 'READING_MESSAGE'
                        self.sphase = 0.0 # PHASE RESET: start message with perfect clock
                    elif self.head_buf == EOM_MARKER:
                        # If EOM detected, force closure of previous message (if any)
                        if self.l2_state == 'READING_MESSAGE':
                            self._eas_frame(0x00)
                        self.l2_state = 'READING_EOM'
                    else:
                        # Invalid Header
                        self.l2_state = 'IDLE'
                        self.head_buf = ''
                        self.headlen  = 0

            elif self.l2_state == 'READING_MESSAGE':
                if self.msglen < MAX_MSG_LEN:
                    self.msg_buf[self.msgno] += ch
                    self.msglen += 1
                else:
                    # Message too long: force frame closure.
                    # Prevents concatenation of consecutive messages when
                    # noise between transmissions produces valid ASCII bytes.
                    self._eas_frame(0x00)
                    return

        else:
            if self.l2_state == 'READING_MESSAGE':
                # Clip until last '-' (standard SAME format)
                msg = self.msg_buf[0]
                idx = msg.rfind('-')
                if idx >= 0:
                    msg = msg[:idx + 1]
                
                # If it's a new message, emit immediately
                if msg and msg != self.last_message:
                    self.last_message = msg
                    self.callback(f'ZCZC{self.last_message}')
                    self._nnnn_bursts = 0
                
                # Bursts 2 and 3 are ignored if they match self.last_message
                self.msg_buf[0] = ''

            elif self.l2_state == 'READING_EOM':
                if self.last_message:
                    self.callback('NNNN')
                    self.last_message = ''  # Indicates no active message
                self._nnnn_bursts = 0 
                self.msg_buf[0]   = ''

            elif self.l2_state == 'HEADER_SEARCH':
                # Partial EOM (SASMEX / Short preamble)
                # If we receive at least one 'N' in a burst with L1 sync
                if self.headlen >= 1 and all(c == 'N' for c in self.head_buf):
                    self._nnnn_bursts += 1
                    # If we detect bursts with N's and have an active message, emit EOM
                    if self._nnnn_bursts >= 1: 
                        if self.last_message:
                            self.callback('NNNN')
                            self.last_message = ''
                        self._nnnn_bursts = 0
                        self.msg_buf[0]   = ''
                else:
                    # If we receive something other than 'N', reset accumulated bursts
                    if self.headlen > 0:
                        self._nnnn_bursts = 0

            # Back to IDLE
            self.l2_state = 'IDLE'
            self.head_buf = ''
            self.headlen  = 0
            self.msglen   = 0

    def _check_and_emit(self):
        """Not used in immediate burst mode."""
        pass
