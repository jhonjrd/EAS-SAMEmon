"""
  ┌────────────────────────▼────────────────────────────────────┐
  │  Thread 3: Decoder + Display (main thread)                  │
  │  alertparser.same_decode() → WebDashboard / SasmexDisplay         │
  └─────────────────────────────────────────────────────────────┘

Supported signal sources:
  --host  HOST[:PORT]     Remote RTL-TCP (external or local manual server)
  --device N              Local USB RTL-SDR via pyrtlsdr (no rtl_tcp needed)

Display:
  Default: web server at http://localhost:PORT  (WebDashboard)
  --no-web    : Rich panel in terminal            (SasmexDisplay)
  --web-port N: web server port (default 8080)

Usage:
  python pipeline.py --device 0 --channel 3 --gain 40       # local USB, ch3 (162.450)
  python pipeline.py --host 192.168.1.10 --channel 3 --gain 40  # remote RTL-TCP
  python pipeline.py --host localhost --channel 1 --event EQW   # filter seismic only
  python pipeline.py --device 0 --no-web                    # terminal panel
  python pipeline.py --device 0 --web-port 9090             # different port
"""

import sys
import os
import queue
import threading
import logging
import argparse
import datetime
import json

import numpy as np

# Internal EAS-SAMEmon modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
from rtl_source    import RtlTcpSource
from local_source  import PyRtlSdrSource
from fm_demod      import FMDemod, AUDIO_RATE
from eas_demod     import EASDemod
from display        import SasmexDisplay
from web_dashboard  import WebDashboard
from audio_monitor  import AudioMonitor, MessageAudioRecorder
from event_store    import EventStore
import alertparser
import mx_defs
from integrations import AlertDispatcher

log = logging.getLogger('EAS-SAMEmon')

PREFS_FILE = 'preferences.json'

def load_preferences() -> dict:
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Error loading preferences: {e}")
    return {}

# Lock to prevent multiple threads from writing to the JSON preference file simultaneously
prefs_lock = threading.Lock()

def save_preference(key, value):
    with prefs_lock:
        prefs = load_preferences()
        prefs[key] = value
        try:
            # Atomic write: first to a temp file, then replace the original
            tmp_file = f"{PREFS_FILE}.tmp"
            with open(tmp_file, 'w') as f:
                json.dump(prefs, f, indent=2)
            # os.replace is atomic on most modern operating systems
            os.replace(tmp_file, PREFS_FILE)
            log.info(f"Preference saved: {key} = {value}")
            print(f"[*] Setting persisted in {PREFS_FILE}: {key}")
        except Exception as e:
            log.warning(f"Error saving preferences: {e}")
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

# ---------------------------------------------------------------------------
# SASMEX transmitter frequencies (quick reference)
# ---------------------------------------------------------------------------
SASMEX_FREQS = {
    'teuhitl':   162.450,
    'cuajimalpa': 162.500,
    'zacatenco':  162.500,
    'la_palma':   162.525,
    'cenapred':   162.400,
}


# ---------------------------------------------------------------------------
# DSP Thread — FM demod + EAS demod
# ---------------------------------------------------------------------------

class DSPWorker(threading.Thread):
    """
    Consumes IQ chunks from iq_queue, applies FMDemod + EASDemod on a single channel,
    and feeds the audio monitor.
    """

    def __init__(self, iq_queue: queue.Queue, msg_queue: queue.Queue,
                 sample_rate: int, display,
                 audio_monitor: AudioMonitor | None = None,
                 msg_recorder: 'MessageAudioRecorder | None' = None):
        super().__init__(name='dsp-worker', daemon=True)
        self.iq_queue    = iq_queue
        self.msg_queue   = msg_queue
        self.display     = display
        self._monitor    = audio_monitor
        self._recorder   = msg_recorder
        self.sample_rate = sample_rate
        self._running    = True

        self.fm  = FMDemod(fs_in=sample_rate)
        self.eas = EASDemod(callback=self._on_frame, sample_rate=AUDIO_RATE)

    def _on_frame(self, raw_msg: str):
        try:
            self.msg_queue.put_nowait((raw_msg, 0.0))
        except queue.Full:
            log.warning('msg_queue full — dropping EAS frame')

    def run(self):
        while self._running:
            try:
                iq = self.iq_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                audio = self.fm.process(iq)
                self.eas.process(audio)
                if self._monitor:  self._monitor.feed(audio)
                if self._recorder: self._recorder.feed(audio)
                self.display.tick()
            except Exception as e:
                log.error(f'DSP error: {e}', exc_info=True)

    def stop(self):
        self._running = False




# ---------------------------------------------------------------------------
# Main thread — EAS-SAMEmon decoder + display
# ---------------------------------------------------------------------------

def decoder_loop(msg_queue: queue.Queue, display,
                 event_filter: list, same_filter: list,
                 json_dir: str | None,
                 msg_recorder: 'MessageAudioRecorder | None' = None,
                 event_store:  'EventStore | None' = None,
                 dispatcher:   'AlertDispatcher | None' = None):
    """
    Takes raw frames from msg_queue, processes them through alertparser.same_decode(),
    and updates the display.
    """
    while True:
        try:
            payload = msg_queue.get(timeout=1.0)
            if isinstance(payload, tuple):
                raw, ch_freq = payload
            else:
                # Safety fallback
                raw, ch_freq = payload, 0.0
        except queue.Empty:
            continue

        display.log(f'Frame EAS: {raw}')

        # Skip EOM frames silently — NNNN is end-of-message, not a SAME header
        if raw.strip() == 'NNNN':
            continue

        try:
            received_at = datetime.datetime.now(datetime.timezone.utc)
            result = _decode_frame(raw, event_filter, same_filter)
            if result:
                # Timestamp system time before passing to display and store
                result['received_at'] = received_at.isoformat()
                if ch_freq > 0.0:
                    result['channel_mhz'] = ch_freq

                display.add_message(result)

                if json_dir:
                    _save_json(result, json_dir)

                # Persist in history
                if event_store:
                    event_store.save(result)

                # Start recording the message audio
                if msg_recorder:
                    msg_recorder.trigger(
                        event_code  = result.get('EEE', 'UNK'),
                        received_at = received_at,
                    )

                # External Integrations (Home Assistant Webhook)
                if dispatcher:
                    dispatcher.dispatch(result)
            else:
                reason = _diagnose_frame(raw, event_filter, same_filter)
                display.log(f'Frame descartado — {reason}', 'warn')
        except Exception as e:
            log.error(f'Decode error: {e}', exc_info=True)
            display.log(f'Error decodificando frame: {e}', 'error')


def _diagnose_frame(raw: str, event_filter: list, same_filter: list) -> str:
    """Returns a human-readable reason why _decode_frame returned None."""
    try:
        cleaned = alertparser.clean_msg(raw)
    except Exception as e:
        return f'clean_msg falló: {e}'
    if 'ZCZC' not in cleaned:
        return f'ZCZC no encontrado tras clean_msg (resultado={cleaned[:50]!r})'
    tail = cleaned[cleaned.find('ZCZC'):]
    if '+' not in tail:
        return f'falta separador "+" en el frame (tail={tail[:60]!r})'
    s1, s2 = tail.split('+', 1)
    parts_s1 = s1.split('-')
    if len(parts_s1) < 4:
        return f's1 tiene {len(parts_s1)} campos, se esperan ≥4 (s1={s1!r})'
    EEE = parts_s1[2]
    PSSCCC = parts_s1[3]
    PSSCCC_list = [c for c in PSSCCC.split('-') if c]
    if event_filter and EEE not in event_filter:
        return f'filtrado por event_filter: EEE={EEE!r} no está en {event_filter}'
    if same_filter:
        stripped_watch = [w[1:] for w in same_filter]
        stripped_codes = [c[1:] for c in PSSCCC_list]
        if not (set(stripped_watch) & set(stripped_codes)):
            return f'filtrado por same_filter: áreas={PSSCCC_list}, filtro={same_filter}'
    return 'razón desconocida (el frame pareció válido — revisar excepciones)'


def _decode_frame(raw: str, event_filter: list,
                  same_filter: list) -> dict | None:
    """
    Calls alertparser.same_decode() in silent mode and returns
    the message data dict, or None if filtered.
    """
    # Clean and parse the message
    try:
        cleaned = alertparser.clean_msg(raw)
    except Exception:
        return None

    if 'ZCZC' not in cleaned:
        return None

    try:
        s1, s2 = cleaned[cleaned.find('ZCZC'):].split('+', 1)
    except ValueError:
        return None

    try:
        _, ORG, EEE, PSSCCC = s1.split('-', 3)
    except ValueError:
        return None

    PSSCCC_list = [c for c in PSSCCC.split('-') if c]

    parts = s2.split('-', 3)
    while len(parts) < 4:
        parts.append('')
    TTTT, JJJHHMM, LLLLLLLL, _ = parts

    # Detect country
    COUNTRY = alertparser.detect_country(PSSCCC_list, LLLLLLLL, ORG)

    # Apply filters
    if event_filter and EEE not in event_filter:
        return None
    if same_filter:
        stripped_watch  = [w[1:] for w in same_filter]
        stripped_codes  = [c[1:] for c in PSSCCC_list]
        if not (set(stripped_watch) & set(stripped_codes)):
            return None

    # Transmitter info
    tx_info = mx_defs.get_mx_transmitter_info(LLLLLLLL) if COUNTRY == 'MX' else {}

    # Calculate timestamps
    try:
        start = alertparser.alert_start(JJJHHMM)
        end   = alertparser.alert_end(JJJHHMM, TTTT)
    except Exception:
        start = end = datetime.datetime.now(datetime.timezone.utc)

    areas_decoded = []
    for c in PSSCCC_list:
        place, state = alertparser.county_decode(c, COUNTRY)
        areas_decoded.append({'code': c, 'place': place, 'state': state})

    return {
        'ORG':          ORG,
        'EEE':          EEE,
        'TTTT':         TTTT,
        'JJJHHMM':      JJJHHMM,
        'LLLLLLLL':     LLLLLLLL,
        'COUNTRY':      COUNTRY,
        'event':           alertparser.get_event(EEE, 'EN'),
        'event_es':        alertparser.get_event(EEE, 'ES'),
        'organization':    alertparser.get_org_name(ORG, COUNTRY, 'EN'),
        'organization_es': alertparser.get_org_name(ORG, COUNTRY, 'ES'),
        'start':        alertparser.fn_dt(start),
        'end':          alertparser.fn_dt(end),
        'start_dt':     start.isoformat(),
        'end_dt':       end.isoformat(),
        'length':       alertparser.get_length(TTTT),
        'seconds':      alertparser.alert_length(TTTT),
        'PSSCCC_list':  PSSCCC_list,
        'areas_decoded': areas_decoded,
        'transmitter':  tx_info,
        'MESSAGE':      raw,
    }


def _save_json(msg: dict, json_dir: str):
    ts  = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    eee = msg.get('EEE', 'UNK')
    path = os.path.join(json_dir, f'sasmex_{eee}_{ts}.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            data = {k: v for k, v in msg.items()
                    if k != '_received'}        # non-serializable
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.error(f'Error saving JSON: {e}')


# ---------------------------------------------------------------------------
# RTL-SDR USB local — auto-spawn rtl_tcp
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_rate(value: str) -> int:
    """
    Converts --rate argument to integer Hz.
    Accepts:
        '2.4'       → 2,400,000 Hz  (MSps, value < 100)
        '1.024'     → 1,024,000 Hz
        '0.25'      → 250,000 Hz
        '250000'    → 250,000 Hz    (Direct Hz, value >= 100)
        '1024000'   → 1,024,000 Hz
    Valid RTL-SDR range: 0.225 – 3.2 MSps
    """
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f'Invalid sample rate: {value!r}. Use MSps (e.g., 2.4) or Hz (e.g., 2400000)'
        )

    # If the value is less than 100, we assume MSps
    if v < 100:
        hz = int(v * 1_000_000)
    else:
        hz = int(v)

    # Validate RTL-SDR range
    if not (225_000 <= hz <= 3_200_000):
        raise argparse.ArgumentTypeError(
            f'Sample rate {hz:,} Hz out of RTL-SDR range (0.225 – 3.2 MSps). '
            f'Received: {value!r}'
        )
    return hz


def _parse_gain(value: str) -> float:
    """
    Converts --gain argument to float.
    Accepts:
        'auto' or '0'  → 0.0  (Dongle Automatic AGC)
        '35'          → 35.0 dB (Manual gain)
    """
    if isinstance(value, str) and value.lower() == 'auto':
        return 0.0
    try:
        g = float(value)
        if g < 0:
            raise argparse.ArgumentTypeError(f'Invalid gain: {value!r}. Use a number >= 0 or "auto".')
        return g
    except ValueError:
        raise argparse.ArgumentTypeError(f'Invalid gain: {value!r}. Use a number (dB) or "auto".')


def _parse_host_port(value: str, default_port: int = 1234):
    """
    Accepts 'host', 'host:port' or 'ip:port'.
    Valid examples:
        localhost
        192.168.1.10
        192.168.1.10:1234
        my-raspi.local:5555
    """
    if ':' in value:
        host, port_str = value.rsplit(':', 1)
        try:
            return host, int(port_str)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f'Invalid port in "{value}". Format: host:port'
            )
    return value, default_port


def parse_args():
    p = argparse.ArgumentParser(
        prog='pipeline.py',
        description='EAS-SAMEmon — Real-time SASMEX receiver via RTL-TCP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Local USB RTL-SDR (pyrtlsdr — requires librtlsdr.dll)
  python pipeline.py --device 0 --channel 3 --gain 40
  python pipeline.py --device 0 --channel 1 --tuner-agc --audio

  # Remote RTL-TCP
  python pipeline.py --host 192.168.1.10:1234 --channel 3 --gain 35
  python pipeline.py --host 10.0.0.5:5555 --channel 7 --gain 35 --ppm -3

  # List connected USB dongles
  python pipeline.py --list-devices-usb
        """,
    )
    # --- Signal Source (mutually exclusive) ---
    src = p.add_mutually_exclusive_group()
    src.add_argument('--host', default=None, metavar='HOST[:PORT]',
                     help='Remote RTL-TCP. Accepts "host" or "host:port" '
                          '(defaults to localhost:1234 if --device is not used)')
    src.add_argument('--device', default=None, type=int, metavar='N',
                     help='Local USB RTL-SDR, device index (e.g.: 0). '
                          'Uses pyrtlsdr directly — no rtl_tcp or external processes required.')

    p.add_argument('--port', default=1234, type=int,
                   help='RTL-TCP port when --host is used (default: 1234). '
                        'Ignored if --host includes ":port"')
    p.add_argument('--list-devices-usb', action='store_true', default=False,
                   dest='list_devices_usb',
                   help='List connected USB RTL-SDR dongles and exit')
    p.add_argument('--freq', default=162.400, type=float,
                   help='Frequency in MHz (default: 162.400 — NWR Channel 1)')
    p.add_argument('--channel', default=None, type=int, choices=range(1, 8), metavar='1-7',
                   help='NWR Channel: 1=162.400, 2=162.425, 3=162.450, 4=162.475, '
                        '5=162.500, 6=162.525, 7=162.550. Shortcut for --freq.')
    p.add_argument('--rate', default='0.25', metavar='RATE',
                   help='Sample rate: MSps (e.g., 2.4, 1.024, 0.25) or Hz (e.g., 250000). '
                        'RTL-SDR range: 0.225–3.2 MSps  (default: 0.25 = 250,000 Hz)')
    p.add_argument('--gain', default='40', metavar='GAIN',
                   help='Gain in dB or "auto" (default: 40). E.g.: --gain 35 | --gain auto')
    p.add_argument('--ppm',     default=0, type=int,
                   help='Frequency correction in PPM (default: 0)')
    p.add_argument('--tuner-agc', dest='tuner_agc', action='store_true', default=True,
                   help='Enable Tuner AGC (automatic gain control). '
                        'Equivalent to --gain auto')
    p.add_argument('--rtl-agc', dest='rtl_agc', action='store_true', default=False,
                   help='Enable RTL AGC (internal digital gain of RTL2832U)')
    p.add_argument('--event',   nargs='*', default=None,
                   help='Filter by event code (e.g., --event EQW RWT)')
    p.add_argument('--same',    nargs='*', default=None,
                   help='Filter by SAME area code')
    p.add_argument('--json-dir', default=None, dest='json_dir',
                   help='Directory to save JSON alerts')
    p.add_argument('--loglevel', default='WARNING',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])

    # --- Persistent History ---
    p.add_argument('--db', default='alerts_history.db', dest='db_path',
                   metavar='PATH',
                   help='Path to the SQLite file for event history '
                        '(default: alerts_history.db). Accessible from the '
                        'web dashboard with date filters.')
    p.add_argument('--no-db', action='store_true', dest='no_db', default=False,
                   help='Disable persistent history storage')

    # --- Web Dashboard ---
    p.add_argument('--web-port', default=8080, type=int, dest='web_port',
                   metavar='PORT',
                   help='Web server port (default: 8080). '
                        'Open http://localhost:PORT in your browser.')
    p.add_argument('--no-web', action='store_true', default=False, dest='no_web',
                   help='Disable web dashboard and use terminal Rich panel.')

    # Quick reference for SASMEX transmitters
    p.add_argument('--transmitter', '--transmisor', choices=list(SASMEX_FREQS.keys()), default=None,
                   help='Frequency shortcut for SASMEX transmitters: teuhitl | cenapred | cuajimalpa | ...')

    # --- FM Audio Monitoring ---
    audio = p.add_argument_group('FM audio monitoring')
    audio.add_argument('--audio', action='store_true', default=False,
                       help='Play demodulated FM audio in real-time')
    audio.add_argument('--audio-device', default=None, metavar='ID',
                       help='Audio device index or name (see --list-devices)')
    audio.add_argument('--volume', default=0.8, type=float, metavar='0.0-1.0',
                       help='Playback volume 0.0–1.0 (default: 0.8)')
    audio.add_argument('--record', default=None, metavar='FILE.wav',
                       help='Record demodulated FM audio to a WAV file')
    audio.add_argument('--list-devices', action='store_true', default=False,
                       help='List available audio devices and exit')
    audio.add_argument('--save-audiomsgs', default=None, metavar='DIR',
                       dest='save_audiomsgs',
                       help='Directory to save a WAV for each received EAS message. '
                            'Format: {EEE}_{YYYYMMDD}_{HHMMSS}.wav  '
                            'Mono 16-bit 25000 Hz. '
                            'Includes ~15s before and ~90s after detection.')

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.loglevel),
                        format='%(levelname)s [%(name)s] %(message)s')

    # List USB dongles and exit
    if args.list_devices_usb:
        devs = PyRtlSdrSource.list_devices()
        if devs:
            print('\nConnected USB RTL-SDR Dongles:\n')
            for d in devs:
                print(f"  [{d['index']}] {d['name']}")
        else:
            print('\nNo RTL-SDR dongles found (or pyrtlsdr is not installed).\n'
                  '  → pip install pyrtlsdr\n')
        print()
        return

    # List audio devices and exit
    if args.list_devices:
        devs = AudioMonitor.list_devices()
        print('\nAvailable audio devices:\n')
        for d in devs:
            marker = ' [default]' if d['default'] else ''
            print(f"  [{d['index']:2d}] {d['name']}{marker}")
        print()
        return

    # ── Resolve signal source ───────────────────────────────────────────────
    #
    # Mode A — Remote RTL-TCP:  --host HOST[:PORT]
    #           RtlTcpSource connects to external server.
    #
    # Mode B — Local USB RTL-SDR:  --device N
    #           PyRtlSdrSource talks directly to the dongle via pyrtlsdr.
    #           No external processes needed.

    # ── Override with Persistent Preferences ──────────────────────────────────
    prefs = load_preferences()

    try:
        sample_rate = _parse_rate(args.rate)
    except argparse.ArgumentTypeError as e:
        print(f'Error: {e}')
        return

    # Initialize external integrations (Home Assistant)
    dispatcher = AlertDispatcher(prefs.get('integrations', {}))

    # Standard NWR channel map
    NWR_CHANNELS = [162.400, 162.425, 162.450, 162.475, 162.500, 162.525, 162.550]

    freq_mhz = prefs.get('freq_mhz', args.freq)
    if args.channel is not None:
        freq_mhz = NWR_CHANNELS[args.channel - 1]
    if args.transmitter:
        freq_mhz = SASMEX_FREQS[args.transmitter]

    freq_hz = int(freq_mhz * 1e6)
    physical_freq_hz = freq_hz

    gain = prefs.get('gain', _parse_gain(args.gain))
    ppm = prefs.get('ppm', args.ppm)
    tuner_agc = prefs.get('tuner_agc', args.tuner_agc)
    rtl_agc = prefs.get('rtl_agc', args.rtl_agc)

    source_kwargs = dict(
        freq        = physical_freq_hz,
        sample_rate = sample_rate,
        gain        = gain,
        tuner_agc   = tuner_agc,
        rtl_agc     = rtl_agc,
    )

    # Dual Hardware Configuration (USB and TCP)
    hw_usb = prefs.get('hardware_usb', {'mode': 'USB', 'device': 0, 'ppm': 0})
    hw_tcp = prefs.get('hardware_tcp', {'mode': 'TCP', 'host': 'localhost', 'port': 1234, 'ppm': 0})
    
    # Determine initial mode: CLI takes precedence; if no CLI, saved preference wins
    if args.host:
        last_mode = 'TCP'
    elif args.device is not None:
        last_mode = 'USB'
    else:
        last_mode = prefs.get('hardware_last_mode', 'TCP')

    # Priority 1: Explicit Command Line Arguments (CLI)
    if args.device is not None:
        device_idx = args.device
        ppm = hw_usb.get('ppm', 0)
        source = PyRtlSdrSource(device_index=device_idx, ppm=ppm, **source_kwargs)
        source_label = f'USB:{device_idx}'
        last_mode = 'USB'
        # Update USB memory with new CLI index
        hw_usb.update({'device': device_idx, 'ppm': ppm})
    elif args.host is not None:
        host, port = _parse_host_port(args.host, default_port=args.port)
        ppm = hw_tcp.get('ppm', 0)
        source = RtlTcpSource(host=host, port=port, ppm=ppm, **source_kwargs)
        source_label = f'{host}:{port}'
        last_mode = 'TCP'
        # Update TCP memory with new CLI host/port
        hw_tcp.update({'host': host, 'port': port, 'ppm': ppm})
    
    # Priority 2: Saved Preference (based on last used mode)
    elif last_mode == 'USB':
        device_idx = hw_usb.get('device', 0)
        source = PyRtlSdrSource(device_index=device_idx, ppm=hw_usb.get('ppm', 0), **source_kwargs)
        source_label = f'USB:{device_idx}'
    else:
        host = hw_tcp.get('host', 'localhost')
        port = hw_tcp.get('port', 1234)
        source = RtlTcpSource(host=host, port=port, ppm=hw_tcp.get('ppm', 0), **source_kwargs)
        source_label = f'{host}:{port}'

    # Save initial state as "last successful"
    save_preference('hardware_usb', hw_usb)
    save_preference('hardware_tcp', hw_tcp)
    save_preference('hardware_last_mode', last_mode)

    # Create JSON directory if specified
    if args.json_dir:
        os.makedirs(args.json_dir, exist_ok=True)

    # --- Inter-thread queues ---
    iq_queue  = queue.Queue(maxsize=16)
    msg_queue = queue.Queue(maxsize=100)

    # --- Event history (SQLite) ---
    event_store = None
    if not args.no_db:
        try:
            event_store = EventStore(args.db_path)
        except Exception as e:
            log.warning(f'Could not start EventStore: {e}')

    # --- EAS message recorder (optional) ---
    msg_recorder = None
    if args.save_audiomsgs:
        msg_recorder = MessageAudioRecorder(save_dir=args.save_audiomsgs)

    monitor_vol = prefs.get('volume', args.volume)
    
    # --- Audio monitor ---
    # Created whenever the web dashboard is active (required for metrics
    # and audio streaming to the browser), even if local output is not used.
    # enable_playback=True only when --audio is specified.
    monitor = None
    needs_monitor = bool(args.audio or args.record) or (not args.no_web)
    if needs_monitor:
        dev = args.audio_device
        if dev is not None:
            try:
                dev = int(dev)
            except ValueError:
                pass
        monitor = AudioMonitor(
            device          = dev if args.audio else None,
            record_path     = args.record,
            volume          = monitor_vol,
            enable_playback = bool(args.audio),
        )

    # ── Create Display ────────────────────────────────────────────────────────
    #
    # By default, use web dashboard (WebDashboard, requires fastapi+uvicorn).
    # With --no-web, use Rich terminal panel (SasmexDisplay).

    # Shared mutable list between decoder_loop and WebDashboard (remote control)
    # Priority: CLI > Preferences > All (empty)
    initial_filter = args.event
    if initial_filter is None:
        initial_filter = prefs.get('event_filter', [])
    
    event_filter_list = list(initial_filter)

    if args.no_web:
        display = SasmexDisplay(
            freq_mhz      = freq_mhz,
            gain          = gain if gain > 0 else 'auto',
            host          = host,
            port          = port,
            audio_monitor = monitor,
            tuner_agc     = args.tuner_agc,
            rtl_agc       = args.rtl_agc,
        )
    else:
        display = WebDashboard(
            host          = '0.0.0.0',
            port          = args.web_port,
            freq_mhz      = freq_mhz,
            gain          = gain if gain > 0 else 'auto',
            source        = source_label,
            audio_monitor = monitor,
            tuner_agc     = tuner_agc,
            rtl_agc       = rtl_agc,
        )

        dsp = None

        # Store active hardware configuration to prevent redundant restarts
        current_hw_config = {
            'mode':   'USB' if isinstance(source, PyRtlSdrSource) else 'TCP',
            'device': getattr(source, 'device_index', 0),
            'host':   getattr(source, 'host', 'localhost'),
            'port':   getattr(source, 'port', 0),
            'ppm':    getattr(source, 'ppm', 0)
        }

        def on_hardware_change(new_hw_dict):
            nonlocal source, source_label, dsp, current_hw_config
            
            # Normalize new values
            new_mode   = new_hw_dict.get('mode', current_hw_config['mode'])
            new_device = int(new_hw_dict.get('device', 0))
            new_host   = new_hw_dict.get('host', 'localhost')
            new_port   = int(new_hw_dict.get('port', 0))
            new_ppm    = int(new_hw_dict.get('ppm', 0))

            # Check for structural changes (mode, host, port, device)
            hard_change = (
                new_mode != current_hw_config['mode'] or
                new_device != current_hw_config['device'] or
                new_host != current_hw_config['host'] or
                new_port != current_hw_config['port']
            )
            
            # Check if only PPM changed
            ppm_change = (new_ppm != current_hw_config['ppm'])

            if not hard_change and not ppm_change:
                display.log("Configuration is identical; skipping restart.", "info")
                return

            if not hard_change and ppm_change:
                # OPTIMIZATION: PPM change only, no hardware restart needed
                try:
                    source.set_ppm(new_ppm)
                    current_hw_config['ppm'] = new_ppm
                    # Update mode-specific preference
                    pref_key = 'hardware_usb' if new_mode == 'USB' else 'hardware_tcp'
                    save_preference(pref_key, new_hw_dict)
                    display.log(f'PPM correction updated to {new_ppm} (no hardware restart)', 'ok')
                    display._mgr.broadcast('controls', display._build_controls())
                    return
                except Exception as e:
                    display.log(f"Error updating PPM on the fly: {e}", "warn")
            
            # If we reach here, it's a Hard Change. Save mode-specific preference.
            pref_key = 'hardware_usb' if new_mode == 'USB' else 'hardware_tcp'
            save_preference(pref_key, new_hw_dict)
            save_preference('hardware_last_mode', new_mode)
            
            try:
                if source: source.stop()
                if dsp: dsp.stop()
                import time
                time.sleep(1.0)
                
                phys_freq = int(getattr(display, 'freq_mhz', 162.4) * 1e6)

                if new_mode == 'USB':
                    source = PyRtlSdrSource(device_index=new_device, freq=phys_freq, sample_rate=sample_rate,
                                            gain=source.gain, ppm=new_ppm, tuner_agc=source.tuner_agc, rtl_agc=source.rtl_agc)
                    source_label = f'USB:{new_device}'
                else:
                    source = RtlTcpSource(host=new_host, port=new_port, freq=phys_freq, sample_rate=sample_rate,
                                          gain=source.gain, ppm=new_ppm, tuner_agc=source.tuner_agc, rtl_agc=source.rtl_agc)
                    source_label = f'{new_host}:{new_port}'

                current_hw_config = {
                    'mode':   new_mode,
                    'device': new_device,
                    'host':   new_host,
                    'port':   new_port,
                    'ppm':    new_ppm
                }

                dsp = DSPWorker(iq_queue, msg_queue, sample_rate, display,
                                audio_monitor=monitor, msg_recorder=msg_recorder)

                source.start(iq_queue)
                dsp.start()
                display.source = source_label
                # Update controls with new settings loaded from disk
                updated_prefs = load_preferences()
                display.set_controls(source=source, event_filter=event_filter_list, event_store=event_store,
                                     on_hw_change=on_hardware_change, save_pref=save_preference,
                                     hw_usb=updated_prefs.get('hardware_usb'),
                                     hw_tcp=updated_prefs.get('hardware_tcp'),
                                     dispatcher=dispatcher,
                                     integrations=updated_prefs.get('integrations', {}))
                
                display._mgr.broadcast('status', display._build_status())
                display._mgr.broadcast('controls', display._build_controls())
                display.log(f'Hardware reconfigured → {source_label} (PPM={new_ppm})', 'ok')
            except Exception as e:
                display.log(f"Error hot-swapping hardware: {e}", "error")

        # Connect remote control: IQ source + event filter + history
        display.set_controls(source=source, event_filter=event_filter_list,
                             event_store=event_store, on_hw_change=on_hardware_change, 
                             save_pref=save_preference,
                             hw_usb=prefs.get('hardware_usb'),
                             hw_tcp=prefs.get('hardware_tcp'),
                             dispatcher=dispatcher,
                             integrations=prefs.get('integrations', {}))
        # Connect PCM audio streaming → WebSocket /ws/audio
        if monitor:
            monitor.set_audio_stream_callback(display._on_audio_frame)

    if dsp is None:
        dsp = DSPWorker(iq_queue, msg_queue, sample_rate, display,
                        audio_monitor=monitor,
                        msg_recorder=msg_recorder)

    # --- Start ---
    with display:
        if args.device is not None:
            display.log(f'Local USB RTL-SDR — device [{args.device}] (pyrtlsdr / librtlsdr)', 'ok')
        else:
            display.log(f'Connecting to RTL-TCP {source_label}…')

        try:
            source.start(iq_queue)
        except ImportError as e:
            display.log(str(e), 'error')
            return
        except Exception as e:
            display.log(f'Error opening IQ source: {e}', 'error')
            return

        gain_str    = 'auto (Tuner-AGC)' if (tuner_agc or gain == 0) else f'{gain} dB'
        rtl_agc_str = '  RTL-AGC=ON' if rtl_agc else ''
        display.log(
            f'Tuned: {freq_mhz:.3f} MHz  |  '
            f'SR: {sample_rate/1e6:.3f} MSps ({sample_rate:,} Hz)  |  '
            f'Gain: {gain_str}{rtl_agc_str}',
            'ok'
        )

        if monitor:
            monitor.start()
            audio_dev = args.audio_device or 'default'
            if args.audio or not args.no_web:
                display.log(f'FM Audio → device [{audio_dev}]  '
                            f'vol={monitor_vol:.2f}', 'ok')
            if args.record:
                display.log(f'Recording continuous FM audio → {args.record}', 'ok')

        if msg_recorder:
            display.log(
                f'EAS message recording → {args.save_audiomsgs}  '
                f'(pre=15s + post=90s per event)', 'ok'
            )

        if event_store:
            display.log(
                f'History: {args.db_path}  '
                f'({event_store.count()} events stored)', 'ok'
            )

        display.log('Listening for EAS/SASMEX messages… (Ctrl+C to exit)')
        dsp.start()

        try:
            decoder_loop(msg_queue, display,
                         event_filter=event_filter_list,
                         same_filter=args.same  or [],
                         json_dir=args.json_dir,
                         msg_recorder=msg_recorder,
                         event_store=event_store,
                         dispatcher=dispatcher)
        except KeyboardInterrupt:
            display.log('Stopping…', 'warn')
        finally:
            source.stop()
            dsp.stop()
            if monitor:
                monitor.stop()
            if msg_recorder:
                msg_recorder.stop()
            if dispatcher:
                dispatcher.stop()


if __name__ == '__main__':
    main()
