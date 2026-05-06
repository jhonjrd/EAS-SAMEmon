"""
eas_demod.py — EAS-SAMEmon
Pure Python + NumPy EAS/SAME demodulator.
Native, high-precision, and optimized for embedded systems.

Features:
- Direct port of quadrature IQ correlation algorithms.
- Accepts float32 PCM audio @ 25000 Hz.
- Implements bit-clock phase tracking (DLL) and 2-of-3 voting.

Callback contract (dual-path emission):
    callback(msg: str, stage: str)

    stage='preliminary' — exactly once per alert, on the FIRST decoded burst.
        Use for low-latency actions: webhook (Alerta Sísmica), audio recorder.
        msg may contain noise chars in fields beyond EEE; trust EEE/PSSCCC.

    stage='final' — exactly once per alert, after consolidation.
        Emitted when any of these happens (whichever first):
          (a) all 3 burst repetitions have been processed,
          (b) EOM marker (NNNN) is detected,
          (c) FINAL_TIMEOUT_S of silence after the last seen burst.
        Use for persistence: event_store, display, JSON dump.
        msg is the result of char-by-char 2-of-3 majority voting (or single-
        burst content when fewer bursts decoded).

    stage='eom' — End-Of-Message marker received (msg is empty string).
        Optional; consumers may ignore.
"""

import time

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
# Force-close the current SAME burst if the carrier (squelch) stays closed
# this many consecutive subsamples while in READING_MESSAGE. SASMEX EQW
# repeats the header with only ~93 ms gap; without this, byte_counter and
# the L2 buffer leak across bursts and the 2-of-3 vote fails on garbage
# positions. 750 subsamples @ SUBSAMP=2, fs=25000 ≈ 60 ms — safely
# shorter than 93 ms but long enough not to fire on a single fade.
CARRIER_OFF_RESET_SS = 750

PREAMBLE     = 0xAB      # preamble byte (LSB first on wire = 11010101)
HEADER_BEGIN = 'ZCZC'
EOM_MARKER   = 'NNNN'
MAX_MSG_LEN  = 268
MAX_STORE    = 3         # repetitions to store for 2-of-3 voting
# TTL (s) for deduplicating identical ZCZC bursts within a single transmission.
# Covers the 3 SAME header repetitions (~3 s apart) without blocking identical
# content that repeats hours later (e.g., weekly RWT with fixed JJJHHMM).
DEDUP_TTL_S    = 30
# How long to wait after the last seen burst before forcing a 'final' emit
# if no further bursts arrive and no EOM is detected. SAME bursts are ~1 s
# apart; 6 s gives ample margin for 3 bursts plus jitter.
FINAL_TIMEOUT_S = 6.0


# ---------------------------------------------------------------------------
# ASCII character validation (eas_allowed in C)
# ---------------------------------------------------------------------------
def _is_allowed(ch: int) -> bool:
    """Returns True if byte is a valid EAS ASCII character."""
    if ch & 0x80:
        return False
    return ch in (10, 13) or (32 <= ch <= 126)


def _dedup_key(s: str) -> str:
    """Structural prefix of a SAME message excluding the LLLLLLLL field.
    Format: ...+TTTT-JJJHHMM-LLLLLLLL-  → returns up to the '-' after
    JJJHHMM. Used for dedup so bit-flips in the (often truncated) callsign
    don't fool us into emitting the 3 header repetitions as 3 messages."""
    plus = s.find('+')
    if plus < 0:
        return s
    d1 = s.find('-', plus)
    if d1 < 0:
        return s
    d2 = s.find('-', d1 + 1)
    return s[:d2 + 1] if d2 >= 0 else s


def _vote_messages(msgs):
    """Strict char-by-char majority vote across stored bursts. Positions
    without ≥2 matching chars become '?'. Single-burst input returns as-is
    (no false confidence loss when only 1 of 3 bursts decoded)."""
    msgs = [m for m in msgs if m]
    if not msgs:
        return ''
    if len(msgs) == 1:
        return msgs[0]
    L = max(len(m) for m in msgs)
    out = []
    for i in range(L):
        chars = [m[i] for m in msgs if i < len(m)]
        counts = {}
        for c in chars:
            counts[c] = counts.get(c, 0) + 1
        best_char, best_n = max(counts.items(), key=lambda kv: kv[1])
        out.append(best_char if best_n >= 2 else '?')
    return ''.join(out)


def _trim_keep_callsign(msg: str) -> str:
    """Trim message to standard SAME format ending in '-', but preserve a
    truncated callsign-like tail (1-8 chars in [A-Z0-9/?]). SASMEX TXs
    routinely cut carrier ~30 ms early, dropping the final '3-' off
    LLLLLLLL; this keeps what we have so alertparser can pad with '?'.

    A tail consisting entirely of '?' is treated as carrier-decay noise that
    survived the 2-of-3 vote (bursts disagreed in every position past the
    real end of frame) and is dropped — otherwise the stored raw frame ends
    in spurious '-????????-' that wasn't on the air."""
    idx = msg.rfind('-')
    if idx < 0:
        return msg
    tail = msg[idx + 1:][:8]
    if tail and all(c.isalnum() or c in '/?' for c in tail) and any(c != '?' for c in tail):
        return msg[:idx + 1] + tail + '-'
    return msg[:idx + 1]


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
        self.sphase            = 0.0
        self.dcd_shreg         = 0
        self.bit_shreg         = 0
        self.dcd_integrator    = 0
        self.lasts             = 0
        self.l1_sync           = False
        self.byte_counter      = 0
        self._subsamp_skip     = 0   # samples to skip at start of next chunk
        self._carrier_off_run  = 0   # consecutive sub-samples below squelch

    def _l2_reset(self):
        # L2: IDLE | HEADER_SEARCH | READING_MESSAGE | READING_EOM
        self.l2_state    = 'IDLE'
        self.head_buf    = ''
        self.headlen     = 0
        self.msg_buf     = [''] * MAX_STORE
        self.msgno       = 0
        self.msglen      = 0
        self.last_message = ''
        self._last_message_ts = 0.0
        # Counter for partial EOM bursts.
        self._nnnn_bursts = 0
        # Bit error tolerance
        self.bad_byte_counter = 0
        # Dual-path emission state (per active alert).
        self._first_emitted   = False   # 'preliminary' already emitted?
        self._finalized       = False   # 'final' already emitted?
        self._preliminary_msg = ''      # what we sent as preliminary
        self._final_deadline  = 0.0     # monotonic; 0 = inactive

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def process(self, samples: np.ndarray):
        """
        Process float32 audio array @ 25000 Hz.
        Can be called repeatedly with chunks of any size >= CORRLEN.
        """
        # Force-finalize a pending alert if too much time has passed since
        # the last burst (timeout path of dual-path emission).
        self._check_final_timeout()

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

        # Vectorize energy calculation for full block (much faster than doing it in loop)
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
                # Force-close the current SAME burst if the carrier stays
                # off long enough that we know we're between bursts. Without
                # this, SASMEX EQW (93 ms inter-burst gap) leaks bytes from
                # one repetition into the next slot and breaks 2-of-3 vote.
                self._carrier_off_run += 1
                if (self.l2_state == 'READING_MESSAGE' and
                        self._carrier_off_run >= CARRIER_OFF_RESET_SS):
                    self._carrier_off_run = 0
                    self._eas_frame(0x00)
                    self.l1_sync = False
                continue
            self._carrier_off_run = 0
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
                # SAME sends 3 identical header repetitions. Dual-path
                # emission strategy (see module docstring):
                #   - 'preliminary' once on burst 1 (low latency for alerting).
                #   - 'final'       once after quorum / EOM / timeout, with
                #                   the consolidated voted message.
                now = time.monotonic()
                cur_raw = self.msg_buf[self.msgno]
                cur_trim = _trim_keep_callsign(cur_raw)

                # New alert mid-flight: previous alert never reached EOM/timeout.
                # Force-finalize the prior alert and restart accumulation.
                if (self.msgno > 0 and self._preliminary_msg and
                    (_dedup_key(cur_trim) != _dedup_key(self._preliminary_msg)
                     or now - self._last_message_ts > DEDUP_TTL_S)):
                    self._emit_final()
                    self._reset_alert_state()
                    self.msg_buf[0] = cur_raw
                    self.msgno = 0

                # PRELIMINARY: emit once, on the very first burst.
                if not self._first_emitted and cur_trim:
                    self._first_emitted   = True
                    self._preliminary_msg = cur_trim
                    self._last_message_ts = now
                    self.last_message     = cur_trim
                    self.callback(f'ZCZC{cur_trim}', 'preliminary')

                # Track when to finalize if no more bursts arrive.
                self._final_deadline = now + FINAL_TIMEOUT_S

                # Quorum reached (3 bursts collected) → final immediately.
                if self.msgno + 1 >= MAX_STORE:
                    self._emit_final()

                # Advance slot for next burst (cap at MAX_STORE-1; extra
                # bursts overwrite last slot harmlessly).
                if self.msgno < MAX_STORE - 1:
                    self.msgno += 1
                    self.msg_buf[self.msgno] = ''
                else:
                    self.msg_buf[self.msgno] = ''

            elif self.l2_state == 'READING_EOM':
                self._emit_final()
                if self._first_emitted:
                    self.callback('', 'eom')
                self._reset_alert_state()

            elif self.l2_state == 'HEADER_SEARCH':
                # Partial EOM (SASMEX / Short preamble)
                # If we receive at least one 'N' in a burst with L1 sync
                if self.headlen >= 1 and all(c == 'N' for c in self.head_buf):
                    self._nnnn_bursts += 1
                    if self._nnnn_bursts >= 1:
                        self._emit_final()
                        if self._first_emitted:
                            self.callback('', 'eom')
                        self._reset_alert_state()
                else:
                    # If we receive something other than 'N', reset accumulated bursts
                    if self.headlen > 0:
                        self._nnnn_bursts = 0

            # Back to IDLE
            self.l2_state = 'IDLE'
            self.head_buf = ''
            self.headlen  = 0
            self.msglen   = 0

    # ------------------------------------------------------------------
    # Dual-path emission helpers
    # ------------------------------------------------------------------
    def _emit_final(self):
        """Emit the consolidated 'final' message exactly once per alert.
        Idempotent: subsequent calls within the same alert are no-ops."""
        if self._finalized or not self._first_emitted:
            return
        stored = [m for m in self.msg_buf if m]
        if stored:
            voted = _trim_keep_callsign(_vote_messages(stored))
        else:
            voted = self._preliminary_msg
        # If voting produced nothing usable, fall back to preliminary content.
        if not voted:
            voted = self._preliminary_msg
        if voted:
            self._finalized = True
            self.last_message = voted
            self._last_message_ts = time.monotonic()
            self.callback(f'ZCZC{voted}', 'final')

    def _check_final_timeout(self):
        """Called from process(): if a preliminary was emitted but no
        EOM / 3rd burst arrived within FINAL_TIMEOUT_S, finalize now."""
        if (self._first_emitted and not self._finalized and
                self._final_deadline > 0 and
                time.monotonic() >= self._final_deadline):
            self._emit_final()
            self._reset_alert_state()

    def _reset_alert_state(self):
        """Clear per-alert tracking after final/EOM. Preserves last_message
        and _last_message_ts so dedup still works for repeat-bursts."""
        self.msg_buf          = [''] * MAX_STORE
        self.msgno            = 0
        self.msglen           = 0
        self._nnnn_bursts     = 0
        self._first_emitted   = False
        self._finalized       = False
        self._preliminary_msg = ''
        self._final_deadline  = 0.0
