#!/usr/bin/env python3
"""Plot RMS envelope and FSK discriminator across the burst to see if the
carrier drops or the bit timing drifts at the point where decoding fails."""
import sys, os, wave, numpy as np
from scipy.signal import resample
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
from eas_demod import FREQ_MARK, FREQ_SPACE, BAUD

def load(p, fs=25000):
    with wave.open(p, 'rb') as w:
        ch=w.getnchannels(); rate=w.getframerate(); raw=w.readframes(w.getnframes())
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 2: s = s.reshape(-1, 2).mean(1)
    if rate != fs: s = resample(s, int(len(s)*fs/rate)).astype(np.float32)
    return s

def main():
    s = load(sys.argv[1])
    fs = 25000
    win = 240  # ~1 bit
    # RMS envelope
    sq = s*s
    rms = np.sqrt(np.convolve(sq, np.ones(win)/win, 'same'))
    # FSK power for mark and space
    n = np.arange(len(s))
    mi = np.cos(2*np.pi*FREQ_MARK/fs*n)*s
    mq = np.sin(2*np.pi*FREQ_MARK/fs*n)*s
    si = np.cos(2*np.pi*FREQ_SPACE/fs*n)*s
    sq2 = np.sin(2*np.pi*FREQ_SPACE/fs*n)*s
    box = np.ones(win)/win
    Mi = np.convolve(mi, box, 'same'); Mq = np.convolve(mq, box, 'same')
    Si = np.convolve(si, box, 'same'); Sq = np.convolve(sq2, box, 'same')
    Mp = Mi*Mi+Mq*Mq; Sp = Si*Si+Sq*Sq
    total = Mp + Sp
    # Print key samples  every 100ms
    print(f"{'t(s)':>6} {'rms':>8} {'mark':>10} {'space':>10} {'mark/(M+S)':>10}")
    for t in np.arange(1.0, 5.0, 0.02):
        i = int(t*fs)
        if i >= len(s): break
        ratio = Mp[i]/(total[i]+1e-12)
        print(f"{t:6.2f} {rms[i]:8.4f} {Mp[i]:10.6f} {Sp[i]:10.6f} {ratio:10.3f}")

if __name__=='__main__': main()
