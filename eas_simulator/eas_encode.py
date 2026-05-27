import os
import math
import argparse
import random
import datetime
import numpy as np
import scipy.io.wavfile as wavfile
import scipy.signal as signal

import sys
# Añadir el directorio scripts al path para importar definiciones de países
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if root_dir not in sys.path:
    sys.path.append(root_dir)

try:
    import mx_defs
    import us_defs
    import ca_defs
except ImportError:
    mx_defs = us_defs = ca_defs = None

# Constantes del estándar SAME/EAS (alineadas con eas_demod.py)
FREQ_MARK  = 2083.3      # Hz — bit 1
FREQ_SPACE = 1562.5      # Hz — bit 0
BAUD       = 520.833333  # baudios (exactamente 48 muestras @ 25kHz)
PREAMBLE   = 0xAB        # 10101011 (transmitido LSB first)

class EASEncoder:
    def __init__(self, sample_rate=22050):
        self.sample_rate = sample_rate

    def _text_to_bits(self, text: str) -> list:
        """Convierte texto ASCII a tren de bits LSB-first."""
        bits = []
        for char in text:
            val = ord(char)
            # LSB first
            for i in range(8):
                bits.append((val >> i) & 1)
        return bits

    def _preamble_bits(self) -> list:
        """Genera el tren de bits correspondiente a 16 bytes de preámbulo."""
        bits = []
        for _ in range(16):
            val = PREAMBLE
            for i in range(8):
                bits.append((val >> i) & 1)
        return bits

    def encode_burst(self, msg: str) -> np.ndarray:
        """Codifica una única ráfaga (preámbulo + mensaje) a audio AFSK."""
        bits = self._preamble_bits() + self._text_to_bits(msg)
        
        # Calcular el tiempo total y la cantidad de muestras
        total_time = len(bits) / BAUD
        total_samples = int(math.ceil(total_time * self.sample_rate))
        
        # Mapear cada muestra al índice del bit correspondiente
        time_arr = np.arange(total_samples) / self.sample_rate
        bit_indices = np.floor(time_arr * BAUD).astype(int)
        
        # Evitar out of bounds por precisión de flotantes
        bit_indices[bit_indices >= len(bits)] = len(bits) - 1
        
        bit_array = np.array(bits)
        sample_bits = bit_array[bit_indices]
        
        # Asignar frecuencia base según el bit
        freqs = np.where(sample_bits == 1, FREQ_MARK, FREQ_SPACE).astype(np.float32)
        
        # Suavizar las transiciones de frecuencia (GFSK-like)
        nyq_f = self.sample_rate / 2.0
        sos_f = signal.butter(2, 3000.0 / nyq_f, btype='low', output='sos')
        freqs_smooth = signal.sosfiltfilt(sos_f, freqs)
        
        # Integrar la frecuencia suavizada para obtener pre-fase continua
        phases = 2 * np.pi * freqs_smooth / self.sample_rate
        accumulated_phase = np.cumsum(phases)
        
        # Generar oscilación
        wav_data = np.sin(accumulated_phase).astype(np.float32)
        
        # Aplicar fade-in / fade-out (2 ms) para evitar clics de concatenación
        fade_len = int(self.sample_rate * 0.002)
        if len(wav_data) > 2 * fade_len:
            fade_in = np.linspace(0, 1, fade_len, dtype=np.float32)
            fade_out = np.linspace(1, 0, fade_len, dtype=np.float32)
            wav_data[:fade_len] *= fade_in
            wav_data[-fade_len:] *= fade_out
            
        return wav_data

    def build_full_transmission(self, msg: str, include_eom: bool = True) -> np.ndarray:
        """
        Construye la transmisión completa en cumplimiento con EAS:
        [Ráfaga] - [Silencio 1s] - [Ráfaga] - [Silencio 1s] - [Ráfaga]
        Opcionalmente agrega ráfagas EOM.
        """
        burst = self.encode_burst(msg)
        silence = np.zeros(self.sample_rate, dtype=np.float32) # 1 segundo exacto de silencio
        
        parts = [
            burst, silence,
            burst, silence,
            burst
        ]
        
        if include_eom:
            parts.append(silence)
            # Agregar EOM burst (preámbulo + NNNN) x 3
            eom_burst = self.encode_burst("NNNN")
            parts.extend([
                silence, eom_burst,
                silence, eom_burst,
                silence, eom_burst
            ])
            
        full_audio = np.concatenate(parts)
        return full_audio


def generate_random_message() -> str:
    country = random.choices(['US', 'MX', 'CA'], weights=[40, 40, 20])[0]

    if country == 'MX' and mx_defs:
        org  = random.choice(list(mx_defs.MX_ORG_NAMES.keys()))
        eee  = random.choice(list(mx_defs.MX_EVENTS.keys()))
        # EQW usa cobertura nacional (000000); RWT usa áreas específicas
        if eee == 'EQW':
            base_areas = [mx_defs.MX_ALL_AREA]
        else:
            base_areas = [mx_defs.MX_ALL_AREA] + list(mx_defs.MX_SAME_CODE.keys())
        tttt    = mx_defs.MX_DURATIONS.get(eee, '0030')
        station = random.choice(list(mx_defs.MX_TRANSMITTERS.keys()))

    elif country == 'CA' and ca_defs:
        org        = random.choice(['WXR', 'CIV', 'EAS'])
        eee        = random.choice(['RWT', 'RMT', 'ADR', 'TOR', 'BZW', 'EQW', 'HUW', 'FFW'])
        base_areas = list(ca_defs.CA_SAME_CODE.keys())
        tttt       = '0300' if eee == 'RWT' else random.choice(['0015', '0030', '0100', '0045'])
        station    = random.choice(['CBLA/FM', 'CFRN/AM', 'CKAC/FM', 'CJAD/AM'])

    else:  # US
        org        = random.choice(['PEP', 'WXR', 'EAS', 'CIV'])
        eee        = random.choice(['RWT', 'RMT', 'ADR', 'TOR', 'SVR', 'BZW', 'EQW',
                                    'FFW', 'HUW', 'WSW', 'WFW', 'FRW'])
        base_areas = list(us_defs.US_SAME_CODE.keys()) if us_defs else ['006037', '048453', '036061']
        tttt       = '0300' if eee == 'RWT' else random.choice(['0015', '0030', '0100', '0045'])
        station    = random.choice(['WXYZ/FM', 'KABC/AM', 'KBOI/FM', 'WNTV/DT'])

    # 1–6 áreas al azar (realista; el protocolo SAME permite hasta 31)
    num_areas    = random.randint(1, 6)
    chosen_areas = random.sample(base_areas, min(num_areas, len(base_areas)))

    # Asegurar formato PSSCCC (6 dígitos con P=0 por defecto)
    formatted_areas = []
    for code in chosen_areas:
        if len(code) == 5:
            formatted_areas.append("0" + code)
        elif len(code) == 6:
            formatted_areas.append(code)
        else:
            formatted_areas.append(code.zfill(6))

    areas_str = "-".join(formatted_areas)

    now      = datetime.datetime.now(datetime.timezone.utc)
    jjjhhmm = now.strftime('%j%H%M')

    return f"ZCZC-{org}-{eee}-{areas_str}+{tttt}-{jjjhhmm}-{station}-"

def main():
    parser = argparse.ArgumentParser(description="Codificador EAS-SAME a WAV (AFSK)")
    parser.add_argument("--msg", type=str, required=False,
                        help="Mensaje SAME (ej. ZCZC-CIV-EQW-000000+0001-3001723-XCMX/011-)")
    parser.add_argument("--random", action="store_true",
                        help="Genera un mensaje EAS aleatorio válido en lugar de proveer --msg")
    parser.add_argument("--out", type=str, default="eas_encoded.wav",
                        help="Archivo de salida .wav")
    parser.add_argument("--rate", type=int, default=22050,
                        help="Sample rate (default: 22050 Hz)")
    
    args = parser.parse_args()
    
    if args.random:
        msg_str = generate_random_message()
        print(f"Generando mensaje aleatorio:\n{msg_str}\n")
    elif args.msg:
        msg_str = args.msg
    else:
        parser.error("Debes proveer --msg o usar la bandera --random")
    
    if not msg_str.startswith("ZCZC"):
        print("Advertencia: El mensaje generalmente debería empezar con ZCZC")
    
    encoder = EASEncoder(sample_rate=args.rate)
    audio_float = encoder.build_full_transmission(msg_str, include_eom=True)
    
    # Escalar a int16 para escribir el WAV
    audio_int16 = (audio_float * 32767).astype(np.int16)
    
    # Crear directorio si es necesario
    out_dir = os.path.dirname(args.out)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)
        
    wavfile.write(args.out, args.rate, audio_int16)
    print(f"Archivo codificado exitosamente: {args.out}")
    print(f"Duración: {len(audio_int16) / args.rate:.2f} segundos")

if __name__ == "__main__":
    main()
