#!/usr/bin/env python3
"""Trace the actual demod with timestamps and squelch state to see what
happens at the boundary between carrier-on and carrier-off."""
import sys, os, wave, numpy as np
from scipy.signal import resample
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'scripts'))
from eas_demod import (FREQ_MARK, FREQ_SPACE, BAUD, SUBSAMP,
                       INTEGRATOR_MAXVAL, DLL_GAIN, SQUELCH_THRESHOLD)

def load(p, fs=25000):
    with wave.open(p, 'rb') as w:
        ch=w.getnchannels(); rate=w.getframerate(); raw=w.readframes(w.getnframes())
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
    if ch==2: s = s.reshape(-1,2).mean(1)
    if rate != fs: s = resample(s, int(len(s)*fs/rate)).astype(np.float32)
    return s

def main():
    fs = 25000
    s = load(sys.argv[1])
    corrlen = int(fs/BAUD)
    idx = np.arange(corrlen)
    cmi = np.cos(2*np.pi*FREQ_MARK/fs*idx).astype(np.float32)
    cmq = np.sin(2*np.pi*FREQ_MARK/fs*idx).astype(np.float32)
    csi = np.cos(2*np.pi*FREQ_SPACE/fs*idx).astype(np.float32)
    csq = np.sin(2*np.pi*FREQ_SPACE/fs*idx).astype(np.float32)
    mi=np.correlate(s,cmi,'valid'); mq=np.correlate(s,cmq,'valid')
    si=np.correlate(s,csi,'valid'); sq=np.correlate(s,csq,'valid')
    f_arr = mi*mi+mq*mq-si*si-sq*sq
    pwr = mi*mi+mq*mq+si*si+sq*sq
    f_seq = f_arr[::SUBSAMP]; p_seq = pwr[::SUBSAMP]
    phase_inc = BAUD*SUBSAMP/fs
    sphase=0.0; dcd_shreg=0; integ=0; lasts=0; bit_shreg=0
    sync=False; bytec=0
    last_squelch=False
    for i,f in enumerate(f_seq):
        t = (i*SUBSAMP)/fs
        sphase += phase_inc
        in_squelch = p_seq[i] < SQUELCH_THRESHOLD
        if in_squelch != last_squelch:
            print(f't={t:7.3f}s  squelch -> {"OFF" if in_squelch else "ON"}  pwr={p_seq[i]:.6f}  sync={sync}')
            last_squelch = in_squelch
        if in_squelch:
            if not sync:
                sphase=0.0; integ=0
            continue
        bn = 1 if f>0 else 0
        dcd_shreg = ((dcd_shreg<<1)|bn) & 0xFF
        if f>0 and integ<INTEGRATOR_MAXVAL: integ+=1
        elif f<0 and integ>-INTEGRATOR_MAXVAL: integ-=1
        if (dcd_shreg ^ (dcd_shreg>>1)) & 1:
            sphase += (0.5-sphase)*DLL_GAIN
        if sphase >= 1.0:
            sphase -= 1.0
            bit = 1 if integ>=0 else 0; integ=0
            bit_shreg = (bit_shreg>>1) | (0x8000 if bit else 0)
            lasts = (lasts>>1) | (0x80 if bit else 0)
            if bit_shreg == 0xABAB:
                if not sync:
                    print(f't={t:7.3f}s  >>> SYNC ON (0xABAB)')
                sync=True; bytec=0; lasts=0xAB
            elif sync:
                bytec += 1
                if bytec==8:
                    bytec=0
                    v = lasts & 0xFF
                    ch_ = chr(v) if 32<=v<127 else f'<{v:02X}>'
                    print(f't={t:7.3f}s  byte 0x{v:02X} ({ch_})  pwr={p_seq[i]:.6f}')

if __name__=='__main__': main()
