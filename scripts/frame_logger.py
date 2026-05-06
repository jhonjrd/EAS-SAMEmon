"""
frame_logger.py — Append-only log of every EAS frame seen by the decoder.

One JSONL file per local day: {save_dir}/{YYYY-MM-DD}.jsonl

Each line includes:
  - ts_local / ts_utc          : ISO-8601 timestamps
  - raw                        : full raw frame string as received
  - length                     : len(raw)
  - kind                       : 'header' | 'eom' | 'other'
  - decoded                    : True / False / None (None for EOM)
  - reject_reason              : set when decoded is False
  - level_dbfs / snr_db / deviation : snapshot of AudioMonitor metrics
                                     (if monitor is provided), else None
  - invalid_chars              : list of non-ASCII-printable or out-of-alphabet
                                  characters found in the raw frame
"""

from __future__ import annotations

import os
import json
import threading
import datetime
import logging

log = logging.getLogger(__name__)

# SAME header payload alphabet: uppercase letters, digits, and the frame
# delimiters. Anything outside this set in a header is a decoder error.
_SAME_ALPHABET = set(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-/ '
)


class FrameLogger:
    """
    Thread-safe append-only frame logger. One JSONL file per local day.
    """

    def __init__(self, save_dir: str, audio_monitor=None):
        self.save_dir      = save_dir
        self._monitor      = audio_monitor
        self._lock         = threading.Lock()
        os.makedirs(self.save_dir, exist_ok=True)

    def log(self,
            raw: str,
            decoded: bool | None,
            reject_reason: str | None = None):
        """
        Record a single frame observation.

        Parameters
        ----------
        raw            : the raw frame string as emitted by the demodulator.
        decoded        : True if same_decode() produced a result,
                         False if the frame was rejected,
                         None for EOM (NNNN) which has no decode step.
        reject_reason  : human-readable explanation when decoded is False.
        """
        now_utc   = datetime.datetime.now(datetime.timezone.utc)
        now_local = datetime.datetime.now()

        raw_str = raw if isinstance(raw, str) else str(raw)
        stripped = raw_str.strip()
        if stripped == 'NNNN':
            kind = 'eom'
        elif 'ZCZC' in stripped:
            kind = 'header'
        else:
            kind = 'other'

        invalid_chars: list[str] = []
        if kind == 'header':
            seen: set[str] = set()
            for c in stripped:
                if c not in _SAME_ALPHABET and c not in seen:
                    invalid_chars.append(c)
                    seen.add(c)

        metrics = self._snapshot_metrics()

        record = {
            'ts_local':      now_local.isoformat(timespec='seconds'),
            'ts_utc':        now_utc.isoformat(timespec='seconds'),
            'kind':          kind,
            'raw':           raw_str,
            'length':        len(raw_str),
            'decoded':       decoded,
            'reject_reason': reject_reason,
            'invalid_chars': invalid_chars,
            'level_dbfs':    metrics.get('level_dbfs'),
            'snr_db':        metrics.get('snr_db'),
            'deviation':     metrics.get('deviation'),
        }

        path = os.path.join(
            self.save_dir,
            f'{now_local.strftime("%Y-%m-%d")}.jsonl',
        )
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except OSError as e:
                log.warning(f'FrameLogger: could not write {path}: {e}')

    # ------------------------------------------------------------------

    def _snapshot_metrics(self) -> dict:
        if not self._monitor:
            return {}
        try:
            return self._monitor.get_metrics() or {}
        except Exception:
            return {}
