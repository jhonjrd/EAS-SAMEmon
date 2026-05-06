#!/usr/bin/env python3
"""Print the 3 header bursts side-by-side as the demod actually decodes them
(passing _is_allowed, before any trim), so we can see what voting can recover."""
import sys, os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
from eas_demod import EASDemod, _is_allowed
from fm_demod import AUDIO_RATE
import wave, numpy as np
from scipy.signal import resample

def load(p, fs=AUDIO_RATE):
    with wave.open(p, 'rb') as w:
        ch = w.getnchannels(); rate = w.getframerate(); raw = w.readframes(w.getnframes())
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
    if ch == 2: s = s.reshape(-1,2).mean(1)
    if rate != fs: s = resample(s, int(len(s)*fs/rate)).astype(np.float32)
    return s

bursts = []
current = []
def cb(msg, stage):
    pass

# Hook into EASDemod's _eas_frame to capture every byte going into msg_buf per burst
demod = EASDemod(callback=cb)
orig = demod._eas_frame
state = {'cur': []}

def hook(byte_val):
    if byte_val and demod.l2_state == 'READING_MESSAGE':
        state['cur'].append(chr(byte_val))
    elif byte_val == 0 and demod.l2_state == 'READING_MESSAGE':
        if state['cur']:
            bursts.append(''.join(state['cur']))
            state['cur'] = []
    orig(byte_val)

demod._eas_frame = hook
audio = load(sys.argv[1])
chunk = AUDIO_RATE // 2
for i in range(0, len(audio), chunk):
    demod.process(audio[i:i+chunk])

print(f'Captured {len(bursts)} message bursts:')
maxlen = max(len(b) for b in bursts) if bursts else 0
for i, b in enumerate(bursts):
    print(f'  [{i}] len={len(b):3d}  {b!r}')
print()
print('Char-by-char alignment:')
print('     pos: ' + ''.join(f'{i%10}' for i in range(maxlen)))
for i, b in enumerate(bursts):
    padded = b.ljust(maxlen, '·')
    print(f'  burst{i}: {padded}')

# Majority vote
print()
print('Majority vote (≥2 agree, else · for tie/missing):')
voted = []
for i in range(maxlen):
    chars = [b[i] for b in bursts if i < len(b)]
    counts = {}
    for c in chars: counts[c] = counts.get(c, 0) + 1
    best, n = max(counts.items(), key=lambda kv: kv[1])
    if n >= 2:
        voted.append(best)
    elif len(chars) == 1:
        voted.append(best)
    else:
        voted.append('·')
print('  voted : ' + ''.join(voted))
