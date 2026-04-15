#!/usr/bin/env python3
"""
decode_audio.py — EAS-SAMEmon
Native WAV file decoder for EAS/SAME messages.
No external tools (multimon-ng) required.

Usage:
    python tools/decode_audio.py <file.wav> [--lang EN]
"""

import sys
import os
import wave
import argparse
import numpy as np
from scipy.signal import resample

# Ensure the 'scripts' directory is in the path to import core modules
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))

from eas_demod import EASDemod
from fm_demod import AUDIO_RATE
import alertparser

def resample_wav(input_path: str) -> np.ndarray:
    """
    Reads a WAV file and returns float32 audio @ 25000 Hz.
    """
    with wave.open(input_path, 'rb') as w:
        channels = w.getnchannels()
        rate = w.getframerate()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)

    # Convert to normalized float32 [-1.0, 1.0]
    if sampwidth == 1:
        samples = (samples - 128) / 128.0
    elif sampwidth == 2:
        samples = samples / 32768.0
    elif sampwidth == 4:
        samples = samples / 2147483648.0

    # Stereo -> mono
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    elif channels > 2:
        samples = samples.reshape(-1, channels).mean(axis=1)

    # Resamplear si es necesario
    if rate != AUDIO_RATE:
        new_len = int(len(samples) * AUDIO_RATE / rate)
        # We use scipy's resample for better quality than linear interp
        samples = resample(samples, new_len).astype(np.float32)

    return samples

def main():
    parser = argparse.ArgumentParser(description="EAS-SAMEmon Native Offline Decoder")
    parser.add_argument("file", help="Path to the WAV file")
    parser.add_argument("--lang", default="EN", help="Decoding language (EN/ES)")
    parser.add_argument("--country", default=None, help="Force country (US/CA/MX)")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: The file '{args.file}' does not exist.")
        sys.exit(1)

    print(f"[*] Processing file: {args.file}")
    try:
        audio = resample_wav(args.file)
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)

    print(f"[*] Audio loaded: {len(audio)/AUDIO_RATE:.2f} s @ {AUDIO_RATE} Hz")
    print("[*] Decoding...")

    found_messages = []

    def on_frame(raw_msg: str):
        if raw_msg == 'NNNN':
            print("\n--- END OF MESSAGE (EOM) ---")
            return
        
        print(f"\n[+] RAW: {raw_msg}")
        try:
            # Reusing alertparser logic to show detailed text
            alertparser.same_decode(
                raw_msg,
                lang=args.lang,
                text=True,
                country=args.country
            )
            found_messages.append(raw_msg)
        except Exception as e:
            print(f"Error decoding frame: {e}")

    demod = EASDemod(callback=on_frame, sample_rate=AUDIO_RATE)
    
    # We process the audio in small blocks to simulate real-time pipeline behavior
    # and ensure the EASDemod state machine works correctly.
    chunk_size = AUDIO_RATE // 2 # 0.5 seconds per block
    for i in range(0, len(audio), chunk_size):
        chunk = audio[i:i + chunk_size]
        if len(chunk) < 100: continue # evitar chunks minúsculos al final
        demod.process(chunk)

    if not found_messages:
        print("\n[!] No valid EAS/SAME messages detected.")
    else:
        print(f"\n[*] Process finished. Messages detected: {len(found_messages)}")

if __name__ == '__main__':
    main()
