# EAS-SAMEmon — North American Alert Monitor
# Support for SASMEX/SARMEX (Mexico), EAS (USA), and Public Alert (Canada)
#
# Based on dsame by Joseph W. Metcalf (https://github.com/cuppa-joe/dsame)
# Mexican adaptations based on official CIRES documentation (Nov. 2025).
#
# Original License: ISC License
# Modifications: Public Domain / same ISC license
#

import sys
import argparse
import string
import logging
import datetime
import subprocess
import calendar

import mx_defs
import us_defs
import ca_defs

# ---------------------------------------------------------------------------
# EAS / SAME Event Codes (Standard EEE)
# Official sources:
#   - Canada: Environment Canada (Retrieved 2026-03-16)
#   - USA: NOAA/NWS (Retrieved 2026-04-12)
# ---------------------------------------------------------------------------
SAME__ORG = {
    'EAS': {'NAME': {'US': 'Emergency Alert System', 'CA': 'Emergency Alert System', 'MX': 'Emergency Alert System'},     'ARTICLE': {'US': 'THE', 'CA': 'THE', 'MX': 'THE'}, 'PLURAL': False},
    'WXR': {'NAME': {'US': 'National Weather Service', 'CA': 'Environment Canada',     'MX': 'National Weather Service'}, 'ARTICLE': {'US': 'THE', 'CA': 'THE', 'MX': 'THE'}, 'PLURAL': False},
    'CIV': {'NAME': {'US': 'Civil Authorities',        'CA': 'Civil Authorities',       'MX': 'SASMEX'},                  'ARTICLE': {'US': 'THE', 'CA': 'THE', 'MX': 'THE'}, 'PLURAL': True},
    'CTV': {'NAME': {'US': 'Civil Authorities',        'CA': 'Civil Authorities',       'MX': 'Civil Authority'},         'ARTICLE': {'US': 'THE', 'CA': 'THE', 'MX': 'THE'}, 'PLURAL': True},
    'PEP': {'NAME': {'US': 'Primary Entry Point',      'CA': 'Primary Entry Point',     'MX': 'Primary Entry Point'},     'ARTICLE': {'US': 'THE', 'CA': 'THE', 'MX': 'THE'}, 'PLURAL': False},
}


SAME__EEE = {
    # ── National / Tests ─────────────────────────────────────────────────────
    'EAN': 'Emergency Action Notification',
    'EAT': 'Emergency Action Termination',
    'NIC': 'National Information Center',
    'NPT': 'National Periodic Test',
    'RMT': 'Required Monthly Test',
    'RWT': 'Required Weekly Test',
    'DMO': 'Practice/Demo Warning',
    'ADR': 'Administrative Message',
    # ── Earthquake Alert (SASMEX) ──────────────────────────────────────────────
    'EQW': 'Seismic Alert (SASMEX)',
    # ── Civil Emergencies / Safety ──────────────────────────────────────
    'AVA': 'Avalanche Watch',
    'AVW': 'Avalanche Warning',
    'BLU': 'Blue Alert',
    'CAE': 'Child Abduction Emergency (AMBER Alert)',
    'CDW': 'Civil Danger Warning',
    'CEM': 'Civil Emergency Message',
    'EVI': 'Evacuation Immediate',
    'FRW': 'Fire Warning',
    'HMW': 'Hazardous Materials Warning',
    'LAE': 'Local Area Emergency',
    'LEW': 'Law Enforcement Warning',
    'NUW': 'Nuclear Power Plant Warning',
    'RHW': 'Radiological Hazard Warning',
    'SPW': 'Shelter in Place Warning',
    'TOE': '911 Telephone Outage Emergency',
    'VOW': 'Volcano Warning',
    # ── Biological and Health Hazards (Canada) ───────────────────────────────
    'BHW': 'Biological Hazard Warning',
    'BWW': 'Boil Water Warning',
    'FCW': 'Food Contamination Warning',
    'IFW': 'Industrial Fire Warning',
    # ── Severe Weather — Watches ───────────────────────────────────────
    'BZA': 'Blizzard Watch',
    'CFA': 'Coastal Flood Watch',
    'DSA': 'Dust Storm Watch',
    'EQA': 'Earthquake Watch',
    'EVA': 'Evacuation Watch',
    'EXA': 'Extreme Wind Watch',
    'FFA': 'Flash Flood Watch',
    'FLA': 'Flood Watch',
    'HTA': 'Hurricane Force Wind Watch',
    'HUA': 'Hurricane Watch',
    'HWA': 'High Wind Watch',
    'SVA': 'Severe Thunderstorm Watch',
    'TOA': 'Tornado Watch',
    'TRA': 'Tropical Storm Watch',
    'TSA': 'Tsunami Watch',
    'WFA': 'Wildfire Watch',
    'WSA': 'Winter Storm Watch',
    # ── Severe Weather — Warnings ───────────────────────────────
    'BZW': 'Blizzard Warning',
    'CFW': 'Coastal Flood Warning',
    'DSW': 'Dust Storm Warning',
    'EWW': 'Extreme Wind Warning',
    'FFW': 'Flash Flood Warning',
    'FLW': 'Flood Warning',
    'FSW': 'Flash Freeze Warning',
    'FGW': 'Dense Fog Warning',
    'HTW': 'Hurricane Force Wind Warning',
    'HUW': 'Hurricane Warning',
    'HWW': 'High Wind Warning',
    'IBW': 'Iceberg Warning',
    'LSW': 'Land Slide Warning',
    'MAW': 'Marine Warning',
    'SMW': 'Special Marine Warning',
    'SQW': 'Snow Squall Warning',
    'SSA': 'Storm Surge Watch',
    'SSW': 'Storm Surge Warning',
    'SVR': 'Severe Thunderstorm Warning',
    'TOR': 'Tornado Warning',
    'TRW': 'Tropical Storm Warning',
    'TSW': 'Tsunami Warning',
    'WFW': 'Wildfire Warning',
    'WSW': 'Winter Storm Warning',
    # ── Bulletins and Statements ───────────────────────────────────────────
    'FFS': 'Flash Flood Statement',
    'FLS': 'Flood Statement',
    'FLY': 'Flood Advisory',
    'HLS': 'Hurricane Local Statement',
    'HWY': 'High Wind Advisory',
    'MWS': 'Marine Weather Statement',
    'NOW': 'Short Term Forecast',
    'POS': 'Power Outage Statement',
    'SPS': 'Special Weather Statement',
    'SVS': 'Severe Weather Statement',
}

# Spanish translations for EAS event codes
SAME__EEE_ES = {
    'EAN': 'Notificación de Acción de Emergencia',
    'EAT': 'Terminación de Acción de Emergencia',
    'NIC': 'Centro Nacional de Información',
    'NPT': 'Prueba Periódica Nacional',
    'RMT': 'Prueba Mensual Requerida',
    'RWT': 'Prueba Semanal Requerida',
    'DMO': 'Advertencia de Práctica/Demostración',
    'ADR': 'Mensaje Administrativo',
    'EQW': 'Alerta Sísmica (SASMEX)',
    'AVA': 'Vigilancia de Avalancha',
    'AVW': 'Advertencia de Avalancha',
    'BLU': 'Alerta Azul',
    'CAE': 'Emergencia de Sustracción de Menor (Alerta AMBER)',
    'CDW': 'Advertencia de Peligro Civil',
    'CEM': 'Mensaje de Emergencia Civil',
    'EVI': 'Evacuación Inmediata',
    'FRW': 'Advertencia de Incendio',
    'HMW': 'Advertencia de Materiales Peligrosos',
    'LAE': 'Emergencia de Área Local',
    'LEW': 'Advertencia de Aplicación de Ley',
    'NUW': 'Advertencia de Planta Nuclear',
    'RHW': 'Advertencia de Peligro Radiológico',
    'SPW': 'Advertencia de Refugio en el Lugar',
    'TOE': 'Emergencia de Interrupción Telefónica 911',
    'VOW': 'Advertencia de Volcán',
    'BHW': 'Advertencia de Peligro Biológico',
    'BWW': 'Advertencia de Hervir Agua',
    'FCW': 'Advertencia de Contaminación Alimentaria',
    'IFW': 'Advertencia de Incendio Industrial',
    'BZA': 'Vigilancia de Tormenta de Nieve',
    'CFA': 'Vigilancia de Inundación Costera',
    'DSA': 'Vigilancia de Tormenta de Polvo',
    'EQA': 'Vigilancia de Terremoto',
    'EVA': 'Vigilancia de Evacuación',
    'EXA': 'Vigilancia de Viento Extremo',
    'FFA': 'Vigilancia de Inundación Repentina',
    'FLA': 'Vigilancia de Inundación',
    'HTA': 'Vigilancia de Viento de Fuerza Huracán',
    'HUA': 'Vigilancia de Huracán',
    'HWA': 'Vigilancia de Viento Fuerte',
    'SVA': 'Vigilancia de Tormenta Severa',
    'TOA': 'Vigilancia de Tornado',
    'TRA': 'Vigilancia de Tormenta Tropical',
    'TSA': 'Vigilancia de Tsunami',
    'WFA': 'Vigilancia de Incendio Forestal',
    'WSA': 'Vigilancia de Tormenta Invernal',
    'BZW': 'Advertencia de Tormenta de Nieve',
    'CFW': 'Advertencia de Inundación Costera',
    'DSW': 'Advertencia de Tormenta de Polvo',
    'EWW': 'Advertencia de Viento Extremo',
    'FFW': 'Advertencia de Inundación Repentina',
    'FLW': 'Advertencia de Inundación',
    'FSW': 'Advertencia de Congelamiento Repentino',
    'FGW': 'Advertencia de Niebla Densa',
    'HTW': 'Advertencia de Viento de Fuerza Huracán',
    'HUW': 'Advertencia de Huracán',
    'HWW': 'Advertencia de Viento Fuerte',
    'IBW': 'Advertencia de Iceberg',
    'LSW': 'Advertencia de Deslizamiento de Tierra',
    'MAW': 'Advertencia Marítima',
    'SMW': 'Advertencia Marítima Especial',
    'SQW': 'Advertencia de Chubasco de Nieve',
    'SSA': 'Vigilancia de Marejada Ciclónica',
    'SSW': 'Advertencia de Marejada Ciclónica',
    'SVR': 'Advertencia de Tormenta Severa',
    'TOR': 'Advertencia de Tornado',
    'TRW': 'Advertencia de Tormenta Tropical',
    'TSW': 'Advertencia de Tsunami',
    'WFW': 'Advertencia de Incendio Forestal',
    'WSW': 'Advertencia de Tormenta Invernal',
    'FFS': 'Declaración de Inundación Repentina',
    'FLS': 'Declaración de Inundación',
    'FLY': 'Aviso de Inundación',
    'HLS': 'Declaración Local de Huracán',
    'HWY': 'Aviso de Viento Fuerte',
    'MWS': 'Declaración de Clima Marítimo',
    'NOW': 'Pronóstico a Corto Plazo',
    'POS': 'Declaración de Corte de Energía',
    'SPS': 'Declaración Especial del Tiempo',
    'SVS': 'Declaración de Tiempo Severo',
}

# Spanish organization names by country
SAME__ORG_ES = {
    'EAS': {'US': 'Sistema de Alerta de Emergencias', 'CA': 'Sistema de Alerta de Emergencias', 'MX': 'Sistema de Alerta de Emergencias'},
    'WXR': {'US': 'Servicio Nacional de Meteorología', 'CA': 'Medio Ambiente Canadá',            'MX': 'Servicio Meteorológico Nacional'},
    'CIV': {'US': 'Autoridades Civiles',               'CA': 'Autoridades Civiles',               'MX': 'SASMEX'},
    'CTV': {'US': 'Autoridades Civiles',               'CA': 'Autoridades Civiles',               'MX': 'Autoridad Civil'},
    'PEP': {'US': 'Punto de Entrada Principal',        'CA': 'Punto de Entrada Principal',        'MX': 'Punto de Entrada Principal'},
}

# US States moved to us_defs.py for clarity.

# CA_PROVINCES has been moved to ca_defs.py

MSG__TEXT = {
    'EN': {
        'MSG1': '{article} {organization} {preposition} {location} {has} issued a {event} valid until {end}',
        'MSG2': '{conjunction}for the following {division} in {state}: ',
        'MSG3': '{county}{punc} ',
        'MSG4': '',
        'AND': 'and', 'ALL': 'all', 'HAS': 'has', 'HAVE': 'have',
        'THE': 'the', 'A': 'a', 'IN': 'in', '': '',
        'DIVISION_MX': 'boroughs/municipalities',
        'DIVISION_CA': 'counties/regions',
        'DIVISION_US': 'counties',
        'FOR': 'for the following',
        'IN_STATE': 'in',
    },
    'ES': {
        'AND': 'y', 'ALL': 'todos', 'HAS': 'ha', 'HAVE': 'han',
        'THE': 'la', 'A': 'una', 'IN': 'en', '': '',
        'DIVISION_MX': 'alcaldías/municipios',
        'DIVISION_CA': 'condados/regiones',
        'DIVISION_US': 'condados',
        'FOR': 'para los siguientes',
        'IN_STATE': 'en',
    },
}

DESCRIPTION = 'EAS-SAMEmon is an EAS/SAME message decoder with SASMEX (Mexico) support'
PROGRAM     = 'EAS-SAMEmon'
VERSION     = '1.0.0'

# ---------------------------------------------------------------------------
# Time functions
# ---------------------------------------------------------------------------

def alert_start(JJJHHMM, fmt='%j%H%M'):
    """Converts EAS date string to UTC-aware datetime."""
    utc_dt = datetime.datetime.strptime(JJJHHMM, fmt).replace(
        year=datetime.datetime.now(datetime.timezone.utc).year,
        tzinfo=datetime.timezone.utc
    )
    timestamp = calendar.timegm(utc_dt.timetuple())
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)

def fn_dt(dt, fmt='%I:%M %p'):
    """Formats datetime converting it to system local time if it is UTC."""
    if dt.tzinfo is None:
        # If naive, assume it's already local (backwards compatibility)
        return dt.strftime(fmt)
    # Convert to system local timezone
    return dt.astimezone().strftime(fmt)

def get_length(TTTT):
    def time_str(x, unit='hour'):
        if x == 1:   return f'{x} {unit}'
        elif x >= 2: return f'{x} {unit}s'
        return ''
    hh, mm = int(TTTT[:2]), int(TTTT[2:])
    return ' '.join(filter(None, (time_str(hh), time_str(mm, 'minute'))))

def alert_end(JJJHHMM, TTTT):
    start = alert_start(JJJHHMM)
    delta = datetime.timedelta(hours=int(TTTT[:2]), minutes=int(TTTT[2:]))
    return start + delta

def alert_length(TTTT):
    delta = datetime.timedelta(hours=int(TTTT[:2]), minutes=int(TTTT[2:]))
    return int(delta.total_seconds())

# ---------------------------------------------------------------------------
# Area decoding functions
# ---------------------------------------------------------------------------

def county_decode(code, COUNTRY):
    """Converts a PSSCCC code to (place, state/entity/province)."""
    if COUNTRY == 'MX':
        return mx_defs.decode_mx_area(code)

    if len(code) < 6:
        return code, 'Unknown'

    if COUNTRY == 'CA':
        return ca_defs.decode_ca_area(code)
    else:  # US (default)
        return us_defs.decode_us_area(code)

    return place, state

def get_division(SS, COUNTRY, lang='EN'):
    T = MSG__TEXT.get(lang, MSG__TEXT['EN'])
    if COUNTRY == 'MX':
        return T.get('DIVISION_MX', 'boroughs/municipalities')
    if COUNTRY == 'CA':
        return T.get('DIVISION_CA', 'counties/regions')
    return T.get('DIVISION_US', 'counties')

def get_event(EEE, lang='EN'):
    if lang == 'ES':
        return SAME__EEE_ES.get(EEE) or SAME__EEE.get(EEE) or f'Evento desconocido ({EEE})'
    return SAME__EEE.get(EEE) or f'Unknown event ({EEE})'

def get_org_name(ORG, COUNTRY, lang='EN'):
    if lang == 'ES':
        try:
            return SAME__ORG_ES[ORG][COUNTRY]
        except KeyError:
            pass
    try:
        return SAME__ORG[ORG]['NAME'][COUNTRY]
    except KeyError:
        return ORG

# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------

def detect_country(PSSCCC_list, LLLLLLLL, ORG, forced=None):
    """
    Detects the country of the EAS message.

    Priority:
      1. --country forced via CLI
      2. MX: Transmitter ID XCMX/* (see mx_defs.is_mx_message)
      3. CA: Callsign with C prefix and CXXX/YY format (e.g., CBLA/FM)
         and/or Canadian province code in area
      4. US: Any other case (W/K callsign or unidentified)
    """
    if forced:
        return forced.upper()

    if mx_defs.is_mx_message(ORG, PSSCCC_list, LLLLLLLL):
        return 'MX'

    # Country detection by callsign
    if LLLLLLLL:
        first = LLLLLLLL[0].upper()
        # W/* or K/* → definitely US (regardless of area FIPS codes)
        if first in ('W', 'K'):
            return 'US'
        # CXXX/YY → Canada
        if first == 'C' and len(LLLLLLLL) >= 4 and '/' in LLLLLLLL:
            return 'CA'

    # Canada detection by province code in the area
    for code in PSSCCC_list:
        if code and len(code) >= 3:
            ss = code[1:3]
            if ss in ca_defs.CA_PROVINCES:
                return 'CA'

    return 'US'

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def printf(output=''):
    output = ' '.join(output.lstrip().split())
    sys.stdout.write(output + '\n')

def format_error(info=''):
    logging.warning(f'INVALID FORMAT {info}')

# ---------------------------------------------------------------------------
# Readable message
# ---------------------------------------------------------------------------

def readable_message(ORG, EEE, PSSCCC_list, TTTT, JJJHHMM, STATION, TYPE,
                     LLLLLLLL, COUNTRY, LANG):
    import textwrap
    printf()

    lang_key = LANG if LANG in MSG__TEXT else 'EN'
    T = MSG__TEXT[lang_key]

    org_name  = get_org_name(ORG, COUNTRY, lang_key)
    event_str = get_event(EEE, lang_key)
    end_str   = fn_dt(alert_end(JJJHHMM, TTTT))

    # Transmitter
    if COUNTRY == 'MX':
        tx = mx_defs.get_mx_transmitter_info(LLLLLLLL)
        location_str = f"{tx['name']} ({tx.get('freq_mhz', '')} MHz)" if tx.get('freq_mhz') else tx['name']
    else:
        location_str = f'{STATION}/{TYPE}' if STATION else ''

    prep = T['IN'] if location_str else ''
    is_plural = SAME__ORG.get(ORG, {}).get('PLURAL', False)

    if lang_key == 'ES':
        has = T['HAVE'] if is_plural else T['HAS']
        article = 'La' if not is_plural else 'Las'
        MSG = [f'{article} {org_name} {prep} {location_str} {has} emitido una {event_str} válida hasta {end_str}.']
        # Areas
        current_state = None
        for idx, code in enumerate(PSSCCC_list):
            place, state = county_decode(code, COUNTRY)
            if current_state != state:
                division = get_division(code[1:3], COUNTRY, lang_key)
                conj = '' if idx == 0 else (T['AND'] + ' ')
                MSG.append(f' {conj}{T["FOR"]} {division} {T["IN_STATE"]} {state}:')
                current_state = state
            punc = ',' if idx != len(PSSCCC_list) - 1 else '.'
            MSG.append(f' {place}{punc}')
    else:
        article_key = SAME__ORG.get(ORG, {}).get('ARTICLE', {}).get(COUNTRY, 'THE')
        article = T.get(article_key, 'The').capitalize()
        has = T['HAVE'] if is_plural else T['HAS']
        MSG = [f'{article} {org_name} {prep} {location_str} {has} issued a {event_str} valid until {end_str}.']
        # Areas
        current_state = None
        for idx, code in enumerate(PSSCCC_list):
            place, state = county_decode(code, COUNTRY)
            if current_state != state:
                division = get_division(code[1:3], COUNTRY, lang_key)
                conj = '' if idx == 0 else (T['AND'] + ' ')
                MSG.append(f' {conj}for the following {division} in {state}:')
                current_state = state
            punc = ',' if idx != len(PSSCCC_list) - 1 else '.'
            MSG.append(f' {place}{punc}')

    MSG.append(f' ({LLLLLLLL})')

    full = ''.join(MSG)
    for line in textwrap.wrap(full, 78):
        printf(line)
    printf()
    return full

# ---------------------------------------------------------------------------
# Main cleaning and decoding
# ---------------------------------------------------------------------------

def clean_msg(same):
    valid_chars = string.ascii_uppercase + string.digits + '+-/*'
    same = same.upper()
    idx = same.find('ZCZC')
    if idx != -1:
        same = same[idx:]
    same = ''.join(same.split())
    same = ''.join(c for c in same if c in valid_chars)
    slen = len(same) - 1
    if same and same[slen] != '-':
        ridx   = same.rfind('-')
        offset = slen - ridx
        if offset <= 8:
            same = same.ljust(slen + (8 - offset) + 1, '?') + '-'
    return same

def same_decode(same, lang, same_watch=None, event_watch=None, text=True,
                call=None, command=None, jsonfile=None, country=None):
    try:
        same = clean_msg(same)
    except Exception:
        return

    msgidx = same.find('ZCZC')
    if msgidx != -1:
        logging.debug('-' * 30)
        S1 = S2 = None
        try:
            S1, S2 = same[msgidx:].split('+', 1)
        except ValueError:
            format_error(); return
        try:
            ZCZC, ORG, EEE, PSSCCC = S1.split('-', 3)
        except ValueError:
            format_error(); return

        logging.debug(f'   Originator: {ORG}')
        logging.debug(f'   Event Code: {EEE}')

        PSSCCC_list = PSSCCC.split('-')

        parts = S2.split('-', 3)
        if len(parts) < 2:
            format_error(); return
        # Fill missing fields (partial/truncated message)
        while len(parts) < 4:
            parts.append('')
        TTTT, JJJHHMM, LLLLLLLL, tail = parts

        logging.debug(f'   Purge Time: {TTTT}')
        logging.debug(f'    Date Code: {JJJHHMM}')
        logging.debug(f' Location ID: {LLLLLLLL}')

        try:
            STATION, TYPE = LLLLLLLL.split('/', 1)
        except ValueError:
            STATION = TYPE = None

        COUNTRY = detect_country(PSSCCC_list, LLLLLLLL, ORG, forced=country)
        logging.debug(f'      Country: {COUNTRY}')
        logging.debug('-' * 30)

        # Valid areas filtering for MX
        if COUNTRY == 'MX':
            valid_list = []
            for code in PSSCCC_list:
                if code == mx_defs.MX_ALL_AREA:
                    valid_list.append(code)
                elif code[1:3] in ('09', '15', '00'):
                    valid_list.append(code)
                else:
                    logging.warning(f'Unknown area code: {code}')
            PSSCCC_list = sorted(valid_list)

        # Watch filters check (same_watch and event_watch)
        if same_watch:
            watch_stripped = [w[1:] for w in same_watch]
            codes_stripped  = [c[1:] for c in PSSCCC_list]
            if not (set(watch_stripped) & set(codes_stripped)):
                return
        if event_watch and EEE not in event_watch:
            return

        # Decode areas for frontend
        areas_decoded = []
        for code in PSSCCC_list:
            place, state = county_decode(code, COUNTRY)
            areas_decoded.append({'code': code, 'place': place, 'state': state})

        # Generate readable message (always needed for JSON, optional for console)
        full_message = readable_message(
            ORG, EEE, PSSCCC_list, TTTT, JJJHHMM, STATION, TYPE,
            LLLLLLLL, COUNTRY, lang
        ) if text else None

        if not text:
            # If text output is disabled, generate it silently for JSON/Logs
            full_message = readable_message(
                ORG, EEE, PSSCCC_list, TTTT, JJJHHMM, STATION, TYPE,
                LLLLLLLL, COUNTRY, lang
            )

        if jsonfile:
            try:
                import json
                data = {
                    'ORG': ORG, 'EEE': EEE, 'TTTT': TTTT, 'JJJHHMM': JJJHHMM,
                    'STATION': STATION, 'TYPE': TYPE, 'LLLLLLLL': LLLLLLLL,
                    'COUNTRY': COUNTRY, 'LANG': lang,
                    'event': get_event(EEE, 'EN'),
                    'event_es': get_event(EEE, 'ES'),
                    'organization': get_org_name(ORG, COUNTRY, 'EN'),
                    'organization_es': get_org_name(ORG, COUNTRY, 'ES'),
                    'end': fn_dt(alert_end(JJJHHMM, TTTT)),
                    'start': fn_dt(alert_start(JJJHHMM)),
                    'PSSCCC_list': PSSCCC_list,
                    'areas_decoded': areas_decoded,
                    'length': get_length(TTTT),
                    'seconds': alert_length(TTTT),
                    'MESSAGE': full_message,
                }
                if COUNTRY == 'MX':
                    data['transmitter'] = mx_defs.get_mx_transmitter_info(LLLLLLLL)
                with open(jsonfile, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.error(e); return

        if command:
            fmt_cmd = ' '.join(command).format(
                ORG=ORG, EEE=EEE, TTTT=TTTT, JJJHHMM=JJJHHMM,
                LLLLLLLL=LLLLLLLL, COUNTRY=COUNTRY, LANG=lang,
                event=get_event(EEE), MESSAGE=full_message or '',
                end=fn_dt(alert_end(JJJHHMM, TTTT)),
                start=fn_dt(alert_start(JJJHHMM)),
            )
            if call:
                try:
                    subprocess.call([call] + fmt_cmd.split())
                except Exception as e:
                    logging.error(e)
            else:
                printf(fmt_cmd)

    else:
        if same.find('NNNN') == -1:
            logging.warning('Valid identifier not found.')
        else:
            logging.debug('End of Message: NNNN')

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=DESCRIPTION, prog=PROGRAM, fromfile_prefix_chars='@'
    )
    parser.add_argument('--msg',      help='Message to decode (text)')
    parser.add_argument('--same',     nargs='*', help='Filter by SAME/FIPS area code')
    parser.add_argument('--event',    nargs='*', help='Filter by event code (e.g., EQW RWT)')
    parser.add_argument('--lang',     default='EN', choices=['EN', 'ES'], help='Output language (default: EN)')
    parser.add_argument('--country',  default=None, choices=['US', 'CA', 'MX'], help='Force country (auto-detected if omitted)')
    parser.add_argument('--loglevel', default=40, type=int, choices=[10, 20, 30, 40, 50])
    parser.add_argument('--text',     dest='text', action='store_true',  help='Show readable message')
    parser.add_argument('--no-text',  dest='text', action='store_false', help='Suppress readable message')
    parser.add_argument('--version',  action='version', version=f'{PROGRAM} {VERSION}')
    parser.add_argument('--call',     help='Call external program when alert is received')
    parser.add_argument('--command',  nargs='*', help='External command with format')
    parser.add_argument('--json',     help='Save result to JSON file')
    parser.add_argument('--source',   help='Source program (e.g., EAS-SAMEmon)')
    parser.set_defaults(text=True)
    return parser.parse_known_args()[0]

def main():
    args = parse_arguments()
    logging.basicConfig(level=args.loglevel, format='%(levelname)s: %(message)s')

    kwargs = dict(
        lang=args.lang, same_watch=args.same, event_watch=args.event,
        text=args.text, call=args.call, command=args.command,
        jsonfile=args.json, country=args.country,
    )

    if args.msg:
        same_decode(args.msg, **kwargs)
    elif args.source:
        try:
            proc = subprocess.Popen(args.source, stdout=subprocess.PIPE, shell=True)
        except Exception as e:
            logging.error(e); return
        for line in proc.stdout:
            logging.debug(line)
            same_decode(line.decode('utf-8', errors='replace'), **kwargs)
    else:
        for line in sys.stdin:
            logging.debug(line)
            same_decode(line, **kwargs)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
