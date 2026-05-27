#!/usr/bin/env python3
"""
EAS RTL-SDR Emulator — streams NBFM-modulated EAS audio as IQ samples over RTL-TCP.

Usage:
    python eas_rtl_emulator.py audio.wav
    python eas_rtl_emulator.py audio.wav --freq 162400000 --port 1234
    python eas_rtl_emulator.py --random --interval 2
"""

import socket
import struct
import threading
import argparse
import sys
import time

import numpy as np
import scipy.signal as signal
import soundfile as sf

# ─── DEFAULTS ─────────────────────────────────────────────────────────────────
DEFAULT_SAMPLE_RATE  = 250_000
DEFAULT_CENTER_FREQ  = 162_400_000
DEFAULT_FM_DEVIATION = 5_000
DEFAULT_HOST         = "0.0.0.0"
DEFAULT_PORT         = 1234

# Chunk de 8192 muestras = 32.8 ms a 250 kHz.
# Chunks pequeños → entrega suave, sin ráfagas que saturen la cola IQ.
# (El valor original era 65536 = 262 ms — demasiado grande.)
CHUNK_SAMPLES = 8_192

MAGIC = b"RTL0"
# ──────────────────────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="EAS RTL-SDR Emulator — streams NBFM-modulated EAS audio as IQ samples over RTL-TCP"
    )
    parser.add_argument(
        "audio_file", nargs='?',
        help="Audio file to modulate and stream in loop (WAV, FLAC, OGG, etc.)"
    )
    parser.add_argument(
        "--random", action="store_true",
        help="Generates random EAS messages on the fly instead of looping a file"
    )
    parser.add_argument(
        "--interval", type=int, default=2,
        help="Seconds of silence after each message finishes (default: 2)"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Bind address (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TCP port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--freq", type=float, default=DEFAULT_CENTER_FREQ,
        help=f"Center frequency in Hz (default: {DEFAULT_CENTER_FREQ} = 162.4 MHz)"
    )
    parser.add_argument(
        "--samplerate", type=int, default=DEFAULT_SAMPLE_RATE,
        help=f"IQ sample rate in Hz (default: {DEFAULT_SAMPLE_RATE})"
    )
    parser.add_argument(
        "--deviation", type=int, default=DEFAULT_FM_DEVIATION,
        help=f"FM deviation in Hz (default: {DEFAULT_FM_DEVIATION} = ±5 kHz NBFM)"
    )
    parser.add_argument(
        "--channel", type=int, default=None, choices=range(1, 8), metavar="1-7",
        help="Emitir mensajes sólo en este canal NWR (1=162.400, 2=162.425, ..., 7=162.550). "
             "Sin este argumento, los mensajes se distribuyen aleatoriamente entre los 7 canales."
    )
    parser.add_argument(
        "--soundcard", action="store_true",
        help="Reproducir el audio banda-base por la tarjeta de sonido (mono). "
             "Modo local: NO se abre el servidor TCP. Útil para monitorear con SAME-Cast por audio."
    )
    parser.add_argument(
        "--soundcard-device", default=None,
        help="ID o nombre del dispositivo de salida (usa --list-devices para listar)."
    )
    parser.add_argument(
        "--soundcard-rate", type=int, default=48000,
        help="Sample rate de salida a la tarjeta de sonido (default: 48000)."
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="Listar dispositivos de audio disponibles y salir."
    )
    args = parser.parse_args()
    if args.list_devices:
        return args
    if not args.audio_file and not args.random:
        parser.error("You must provide either an audio_file or use --random")
    return args


def load_and_resample_audio(path: str, target_rate: int) -> np.ndarray:
    audio, orig_rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if orig_rate != target_rate:
        n_samples = int(len(audio) * target_rate / orig_rate)
        audio = signal.resample(audio, n_samples)
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio /= peak
    return audio.astype(np.float32)


def nbfm_modulate(audio: np.ndarray, sample_rate: int, deviation: int) -> np.ndarray:
    kf = deviation / sample_rate
    phase = 2 * np.pi * kf * np.cumsum(audio)
    return np.exp(1j * phase).astype(np.complex64)


class NBFMModulator:
    def __init__(self, sample_rate, deviation):
        self.kf = deviation / sample_rate
        self.last_phase = 0.0

    def modulate(self, audio: np.ndarray) -> np.ndarray:
        phase_inc = 2 * np.pi * self.kf * audio
        phase = self.last_phase + np.cumsum(phase_inc)
        self.last_phase = phase[-1] % (2 * np.pi) if len(phase) > 0 else self.last_phase
        return np.exp(1j * phase).astype(np.complex64)


class WidebandEASStreamer:
    def __init__(self, sample_rate, deviation, interval_sec=10, initial_freq=DEFAULT_CENTER_FREQ,
                 fixed_channel=None):
        """
        fixed_channel : int | None
            Si es 1-7, todos los mensajes se inyectan en ese canal NWR.
            Si es None, el canal se elige aleatoriamente en cada transmisión.
        """
        self.sample_rate = sample_rate
        self.interval_sec = interval_sec
        self.deviation = deviation
        self.fixed_channel = fixed_channel  # índice 1-7, o None = aleatorio

        self.current_tune_freq = initial_freq
        import eas_encode
        self.encoder = eas_encode.EASEncoder(sample_rate)
        
        self.nwr_channels = [162400000, 162425000, 162450000, 162475000, 162500000, 162525000, 162550000]
        # Mantenemos las 7 frecuencias emitiendo portadora NBFM continuamente!
        self.channel_state = {ch: {'audio': None, 'idx': 0, 'fm_phase': 0.0, 'shift_accum': 0.0} for ch in self.nwr_channels}
        
        self.msg_count = 0
        self.is_transmitting = False
        self.idle_since = 0 # Permitir mensaje inmediato al conectar
        self.lock = threading.Lock()

    def set_tune_freq(self, freq):
        with self.lock:
            self.current_tune_freq = freq
    def _ensure_transmissions(self):
        now = time.time()
        # Solo iniciamos un nuevo mensaje si NO hay una transmisión activa
        # y han pasado al menos self.interval_sec segundos de silencio.
        if not self.is_transmitting:
            if now - self.idle_since >= self.interval_sec:
                import eas_encode
                import random
                import datetime

                # Seleccionar canal destino
                if self.fixed_channel is not None:
                    ch_freq = self.nwr_channels[self.fixed_channel - 1]
                    tag = f"[Canal {self.fixed_channel} — {ch_freq/1e6:.3f} MHz]"
                else:
                    ch_freq = random.choice(self.nwr_channels)
                    tag = f"[Espectro Aleatorio]"

                self.msg_count += 1
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                msg_str = eas_encode.generate_random_message()
                print(f"  [{ts}] #{self.msg_count:04d} {tag} {ch_freq/1e6:.3f} MHz | {msg_str}", flush=True)
                baseband_audio = self.encoder.build_full_transmission(msg_str)

                self.channel_state[ch_freq]['audio'] = baseband_audio
                self.channel_state[ch_freq]['idx'] = 0
                self.is_transmitting = True

    def get_chunk(self, num_samples) -> bytes:
        with self.lock:
            self._ensure_transmissions()
            
            # Substrato de piso de ruido sintético
            master_iq = (np.random.randn(num_samples) + 1j * np.random.randn(num_samples)).astype(np.complex64) * 0.005

            t_array = np.arange(num_samples)
            # Para evitar saturación y recortes feos ("ruido blanco") en iq_to_uint8, la suma máxima de las 7
            # senoidales jamás debe rebasar 1.0 de amplitud.
            ch_amp = 0.8 / len(self.nwr_channels)
            
            for ch_freq, state in self.channel_state.items():
                offset_hz = ch_freq - self.current_tune_freq
                
                # Descartamos inyectar visualmente portadoras que quedan fuera del alcance paramétrico
                # físico de nuestra frecuencia maestra actual.
                if abs(offset_hz) >= self.sample_rate / 2:
                    if state['audio'] is not None:
                        state['idx'] += num_samples
                        if state['idx'] >= len(state['audio']):
                            state['audio'] = None
                    continue

                if state['audio'] is not None:
                    # Inyectando modulación de Mensaje Activa
                    start = state['idx']
                    end = start + num_samples
                    audio_slice = state['audio'][start:end]
                    
                    actual = len(audio_slice)
                    if actual < num_samples:
                        audio_slice = np.pad(audio_slice, (0, num_samples - actual))
                    
                    kf = self.deviation / self.sample_rate
                    phase_inc = 2 * np.pi * kf * audio_slice
                    fm_phase = state['fm_phase'] + np.cumsum(phase_inc)
                    state['fm_phase'] = fm_phase[-1] % (2 * np.pi) if len(fm_phase) > 0 else state['fm_phase']
                    
                    fm_iq = np.exp(1j * fm_phase).astype(np.complex64)
                    
                    state['idx'] += num_samples
                    if state['idx'] >= len(state['audio']):
                        state['audio'] = None
                else:
                    # Portadora Activa Limpia (Silent Carrier) - Así el Squelch SDR# cierra silenciosamente.
                    fm_iq = np.exp(1j * state['fm_phase']).astype(np.complex64) * np.ones(num_samples)
                
                # Rotación en el espectro (Shift analítico constante sin perder resolución float64 en el tiempo)
                phase_inc_shift = 2 * np.pi * offset_hz / self.sample_rate
                shift_phase = state['shift_accum'] + phase_inc_shift * t_array
                state['shift_accum'] = (state['shift_accum'] + phase_inc_shift * num_samples) % (2 * np.pi)
                
                shifted_iq = fm_iq * np.exp(1j * shift_phase)
                
                master_iq += shifted_iq * ch_amp
            
            # Detectar si la transmisión ha terminado para iniciar el temporizador de inactividad
            if self.is_transmitting:
                still_streaming = any(state['audio'] is not None for state in self.channel_state.values())
                if not still_streaming:
                    self.is_transmitting = False
                    self.idle_since = time.time()
                    # print("  [Log] Transmisión completada. Iniciando pausa de 2s...")
                
        return iq_to_uint8(master_iq)


def iq_to_uint8(iq: np.ndarray) -> bytes:
    # Centrado en 127.5 (protocolo RTL-TCP: uint8 sin signo, 0=−1.0, 255=+1.0)
    i_u8 = np.clip(np.real(iq) * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q_u8 = np.clip(np.imag(iq) * 127.5 + 127.5, 0, 255).astype(np.uint8)
    interleaved = np.empty(len(iq) * 2, dtype=np.uint8)
    interleaved[0::2] = i_u8
    interleaved[1::2] = q_u8
    return interleaved.tobytes()


def precompute_chunks(iq_data: np.ndarray) -> list[bytes]:
    """Pre-convierte todo el IQ a bytes uint8 en bloques de CHUNK_SAMPLES.
    Se hace una sola vez al arrancar para no gastar CPU por cliente."""
    chunks = []
    for i in range(0, len(iq_data), CHUNK_SAMPLES):
        chunks.append(iq_to_uint8(iq_data[i : i + CHUNK_SAMPLES]))
    return chunks


def send_header(conn: socket.socket):
    conn.sendall(MAGIC + struct.pack(">II", 1, 29))


def handle_client(conn: socket.socket, addr,
                  sample_rate: int, chunks: list[bytes] = None, streamer = None):
    """
    Envía los chunks pre-computados o dinámicos a velocidad real-time simulada, 
    evitando deriva de timing y estallidos de red.
    """
    print(f"[+] Client connected: {addr}", flush=True)
    chunk_dur = CHUNK_SAMPLES / sample_rate   # segundos por chunk

    try:
        send_header(conn)

        idx = 0
        n   = len(chunks) if chunks else 0
        deadline = time.monotonic()

        while True:
            if streamer:
                data = streamer.get_chunk(CHUNK_SAMPLES)
            else:
                data = chunks[idx]
                idx  = (idx + 1) % n

            conn.sendall(data)

            # Avanzar el deadline en la duración exacta del chunk.
            # Si sendall tardó más de lo esperado, el siguiente sleep
            # será más corto (o cero) para recuperar el ritmo.
            deadline += chunk_dur
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            # Si remaining < 0 estamos atrasados — no dormir, continuar
            # enviando para alcanzar el ritmo real-time.

    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        print(f"[-] Client disconnected: {addr}", flush=True)
        conn.close()


def drain_commands(conn: socket.socket, streamer_or_none=None):
    try:
        conn.settimeout(0.1)
        while True:
            try:
                data = conn.recv(5)
                if not data:
                    break
                cmd, val = struct.unpack(">BI", data[:5])
                if cmd == 0x01: # CMD_SET_FREQ
                    if hasattr(streamer_or_none, 'set_tune_freq'):
                        streamer_or_none.set_tune_freq(val)
            except socket.timeout:
                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except:
            pass


def run_soundcard(args):
    """Modo local: genera audio banda-base y lo reproduce por la tarjeta de sonido.
    No abre socket TCP. Usa su propio EASEncoder a la tasa de la soundcard."""
    import sounddevice as sd
    import datetime
    SR = args.soundcard_rate

    if args.soundcard_device is not None:
        try:
            sd.default.device = (None, int(args.soundcard_device))
        except ValueError:
            sd.default.device = (None, args.soundcard_device)

    print(f"\nModo SOUNDCARD (sin TCP)")
    print(f"  Sample rate : {SR} Hz")
    print(f"  Device      : {args.soundcard_device or 'default'}")
    print("  Presiona Ctrl+C para detener\n")

    if args.audio_file:
        audio = load_and_resample_audio(args.audio_file, SR)
        while True:
            sd.play(audio, SR, blocking=True)
            time.sleep(args.interval)
        return

    import eas_encode
    encoder = eas_encode.EASEncoder(SR)
    nwr_channels = [162400000, 162425000, 162450000, 162475000, 162500000, 162525000, 162550000]
    msg_count = 0
    while True:
        import random
        if args.channel is not None:
            ch_freq = nwr_channels[args.channel - 1]
            tag = f"[Canal {args.channel} — {ch_freq/1e6:.3f} MHz]"
        else:
            ch_freq = random.choice(nwr_channels)
            tag = f"[{ch_freq/1e6:.3f} MHz]"
        msg_count += 1
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg_str = eas_encode.generate_random_message()
        print(f"  [{ts}] #{msg_count:04d} {tag} | {msg_str}", flush=True)
        baseband = encoder.build_full_transmission(msg_str).astype(np.float32)
        peak = np.max(np.abs(baseband))
        if peak > 0:
            baseband = baseband / peak * 0.9
        sd.play(baseband, SR, blocking=True)
        time.sleep(args.interval)


def main():
    args = parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    if args.soundcard:
        try:
            run_soundcard(args)
        except KeyboardInterrupt:
            print("\n[!] Detenido.")
        return

    chunks = None
    streamer = None

    if args.random:
        center_freq = args.freq if args.freq != 162400000 else 162475000
        if args.channel is not None:
            ch_freq = [162400000, 162425000, 162450000, 162475000, 162500000, 162525000, 162550000][args.channel - 1]
            print(f"Modo Canal Único. Sintetizando NWR en canal {args.channel} ({ch_freq/1e6:.3f} MHz) cada {args.interval}s")
        else:
            print(f"Modo Espectro Ancho (7 Canales). Sintetizando NWR cada {args.interval}s")
        streamer = WidebandEASStreamer(args.samplerate, args.deviation, args.interval,
                                       initial_freq=center_freq, fixed_channel=args.channel)
        args.freq = center_freq
    else:
        print(f"Loading: {args.audio_file}")
        audio = load_and_resample_audio(args.audio_file, args.samplerate)
        dur   = len(audio) / args.samplerate
        print(f"  {dur:.1f}s resampled to {args.samplerate} Hz")

        print("Modulating NBFM...")
        iq = nbfm_modulate(audio, args.samplerate, args.deviation)

        print(f"Pre-computing IQ→uint8 chunks ({CHUNK_SAMPLES} samples = "
              f"{CHUNK_SAMPLES/args.samplerate*1000:.0f} ms each)...")
        chunks = precompute_chunks(iq)
        print(f"  {len(chunks)} chunks listos")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(1)
    srv.settimeout(1.0)

    print(f"\nEAS RTL-SDR Emulator listening on {args.host}:{args.port}")
    print(f"  Center freq : {args.freq/1e6:.3f} MHz")
    print(f"  Sample rate : {args.samplerate/1e3:.0f} kHz")
    print(f"  FM deviation: ±{args.deviation/1e3:.1f} kHz")
    print(f"  Chunk size  : {CHUNK_SAMPLES} samples ({CHUNK_SAMPLES/args.samplerate*1000:.0f} ms)")
    if args.random:
        if args.channel:
            print(f"  Mode        : SINGLE CHANNEL {args.channel} — {streamer.nwr_channels[args.channel-1]/1e6:.3f} MHz (Intervalo {args.interval}s)")
        else:
            print(f"  Mode        : RANDOM 7CH (Intervalo {args.interval}s)")
    else:
        print(f"  Audio file  : {args.audio_file} (loop)")
    print("  [!] NOTA: Los mensajes EAS y el espectro sólo se generan/imprimen")
    print("            cuando el cliente (SAME-Cast) está conectado y leyendo.")
    print("  Presiona Ctrl+C para detener\n")

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(
                target=handle_client,
                args=(conn, addr, args.samplerate, chunks, streamer),
                daemon=True
            ).start()
            threading.Thread(
                target=drain_commands,
                args=(conn, streamer),
                daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\n[!] Detenido.")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
