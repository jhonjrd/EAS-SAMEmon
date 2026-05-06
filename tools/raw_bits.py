#!/usr/bin/env python3
"""Raw bit-level dump of EAS audio. Shows exactly what bytes follow the preamble
without the rfind('-') trim, so we can see what's actually transmitted at the
end of the SASMEX header."""
import sys, os, wave, numpy as np
from scipy.signal import resample
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
from eas_demod import (FREQ_MARK, FREQ_SPACE, BAUD, SUBSAMP,
                       INTEGRATOR_MAXVAL, DLL_GAIN, SQUELCH_THRESHOLD)

def load(path, fs=25000):
    with wave.open(path, 'rb') as w:
        ch = w.getnchannels(); rate = w.getframerate()
        sw = w.getsampwidth(); raw = w.readframes(w.getnframes())
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 2: s = s.reshape(-1, 2).mean(1)
    if rate != fs:
        s = resample(s, int(len(s) * fs / rate)).astype(np.float32)
    return s

def demod(samples, fs=25000):
    corrlen = int(fs / BAUD)
    idx = np.arange(corrlen)
    cmi = np.cos(2*np.pi*FREQ_MARK/fs*idx).astype(np.float32)
    cmq = np.sin(2*np.pi*FREQ_MARK/fs*idx).astype(np.float32)
    csi = np.cos(2*np.pi*FREQ_SPACE/fs*idx).astype(np.float32)
    csq = np.sin(2*np.pi*FREQ_SPACE/fs*idx).astype(np.float32)
    mi = np.correlate(samples, cmi, 'valid'); mq = np.correlate(samples, cmq, 'valid')
    si = np.correlate(samples, csi, 'valid'); sq = np.correlate(samples, csq, 'valid')
    f_arr = mi*mi + mq*mq - si*si - sq*sq
    pwr = mi*mi+mq*mq+si*si+sq*sq
    f_seq = f_arr[::SUBSAMP]; p_seq = pwr[::SUBSAMP]
    phase_inc = BAUD * SUBSAMP / fs
    sphase = 0.0; dcd_shreg = 0; integ = 0; lasts = 0
    bit_shreg = 0; sync = False; bytec = 0
    events = []  # (sample_idx, kind, value)
    for i, f in enumerate(f_seq):
        sphase += phase_inc
        if p_seq[i] < SQUELCH_THRESHOLD:
            if not sync:
                sphase = 0.0; integ = 0
            continue
        bn = 1 if f > 0 else 0
        dcd_shreg = ((dcd_shreg << 1) | bn) & 0xFF
        if f > 0 and integ < INTEGRATOR_MAXVAL: integ += 1
        elif f < 0 and integ > -INTEGRATOR_MAXVAL: integ -= 1
        if (dcd_shreg ^ (dcd_shreg >> 1)) & 1:
            sphase += (0.5 - sphase) * DLL_GAIN
        if sphase >= 1.0:
            sphase -= 1.0
            bit = 1 if integ >= 0 else 0
            integ = 0
            bit_shreg = (bit_shreg >> 1) | (0x8000 if bit else 0)
            lasts = (lasts >> 1) | (0x80 if bit else 0)
            if bit_shreg == 0xABAB:
                if not sync:
                    events.append((i*SUBSAMP, 'SYNC', 0))
                sync = True; bytec = 0; lasts = 0xAB
            elif sync:
                bytec += 1
                if bytec == 8:
                    bytec = 0
                    events.append((i*SUBSAMP, 'BYTE', lasts & 0xFF))
    return events

def main():
    path = sys.argv[1]
    s = load(path)
    print(f'samples={len(s)}  duration={len(s)/25000:.2f}s')
    ev = demod(s)
    burst = []  # collect bytes per sync run
    bursts = []
    last_pos = -1
    for pos, kind, val in ev:
        if kind == 'SYNC':
            if burst: bursts.append(burst)
            burst = []
        elif kind == 'BYTE':
            # break burst on big gap (>200ms = 5000 samples)
            if last_pos >= 0 and pos - last_pos > 5000:
                if burst: bursts.append(burst); burst=[]
            burst.append((pos, val))
            last_pos = pos
    if burst: bursts.append(burst)
    for bi, b in enumerate(bursts):
        if not b: continue
        start_t = b[0][0]/25000
        end_t = b[-1][0]/25000
        raw_bytes = bytes(v for _,v in b)
        printable = ''.join(chr(v) if 32<=v<127 else f'<{v:02X}>' for v in raw_bytes)
        print(f'\n--- BURST {bi}  t={start_t:.2f}..{end_t:.2f}s  len={len(b)} ---')
        print(f'HEX  : {raw_bytes.hex(" ")}')
        print(f'ASCII: {printable}')
        # show last 12 bytes with bit patterns
        print('Last 12 bytes (LSB-first wire bits):')
        for pos, v in b[-12:]:
            wire = format(v, '08b')[::-1]
            print(f'  t={pos/25000:7.3f}s  0x{v:02X}  ascii={chr(v) if 32<=v<127 else "?"}  wire={wire}')

if __name__ == '__main__':
    main()
