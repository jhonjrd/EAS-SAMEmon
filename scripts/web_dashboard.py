"""
web_dashboard.py — Real-time Web Dashboard for EAS-SAMEmon

Replaces the terminal Rich display with a web server accessible
from any browser on the local network.

Technology:
  - FastAPI + uvicorn  →  HTTP / WebSocket server
  - WebSocket push     →  Real-time events (no polling)
  - static/index.html  →  UI with no external dependencies

WebSocket Control Protocol (JSON, bidirectional):

  Server → client:
    {"type": "hello",    "data": {status, messages[], logs[], alert, controls}}
    {"type": "status",   "data": {freq_mhz, gain, source, uptime, chunks}}
    {"type": "metrics",  "data": {level_dbfs, snr_db, deviation, squelch_open}}
    {"type": "message",  "data": {EEE, event, COUNTRY, organization, ...}}
    {"type": "log",      "data": {text, level, time}}
    {"type": "alert",    "data": {active: bool, message: dict|null}}

  Client → server (remote control):
    {"type": "set_freq",           "value": 162.450}       ← MHz
    {"type": "set_gain",           "value": 35.0}          ← dB, 0 = Tuner AGC
    {"type": "set_tuner_agc",      "value": true}
    {"type": "set_volume",         "value": 0.8}           ← 0.0–1.0
    {"type": "set_squelch",        "value": -40.0}         ← dBFS
    {"type": "set_squelch_enabled","value": true}
    {"type": "set_event_filter",   "value": ["EQW","RWT"]} ← [] = no filter

Audio Streaming:
  Binary WebSocket at /ws/audio → PCM float32 LE frames, mono 25000 Hz.
  Sent only when squelch is open.
  The browser applies its own volume control over the stream.

Usage:
  dash = WebDashboard(host='0.0.0.0', port=8080, freq_mhz=162.450, ...)
  dash.set_controls(source=source, event_filter=event_filter_list)
  with dash:
      dash.log('Connecting...')
      dash.add_message(result_dict)
      dash.tick()
"""

import asyncio
import threading
import datetime
import logging
import json
import os
import requests
from collections import deque

log = logging.getLogger(__name__)

MAX_MESSAGES     = 50
MAX_LOGS         = 100
METRICS_INTERVAL = 1.0   # seconds between metrics push


# ---------------------------------------------------------------------------
# WebSocket Connection Manager (JSON control channel)
# ---------------------------------------------------------------------------

class _ConnectionManager:
    """
    Manages control channel WebSocket clients.
    Thread-safe: broadcast() is called from pipeline threads.
    """

    def __init__(self):
        self._clients: list = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def connect(self, ws) -> None:
        with self._lock:
            self._clients.append(ws)
        log.debug(f'WS control connected ({len(self._clients)} total)')

    async def disconnect(self, ws) -> None:
        with self._lock:
            if ws in self._clients:
                self._clients.remove(ws)
        log.debug(f'WS control disconnected ({len(self._clients)} remaining)')

    async def _send_all(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    def broadcast(self, event_type: str, data: dict) -> None:
        """Send JSON event to all clients. Thread-safe."""
        if not self._loop:
            # If server not started, buffer logs for later
            if event_type == 'log':
                log.debug("Buffering log for dashboard startup")
            return
        if not self._clients:
            return
        payload = json.dumps({'type': event_type, 'data': data}, default=str)
        asyncio.run_coroutine_threadsafe(self._send_all(payload), self._loop)

    async def send_one(self, ws, event_type: str, data: dict) -> None:
        payload = json.dumps({'type': event_type, 'data': data}, default=str)
        await ws.send_text(payload)


# ---------------------------------------------------------------------------
# WebDashboard — interface compatible with SasmexDisplay
# ---------------------------------------------------------------------------

class WebDashboard:
    """
    Real-time web dashboard.

    Parameters
    ----------
    host : str
        Listening address (default '0.0.0.0').
    port : int
        HTTP port (default 8080).
    freq_mhz : float
        Tuned frequency.
    gain : float | str
        Gain or 'auto'.
    source : str
        Source label ('USB:0', 'host:port', etc.).
    audio_monitor : AudioMonitor | None
        Audio monitor instance (metrics + streaming).
    tuner_agc : bool
    rtl_agc : bool
    """

    def __init__(self,
                 host:          str   = '0.0.0.0',
                 port:          int   = 8080,
                 freq_mhz:      float = 162.450,
                 gain                 = 40.0,
                 source:        str   = '',
                 audio_monitor        = None,
                 tuner_agc:     bool  = False,
                 rtl_agc:       bool  = False):

        self.host         = host
        self.port         = port
        self.freq_mhz     = freq_mhz
        self.gain         = gain
        self.source       = source
        self.tuner_agc    = tuner_agc
        self.rtl_agc      = rtl_agc
        self._monitor     = audio_monitor
        self._start_time  = datetime.datetime.now()
        self._chunks      = 0

        self._messages: deque[dict] = deque(maxlen=MAX_MESSAGES)
        self._logs:     deque[dict] = deque(maxlen=MAX_LOGS)
        self._active_alert: dict | None = None

        self._mgr   = _ConnectionManager()
        self._app   = None
        self._server_thread: threading.Thread | None = None
        self._server_loop:   asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

        # Remote control
        self._source       = None   # RtlTcpSource | PyRtlSdrSource
        self._event_filter: list = []
        self._store        = None   # EventStore | None
        self._lang         = 'EN'   # UI language preference (EN | ES)

        # Binary audio channel
        self._audio_clients: list = []
        self._audio_lock = threading.Lock()
        
        # Hardware memories
        self._hw_usb = None
        self._hw_tcp = None

        # External Integrations (Home Assistant)
        self._dispatcher = None
        self._integrations = {}

    # ------------------------------------------------------------------
    # Remote control — connect source and event filter
    # ------------------------------------------------------------------

    def set_controls(self, source, event_filter: list,
                     event_store=None, on_hw_change=None, save_pref=None,
                     hw_usb=None, hw_tcp=None, 
                     dispatcher=None, integrations: dict = {}) -> None:
        """
        Register references for remote control and history.

        Parameters
        ----------
        source : RtlTcpSource | PyRtlSdrSource
            Active IQ source (set_freq / set_gain / set_tuner_agc).
        event_filter : list
            Mutable list shared with decoder_loop. The dashboard modifies it
            on-the-fly when the user changes the filter from the browser.
        event_store : EventStore | None
            SQLite database for the history API (/api/events).
        hw_usb/hw_tcp : dict | None
            Persistent configuration memories for each mode.
        dispatcher : AlertDispatcher | None
            Global dispatcher for Home Assistant integrations.
        integrations : dict
            Initial configuration for Webhooks.
        """
        self._source       = source
        self._event_filter = event_filter
        self._store        = event_store
        self._on_hw_change = on_hw_change
        self._save_pref    = save_pref
        self._hw_usb       = hw_usb
        self._hw_tcp       = hw_tcp
        self._dispatcher   = dispatcher
        self._integrations = integrations or {}

    # ------------------------------------------------------------------
    # PCM Audio Streaming (callback from AudioMonitor)
    # ------------------------------------------------------------------

    def _on_audio_frame(self, data: bytes) -> None:
        """
        Receives a PCM float32 frame from AudioMonitor and forwards it
        to all connected audio clients. Thread-safe.
        """
        if not self._server_loop:
            return
        with self._audio_lock:
            if not self._audio_clients:
                return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_audio(data), self._server_loop
        )

    async def _broadcast_audio(self, data: bytes) -> None:
        with self._audio_lock:
            clients = list(self._audio_clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            with self._audio_lock:
                if ws in self._audio_clients:
                    self._audio_clients.remove(ws)
        if dead:
            log.debug(f'WS audio clients cleaned ({len(self._audio_clients)} remaining)')

    # ------------------------------------------------------------------
    # Lifecycle (context manager)
    # ------------------------------------------------------------------

    def __enter__(self):
        self._build_app()
        self._server_thread = threading.Thread(
            target=self._run_server, name='web-dashboard', daemon=True)
        self._server_thread.start()
        self._ready.wait(timeout=5)
        log.info(f'Web dashboard at http://{self.host}:{self.port}')
        print(f'\n  Dashboard: http://localhost:{self.port}\n')
        return self

    def __exit__(self, *_):
        if self._server_loop and self._server_loop.is_running():
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)

    # ------------------------------------------------------------------
    # Public API (same interface as SasmexDisplay)
    # ------------------------------------------------------------------

    def log(self, text: str, level: str = 'info') -> None:
        entry = {
            'text':  text,
            'level': level,
            'time':  datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        }
        self._logs.append(entry)
        self._mgr.broadcast('log', entry)

    def add_message(self, msg: dict) -> None:
        enriched = dict(msg)
        enriched['received_at'] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        self._messages.appendleft(enriched)

        eee = msg.get('EEE', '')
        if eee not in ('RWT', 'RMT', 'NPT', 'DMO', 'ADR'):
            self._active_alert = enriched
            self._mgr.broadcast('alert', {'active': True, 'message': enriched})

        self._mgr.broadcast('message', enriched)

    def tick(self) -> None:
        self._chunks += 1
        if self._chunks % 60 == 0:
            self._check_alert()
            self._mgr.broadcast('status', self._build_status())

    def _check_alert(self) -> None:
        if not self._active_alert:
            return
        end_str = self._active_alert.get('end_dt')
        if not end_str:
            return
        try:
            end_time = datetime.datetime.fromisoformat(end_str)
        except ValueError:
            return
            
        if datetime.datetime.now(datetime.timezone.utc) > end_time:
            self.clear_alert()

    def clear_alert(self) -> None:
        self._active_alert = None
        self._mgr.broadcast('alert', {'active': False, 'message': None})

    # ------------------------------------------------------------------
    # FastAPI App Construction
    # ------------------------------------------------------------------

    def _build_app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles

        app = FastAPI(title='EAS-SAMEmon dashboard', docs_url=None, redoc_url=None)

        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
        if os.path.isdir(static_dir):
            app.mount('/static', StaticFiles(directory=static_dir), name='static')

        @app.get('/', response_class=HTMLResponse)
        async def index():
            html_path = os.path.join(static_dir, 'index.html')
            with open(html_path, encoding='utf-8') as f:
                return HTMLResponse(f.read())

        @app.get('/health')
        async def health():
            return {'status': 'ok', 'chunks': self._chunks,
                    'db': bool(self._store)}

        # ── History API ──────────────────────────────────────────────────
        @app.get('/api/events')
        async def api_events(
            from_date: str | None = None,
            to_date:   str | None = None,
        ):
            """
            Returns EAS events from history in a date range.
            Query parameters: from (YYYY-MM-DD), to (YYYY-MM-DD).
            """
            if not self._store:
                return JSONResponse({'error': 'History not available (--no-db)'}, 503)

            try:
                if from_date:
                    from_dt = datetime.datetime.fromisoformat(from_date)
                else:
                    from_dt = datetime.datetime.now() - datetime.timedelta(days=30)

                if to_date:
                    # Include full "to" day
                    to_dt = datetime.datetime.fromisoformat(to_date).replace(
                        hour=23, minute=59, second=59)
                else:
                    to_dt = datetime.datetime.now()
            except ValueError:
                return JSONResponse({'error': 'Invalid date format. Use YYYY-MM-DD'}, 400)

            events = self._store.query(from_dt, to_dt)
            return JSONResponse(events)

        @app.get('/api/events/count')
        async def api_events_count():
            if not self._store:
                return {'count': 0, 'available': False}
            return {'count': self._store.count(), 'available': True}

        @app.delete('/api/events')
        async def api_events_delete_many(body: dict):
            """Bulk-delete events by timestamp list: {"timestamps": [...]}"""
            if not self._store:
                return JSONResponse({'error': 'History not available'}, 503)
            timestamps = body.get('timestamps') or []
            if not timestamps:
                return JSONResponse({'error': 'No timestamps provided'}, 400)

            deleted_db = self._store.delete_many(timestamps)

            ts_set = set(timestamps)
            original_len = len(self._messages)
            self._messages = deque(
                [m for m in self._messages if m.get('received_at') not in ts_set],
                maxlen=MAX_MESSAGES,
            )
            deleted_mem = original_len - len(self._messages)

            for ts in timestamps:
                self._mgr.broadcast('delete_event', {'received_at': ts})

            return {'status': 'ok', 'deleted_db': deleted_db, 'deleted_mem': deleted_mem}

        @app.delete('/api/events/{received_at}')
        async def api_events_delete(received_at: str):
            if not self._store:
                return JSONResponse({'error': 'History not available'}, 503)
            
            # Delete from database
            deleted_db = self._store.delete_by_timestamp(received_at)
            
            # Delete from local memory
            original_len = len(self._messages)
            new_messages = deque([m for m in self._messages if m.get('received_at') != received_at], maxlen=MAX_MESSAGES)
            self._messages = new_messages
            deleted_mem = len(self._messages) < original_len
            
            # Notify websocket
            if deleted_db or deleted_mem:
                self._mgr.broadcast('delete_event', {'received_at': received_at})
                return {'status': 'ok', 'deleted_db': deleted_db, 'deleted_mem': deleted_mem}
            
            return JSONResponse({'error': 'Event not found'}, 404)

        @app.post('/api/hardware')
        async def post_hardware(new_hw: dict):
            """Endpoint for robust hardware configuration persistence."""
            try:
                if getattr(self, '_on_hw_change', None):
                    # Fire the blocking swap (stop/sleep/start) in a thread without
                    # awaiting it — returning immediately avoids the TCP connection
                    # being dropped by the OS/browser while the swap takes 1-2 s.
                    # The client is notified of completion via the WebSocket
                    # 'controls' broadcast that on_hardware_change sends when done.
                    asyncio.get_running_loop().run_in_executor(
                        None, self._on_hw_change, new_hw
                    )
                    return {'status': 'ok', 'accepted': True}
                return JSONResponse({'error': 'Hardware control not initialized'}, 500)
            except Exception as e:
                log.error(f'post_hardware error: {e}', exc_info=True)
                return JSONResponse({'error': str(e)}, 400)

        # ── Control Channel (bidirectional JSON) ─────────────────────────
        @app.websocket('/ws')
        async def ws_control(ws: WebSocket):
            await ws.accept()
            await self._mgr.connect(ws)
            # Prune expired messages from the in-memory deque before sending.
            # end_dt is an ISO-format UTC string; messages without it never expire.
            _now = datetime.datetime.now(datetime.timezone.utc)
            def _still_active(m):
                end = m.get('end_dt')
                if not end:
                    return True
                try:
                    return datetime.datetime.fromisoformat(end) > _now
                except Exception:
                    return True
            self._messages = deque(
                (m for m in self._messages if _still_active(m)),
                maxlen=MAX_MESSAGES,
            )
            # Full state upon connection
            await self._mgr.send_one(ws, 'hello', {
                'status':   self._build_status(),
                'messages': list(self._messages),
                'logs':     list(self._logs),
                'alert':    {'active': self._active_alert is not None,
                             'message': self._active_alert},
                'controls': self._build_controls(),
            })
            try:
                while True:
                    text = await ws.receive_text()
                    try:
                        msg = json.loads(text)
                        await self._handle_control(msg)
                    except Exception as exc:
                        log.debug(f'Invalid control msg: {exc}')
            except WebSocketDisconnect:
                await self._mgr.disconnect(ws)
            except Exception:
                await self._mgr.disconnect(ws)

        # ── Audio Channel (PCM float32 binary) ───────────────────────────
        @app.websocket('/ws/audio')
        async def ws_audio(ws: WebSocket):
            await ws.accept()
            with self._audio_lock:
                self._audio_clients.append(ws)
            log.debug(f'WS audio connected ({len(self._audio_clients)} total)')
            try:
                while True:
                    await ws.receive_text()   # detect disconnection only
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                with self._audio_lock:
                    if ws in self._audio_clients:
                        self._audio_clients.remove(ws)
                log.debug(f'WS audio disconnected ({len(self._audio_clients)} remaining)')

        self._app = app

    # ------------------------------------------------------------------
    # Incoming Control Message Handler
    # ------------------------------------------------------------------

    async def _handle_control(self, msg: dict) -> None:
        t = msg.get('type')
        v = msg.get('value')

        if t == 'set_freq' and self._source is not None:
            try:
                mhz = float(v)
                self.freq_mhz = mhz
                self._source.set_freq(int(mhz * 1_000_000))
                if getattr(self, '_save_pref', None): self._save_pref('freq_mhz', mhz)
                self._mgr.broadcast('status', self._build_status())
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_freq: {e}')

        elif t == 'set_gain' and self._source is not None:
            try:
                db = float(v)
                self.gain = db
                self.tuner_agc = False # Explicitly switch to manual mode on backend state
                
                self._source.set_gain(db)
                if getattr(self, '_save_pref', None):
                    self._save_pref('gain', db)
                    self._save_pref('tuner_agc', False)
                
                self._mgr.broadcast('status', self._build_status())
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_gain: {e}')

        elif t == 'set_tuner_agc' and self._source is not None:
            try:
                enabled = bool(v)
                self.tuner_agc = enabled
                if enabled:
                    self.gain = self.gain # Keep last manual gain but mark as auto
                
                self._source.set_tuner_agc(enabled)
                if getattr(self, '_save_pref', None):
                    self._save_pref('tuner_agc', enabled)
                
                self._mgr.broadcast('status', self._build_status())
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_tuner_agc: {e}')

        elif t == 'set_volume' and self._monitor is not None:
            try:
                vol = max(0.0, min(1.0, float(v)))
                self._monitor.volume = vol
                if getattr(self, '_save_pref', None): self._save_pref('volume', vol)
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_volume: {e}')

        elif t == 'set_event_filter':
            try:
                new_filter = [str(x) for x in v] if v else []
                self._event_filter.clear()
                self._event_filter.extend(new_filter)
                log.info(f'Event filter → {new_filter or "all"}')
                if getattr(self, '_save_pref', None): self._save_pref('event_filter', new_filter)
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_event_filter: {e}')

        elif t == 'set_ppm' and self._source is not None:
            try:
                ppm = int(v)
                if hasattr(self._source, 'set_ppm'):
                    self._source.set_ppm(ppm)
                if getattr(self, '_save_pref', None): self._save_pref('ppm', ppm)
                self._mgr.broadcast('controls', self._build_controls())
            except Exception as e:
                log.warning(f'set_ppm: {e}')

        elif t == 'set_lang':
            # Language preference is managed by the frontend (localStorage).
            # Save it server-side for future use (e.g. generated MESSAGE field).
            try:
                lang = str(v).upper()
                if lang in ('EN', 'ES'):
                    self._lang = lang
                    if getattr(self, '_save_pref', None): self._save_pref('lang', lang)
                    log.debug(f'Language set to {lang}')
            except Exception as e:
                log.warning(f'set_lang: {e}')

        elif t == 'set_hardware':
            try:
                if getattr(self, '_on_hw_change', None):
                    self._on_hw_change(v)
            except Exception as e:
                log.warning(f'set_hardware: {e}')

        elif t == 'set_integrations':
            try:
                self._integrations = v or {}
                if self._dispatcher:
                    self._dispatcher.reconfig(self._integrations)

                if getattr(self, '_save_pref', None):
                    self._save_pref('integrations', self._integrations)

                self._mgr.broadcast('controls', self._build_controls())
                log.info("Home Assistant integrations updated.")
            except Exception as e:
                log.warning(f'set_integrations: {e}')

        elif t == 'test_webhook':
            url = ((v or {}).get('url') or '').strip()
            if not url:
                self._mgr.broadcast('webhook_test_result', {
                    'ok': False, 'status_code': None, 'error': 'No URL provided'
                })
            else:
                async def _run_test(test_url):
                    try:
                        loop = asyncio.get_event_loop()
                        resp = await loop.run_in_executor(
                            None,
                            lambda: requests.post(
                                test_url,
                                json={
                                    'test': True,
                                    'source': 'EAS-SAMEmon',
                                    'message': 'Test webhook from EAS-SAMEmon',
                                },
                                timeout=10,
                            ),
                        )
                        ok = resp.status_code < 400
                        self._mgr.broadcast('webhook_test_result', {
                            'ok':          ok,
                            'status_code': resp.status_code,
                            'error':       None,
                        })
                        log.info(f"Webhook test → HTTP {resp.status_code}: {test_url}")
                    except requests.exceptions.Timeout:
                        self._mgr.broadcast('webhook_test_result', {
                            'ok': False, 'status_code': None, 'error': 'Connection timed out'
                        })
                        log.warning(f"Webhook test timed out: {test_url}")
                    except requests.exceptions.ConnectionError:
                        self._mgr.broadcast('webhook_test_result', {
                            'ok': False, 'status_code': None, 'error': 'Connection refused'
                        })
                        log.warning(f"Webhook test connection refused: {test_url}")
                    except requests.exceptions.InvalidURL:
                        self._mgr.broadcast('webhook_test_result', {
                            'ok': False, 'status_code': None, 'error': 'Invalid URL'
                        })
                        log.warning(f"Webhook test invalid URL: {test_url}")
                    except Exception as exc:
                        self._mgr.broadcast('webhook_test_result', {
                            'ok': False, 'status_code': None, 'error': 'Connection failed'
                        })
                        log.warning(f"Webhook test error: {exc}")

                asyncio.ensure_future(_run_test(url))

    # ------------------------------------------------------------------
    # Server in separate thread
    # ------------------------------------------------------------------

    def _run_server(self):
        import uvicorn

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._server_loop = loop
        self._mgr.set_loop(loop)

        loop.create_task(self._metrics_loop())

        config = uvicorn.Config(
            app        = self._app,
            host       = self.host,
            port       = self.port,
            loop       = 'none',
            log_level  = 'warning',
            access_log = False,
        )
        server = uvicorn.Server(config)

        async def _serve():
            await server.serve()

        self._ready.set()
        loop.run_until_complete(_serve())

    # ------------------------------------------------------------------
    # Metrics Task (asyncio)
    # ------------------------------------------------------------------

    async def _metrics_loop(self):
        """Push signal metrics every second while there are clients."""
        while True:
            await asyncio.sleep(METRICS_INTERVAL)
            if self._mgr._clients and self._monitor:
                metrics = self._monitor.get_metrics()
                await self._mgr._send_all(
                    json.dumps({'type': 'metrics', 'data': metrics})
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_status(self) -> dict:
        uptime = str(datetime.datetime.now() - self._start_time).split('.')[0]
        if self.tuner_agc:
            gain_label = 'Tuner-AGC'
        else:
            try:
                gain_label = f'Gain: {float(self.gain):.1f} dB'
            except (ValueError, TypeError):
                gain_label = f'Gain: {self.gain} dB'
        if self.rtl_agc:
            gain_label += ' +RTL-AGC'
        return {
            'freq_mhz': self.freq_mhz,
            'gain':     gain_label,
            'source':   self.source,
            'uptime':   uptime,
            'chunks':   self._chunks,
            'version':  '1.0.0',
        }

    def _build_controls(self) -> dict:
        """Current control state — sent to client upon connection."""
        gain_val = 0.0 if (self.tuner_agc or self.gain in (0, 'auto')) else float(self.gain)
        
        ppm_val = getattr(self._source, 'ppm', 0) if self._source else 0
        mode = "USB" if self._source.__class__.__name__ == "PyRtlSdrSource" else "TCP"
        host = getattr(self._source, 'host', '127.0.0.1')
        port = getattr(self._source, 'port', 1234)
        device = getattr(self._source, 'device_index', 0)
        
        return {
            'freq_mhz':        self.freq_mhz,
            'gain':            gain_val,
            'tuner_agc':       self.tuner_agc,
            'volume':          self._monitor.volume       if self._monitor else 0.8,
            'event_filter':    list(self._event_filter),
            'ppm':             ppm_val,
            'hardware':        {'mode': mode, 'host': host, 'port': port, 'device': device},
            'hw_usb':          self._hw_usb,
            'hw_tcp':          self._hw_tcp,
            'integrations':    self._integrations
        }
