"""
display.py — Real-time Rich console for EAS-SAMEmon

Displays:
  - Receiver status (frequency, gain, uptime)
  - Received messages table (timestamp, type, areas, transmitter)
  - Active alert panel (flashing red on EQW)
  - Activity log
"""

import datetime
from collections import deque
from rich.console   import Console
from rich.layout    import Layout
from rich.live      import Live
from rich.panel     import Panel
from rich.table     import Table
from rich.text      import Text
from rich.align     import Align
from rich           import box

MAX_LOG_LINES = 12
MAX_MESSAGES  = 50


class SasmexDisplay:
    """
    Manages the real-time Rich screen.

    Usage:
        disp = SasmexDisplay(freq_mhz=162.450, gain=40)
        with disp:
            disp.log('System started')
            disp.add_message(msg_dict)
    """

    def __init__(self, freq_mhz: float = 162.450, gain = 40.0,
                 host: str = 'localhost', port: int = 1234,
                 audio_monitor=None,
                 tuner_agc: bool = False,
                 rtl_agc: bool = False):
        self.freq_mhz  = freq_mhz
        self.gain      = gain
        self.host      = host
        self.port      = port
        self.tuner_agc = tuner_agc
        self.rtl_agc   = rtl_agc
        self.start_time = datetime.datetime.now()
        self._monitor   = audio_monitor   # AudioMonitor instance or None

        self._console   = Console()
        self._messages  = deque(maxlen=MAX_MESSAGES)
        self._log_lines = deque(maxlen=MAX_LOG_LINES)
        self._alert     = None
        self._chunks    = 0
        self._live      = None
        self._layout    = None   # direct reference to Layout

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self._layout = self._build_layout()
        self._live = Live(self._layout, console=self._console,
                          refresh_per_second=4, screen=True)
        self._live.__enter__()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.__exit__(*args)

    # ------------------------------------------------------------------
    # Public API (thread-safe via CPython GIL)
    # ------------------------------------------------------------------

    def log(self, text: str, level: str = 'info'):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        color = {'info': 'cyan', 'warn': 'yellow', 'error': 'red',
                 'ok': 'green'}.get(level, 'white')
        self._log_lines.append(f'[{color}]{ts}[/] {text}')
        self._refresh()

    def add_message(self, msg: dict):
        """
        Receive a decoded message.

        msg must contain at least:
            EEE, ORG, COUNTRY, event, organization, start, end,
            PSSCCC_list, LLLLLLLL, transmitter (dict), MESSAGE
        """
        msg['_received'] = datetime.datetime.now()
        self._messages.appendleft(msg)

        eee = msg.get('EEE', '')
        if eee == 'EQW':
            self._alert = msg
            self.log(f'[bold red]⚠ SEISMIC ALERT DETECTED[/]', 'error')
        else:
            self.log(f"Message {eee} from {msg.get('organization','?')}", 'ok')

        self._refresh()

    def tick(self):
        """Call for each IQ chunk processed to update uptime."""
        self._chunks += 1
        if self._chunks % 50 == 0:   # update display every ~3 s @ 250kHz
            self._check_alert()
            self._refresh()

    def _check_alert(self):
        if not self._alert:
            return
        end_str = self._alert.get('end_dt')
        if not end_str:
            return
        try:
            end_time = datetime.datetime.fromisoformat(end_str)
        except ValueError:
            return
        
        if datetime.datetime.now() > end_time:
            self.clear_alert()

    def clear_alert(self):
        self._alert = None
        self._refresh()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name='header', size=3),
            Layout(name='body'),
            Layout(name='signal', size=7),   # FM signal panel
            Layout(name='footer', size=MAX_LOG_LINES + 2),
        )
        layout['body'].split_row(
            Layout(name='messages', ratio=3),
            Layout(name='alert',    ratio=2),
        )
        return layout

    def _refresh(self):
        if not self._live or not self._layout:
            return
        self._layout['header'].update(self._render_header())
        self._layout['messages'].update(self._render_messages())
        self._layout['alert'].update(self._render_alert())
        self._layout['signal'].update(self._render_signal())
        self._layout['footer'].update(self._render_log())
        self._live.refresh()

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    def _render_header(self) -> Panel:
        uptime  = datetime.datetime.now() - self.start_time
        up_str  = str(uptime).split('.')[0]

        # Gain / AGC label
        if self.tuner_agc or self.gain == 0 or self.gain == 'auto':
            gain_label = '[magenta]Tuner-AGC[/]'
        else:
            gain_label = f'[magenta]{self.gain} dB[/]'
        if self.rtl_agc:
            gain_label += ' [cyan]+RTL-AGC[/]'

        # Source label: "USB:0" or "host:port"
        src_label = (self.host if str(self.host).startswith('USB')
                     else f'{self.host}:{self.port}')

        content = (
            f'[bold cyan]EAS-SAMEmon[/]  •  '
            f'[yellow]{self.freq_mhz:.3f} MHz[/]  •  '
            f'[green]{src_label}[/]  •  '
            f'Gain {gain_label}  •  '
            f'Uptime [white]{up_str}[/]  •  '
            f'Chunks [dim]{self._chunks:,}[/]'
        )
        return Panel(Align.center(content), style='bold', box=box.HORIZONTALS)

    def _render_messages(self) -> Panel:
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False,
                    header_style='bold cyan')
        tbl.add_column('Time',        style='dim',        width=8)
        tbl.add_column('Type',        style='bold',       width=5)
        tbl.add_column('Event',                           width=22)
        tbl.add_column('Areas',                           ratio=1)
        tbl.add_column('Transmitter', style='magenta',    width=14)

        for msg in list(self._messages)[:20]:
            eee     = msg.get('EEE', '?')
            ts      = msg.get('_received', datetime.datetime.now()).strftime('%H:%M:%S')
            event_text  = msg.get('event', eee)[:22]
            areas   = ', '.join(msg.get('PSSCCC_list', []))[:40]
            tx_info = msg.get('transmitter', {})
            tx_name = tx_info.get('name', msg.get('LLLLLLLL', '?'))

            color = 'red bold' if eee == 'EQW' else ('yellow' if eee == 'RWT' else 'white')
            tbl.add_row(ts,
                        Text(eee, style=color),
                        event_text, areas, tx_name)

        return Panel(tbl, title='[bold]Received messages[/]',
                     border_style='cyan', box=box.ROUNDED)

    def _render_alert(self) -> Panel:
        if not self._alert:
            content = Align.center(
                Text('No active alerts\n\nListening...', style='dim'),
                vertical='middle'
            )
            return Panel(content, title='Alert Status',
                         border_style='green', box=box.ROUNDED)

        msg     = self._alert
        event_text  = msg.get('event', msg.get('EEE', ''))
        end_str = msg.get('end', '?')
        org     = msg.get('organization', '?')
        areas   = '\n'.join(msg.get('PSSCCC_list', []))
        tx      = msg.get('transmitter', {})
        freq    = tx.get('freq_mhz', '')
        tx_str  = f"{tx.get('name','?')}  {freq} MHz" if freq else tx.get('name', '?')

        content = Text()
        content.append('⚠  SEISMIC ALERT  ⚠\n\n', style='bold red blink')
        content.append(f'{event_text}\n\n', style='bold yellow')
        content.append(f'Source:  ', style='dim')
        content.append(f'{org}\n',    style='white')
        content.append(f'Valid until: ', style='dim')
        content.append(f'{end_str}\n', style='cyan')
        content.append(f'Transmitter: ',  style='dim')
        content.append(f'{tx_str}\n\n', style='magenta')
        content.append('Areas:\n', style='dim')
        content.append(areas, style='yellow')

        return Panel(Align.center(content, vertical='middle'),
                     title='[bold red]⚠ ACTIVE ALERT ⚠[/]',
                     border_style='red', box=box.HEAVY)

    def _render_signal(self) -> Panel:
        """FM signal quality panel — always visible."""
        from audio_monitor import AudioMonitor

        if self._monitor is None:
            # No monitor: show hint
            content = Text('Audio not enabled. Use --audio to monitor FM signal.',
                           style='dim')
            return Panel(content, title='[dim]FM Signal[/]',
                         border_style='dim', box=box.ROUNDED)

        m = self._monitor.get_metrics()
        level   = m['level_dbfs']
        snr     = m['snr_db']
        dev     = m['deviation']

        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column('', style='dim',  width=14)
        table.add_column('', no_wrap=True, ratio=1)

        table.add_row('RMS Level',   Text.from_markup(AudioMonitor.level_bar(level, 30)))
        table.add_row('Est. SNR',    Text.from_markup(AudioMonitor.snr_bar(snr,   30)))
        table.add_row('Deviation',  f'{dev:.3f}  ({"OK" if dev > 0.02 else "low — check gain"})')

        return Panel(table, title='[bold]FM Signal[/]',
                     border_style='green', box=box.ROUNDED)

    def _render_log(self) -> Panel:
        lines = list(self._log_lines)
        text  = Text()
        for line in lines:
            text.append_text(Text.from_markup(line + '\n'))
        return Panel(text, title='[dim]Activity[/]',
                     border_style='dim', box=box.ROUNDED)
