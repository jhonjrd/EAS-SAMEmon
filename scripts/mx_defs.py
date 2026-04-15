# mx_defs.py - Mexican definitions for EAS-SAMEmon
# Data based on official CIRES (Centro de Instrumentación y Registro Sísmico A.C.)
# documentation, November 2025.
#
# SASMEX = Mexican Seismic Alert System
# SARMEX = Mexican Emergency Alert and Response System
#

# ---------------------------------------------------------------------------
# EAS Event Codes used by SASMEX
# ---------------------------------------------------------------------------
MX_EVENTS = {
    'EQW': 'Seismic Alert',   # Earthquake Warning - activates sonore alarm for 1 min
    'RWT': 'Required Weekly Test',  # Required Weekly Test
}

# ---------------------------------------------------------------------------
# Organizations/Originators used by SASMEX
# ---------------------------------------------------------------------------
# CIV = Non-governmental civil authority (the one used by SASMEX)
# The CIV key already exists in the US EAS standard; it is only extended here.
MX_ORG_NAMES = {
    'CIV': 'SASMEX',
    'CTV': 'Civil Authority',
}

# ---------------------------------------------------------------------------
# Mexican FIPS area codes (PSSCCC format)
# P  = subdivision (0 = whole state)
# SS = state entity code (09 = CDMX, 15 = State of Mexico)
# CCC= municipality/borough code (000 = whole entity)
# ---------------------------------------------------------------------------

# Federal Entities (SS)
MX_SAME_AREA = {
    '00': 'National',
    '09': 'Ciudad de México',
    '15': 'Estado de México',
}

# CDMX boroughs and covered municipalities
MX_SAME_CODE = {
    # CDMX - code 000 = whole entity
    '09000': 'Ciudad de México (complete)',
    # CDMX boroughs
    '09002': 'Azcapotzalco',
    '09003': 'Coyoacán',
    '09004': 'Cuajimalpa de Morelos',
    '09005': 'Gustavo A. Madero',
    '09006': 'Iztacalco',
    '09007': 'Iztapalapa',
    '09008': 'La Magdalena Contreras',
    '09009': 'Milpa Alta',
    '09010': 'Álvaro Obregón',
    '09011': 'Tláhuac',
    '09012': 'Tlalpan',
    '09013': 'Xochimilco',
    '09014': 'Benito Juárez',
    '09015': 'Cuauhtémoc',
    '09016': 'Miguel Hidalgo',
    '09017': 'Venustiano Carranza',
    # State of Mexico - code 000 = whole state
    '15000': 'Estado de México (complete)',
    # State of Mexico municipalities with documented coverage
    '15010': 'Huixquilucan',
}

# Special code: 000000 = whole coverage area (used in EQW)
MX_ALL_AREA = '000000'

# ---------------------------------------------------------------------------
# EAS-SAME-SARMEX Transmitters in Mexico
# Source: SASMEX / Alerta-Sismica.net
# ---------------------------------------------------------------------------
MX_TRANSMITTERS = {
    'XCMX/003': {'name': 'CENAPRED',     'freq_mhz': 162.400, 'entidad': 'CDMX',     'municipio': 'Coyoacán'},
    'XCMX/011': {'name': 'Teuhitl',      'freq_mhz': 162.450, 'entidad': 'CDMX',     'municipio': 'Tláhuac'},
    'XCMX/004': {'name': 'Cuajimalpa',   'freq_mhz': 162.500, 'entidad': 'CDMX',     'municipio': 'Cuajimalpa'},
    'XMEX/037': {'name': 'Las Palmas',   'freq_mhz': 162.525, 'entidad': 'EDOMEX',   'municipio': 'Huixquilucan'},
    'XCMX/005': {'name': 'Zacatenco',    'freq_mhz': 162.550, 'entidad': 'CDMX',     'municipio': 'Gustavo A. Madero'},
    'XOAX/067': {'name': 'Oaxaca',       'freq_mhz': 162.400, 'entidad': 'OAXACA',   'municipio': 'Oaxaca de Juárez'},
    'XGRO/001': {'name': 'Veladero',     'freq_mhz': 162.400, 'entidad': 'GUERRERO',  'municipio': 'Acapulco'},
    'XGRO/002': {'name': 'Frontera',     'freq_mhz': 162.450, 'entidad': 'GUERRERO',  'municipio': 'Chilpancingo'},
    'XMOR/009': {'name': 'Chichinautzin','freq_mhz': 162.475, 'entidad': 'MORELOS',   'municipio': 'Huitzilac'},
    'XMOR/017': {'name': 'El Zapote',    'freq_mhz': 162.400, 'entidad': 'MORELOS',   'municipio': 'Puente de Ixtla'},
    'XMCH/053': {'name': 'Las Flores',   'freq_mhz': 162.450, 'entidad': 'MICHOACÁN', 'municipio': 'Morelia'},
    'XPUE/001': {'name': 'Altamira',     'freq_mhz': 162.475, 'entidad': 'PUEBLA',    'municipio': 'Ocoyucan'},
    'XPUE/085': {'name': 'Izúcar',       'freq_mhz': 162.550, 'entidad': 'PUEBLA',    'municipio': 'Izúcar de Matamoros'},
    'XPUE/094': {'name': 'Libres',       'freq_mhz': None,    'entidad': 'PUEBLA',    'municipio': 'Libres'},
    'XPUE/003': {'name': 'Acatlán',      'freq_mhz': None,    'entidad': 'PUEBLA',    'municipio': 'Acatlán de Osorio'},
    'XPUE/156': {'name': 'Tehuacán',     'freq_mhz': None,    'entidad': 'PUEBLA',    'municipio': 'Tehuacán'},
}

# ---------------------------------------------------------------------------
# SASMEX Message Parameters
# ---------------------------------------------------------------------------
# Validity duration by event type
MX_DURATIONS = {
    'EQW': '0001',   # 1 minute (active seismic alert)
    'RWT': '0300',   # 3 hours (weekly test)
}

# RWT test schedule (Local CDMX time, UTC-6)
MX_RWT_SCHEDULE_LOCAL = ['02:45', '05:45', '08:45', '11:45', '14:45', '17:45', '20:45', '23:45']

# Pause before NNNN
# EQW: 100 ms  |  RWT: 1000 ms
MX_PAUSE_MS = {
    'EQW': 100,
    'RWT': 1000,
}

# ---------------------------------------------------------------------------
# Readable message templates in English (Fallback)
# ---------------------------------------------------------------------------
MSG__TEXT_EN = {
    'MSG1': '{organization} has issued a {event} valid until {end}',
    'MSG2': '{conjunction}for the following {division} in {state}: ',
    'MSG3': '{county}{punc} ',
    'MSG4': '',
    'AND': 'and',
    'ALL': 'all',
    'HAS': 'has',
    'HAVE': 'have',
    'THE': 'the',
    'A': 'a',
    'IN': 'in',
    '': '',
    'DIVISION_MX': 'boroughs/municipalities',
}

# ---------------------------------------------------------------------------
# Mexican message detection
# Returns True if the message originates from SASMEX
# ---------------------------------------------------------------------------
def is_mx_message(ORG, PSSCCC_list, LLLLLLLL):
    """
    Detects if an EAS message originates from SASMEX/MX.
    """
    if LLLLLLLL:
        upper_lll = LLLLLLLL.upper()
        # 1. Definitive Mexico indicators: known transmitter prefixes
        # Seen in MX_TRANSMITTERS: XCMX, XMEX, XOAX, XGRO, XMOR, XMCH, XPUE
        if any(upper_lll.startswith(p) for p in ('XCMX', 'XMEX', 'XOAX', 'XGRO', 'XMOR', 'XMCH', 'XPUE')):
            return True

        # 2. Known foreign callsigns -> discard MX immediately
        first = upper_lll[0]
        if first in ('W', 'K'): # USA
            return False
        if first == 'C' and '/' in upper_lll: # Canada
            return False

    # 3. If callsign is unknown or empty, use area codes + originator.
    for code in PSSCCC_list:
        if not code:
            continue
        ss = code[1:3] if len(code) >= 3 else ''
        # SS='09' (CDMX) is unique to MX.
        if ss == '09':
            return True
        # SS='15' (EdoMex) matches New Brunswick (CA).
        # But if we reach here and didn't detect 'C' above, we prefer MX if ORG is CIV/CTV.
        if ss == '15' and ORG in ('CIV', 'CTV'):
            return True
        # SASMEX uses 000000 for national coverage
        if ss == '00' and ORG in ('CIV', 'CTV'):
            return True

    return False


def decode_mx_area(code):
    """Converts a Mexican PSSCCC code to readable text."""
    if code == MX_ALL_AREA:
        return 'the entire SASMEX coverage area', 'Ciudad de México'
    P   = code[:1]
    SS  = code[1:3]
    CCC = code[3:]
    SSCCC = code[1:]

    if CCC == '000':
        place = 'complete'
    else:
        place = MX_SAME_CODE.get(SSCCC, f'area {SSCCC}')

    state = MX_SAME_AREA.get(SS, f'entity {SS}')
    return place, state


def get_mx_transmitter_info(LLLLLLLL):
    """Returns transmitter information given the LLLLLLLL field."""
    return MX_TRANSMITTERS.get(LLLLLLLL, {'name': LLLLLLLL, 'freq_mhz': None})
