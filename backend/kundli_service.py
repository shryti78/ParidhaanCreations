"""
Kundli (birth chart) calculation service.

Real astronomical positions via Swiss Ephemeris (pyswisseph), same engine
the panchang service uses. Everything in here is deterministic Vedic
astrology math — the same input produces the same chart, as you'd get
from a trained Jyotishi with a traditional ephemeris.

What the service computes
-------------------------
  - Lagna (Ascendant): sidereal longitude at birth → rashi + nakshatra
                       + pada
  - Chandra rashi (Moon sign) and Janma Nakshatra
  - Nine grahas' sidereal positions: Sun, Moon, Mars, Mercury, Jupiter,
    Venus, Saturn, Rahu (mean node), Ketu (= Rahu + 180°)
    For each: longitude, rashi, rashi lord, nakshatra, nakshatra lord,
    pada, whole-sign house (bhava)
  - Current Vimshottari Mahadasha (120-year planetary period cycle
    starting from Moon's nakshatra at birth)

Whole-sign house system is used (standard Vedic), computed from the
ascendant's rashi — no Placidus / Koch / Alcabitus required.

Entry point
-----------
    compute_kundli(name, date_of_birth, time_of_birth, place_of_birth)
    → structured dict, JSON-serialisable
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Reuse the city database from the panchang service — no duplication
from panchang_service import resolve_location  # noqa: E402

try:
    import swisseph as swe
    # Lahiri ayanamsa is the government-of-India-recognised standard used by
    # almost every Indian Panchang + jyotish software.
    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    _SWE_OK = True
except Exception as e:  # pragma: no cover
    logger.warning("pyswisseph not available: %s. Kundli will error.", e)
    swe = None  # type: ignore
    _SWE_OK = False


# ============================================================================
# Reference data
# ============================================================================

RASHIS_EN = [
    "Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya",
    "Tula", "Vrishchika", "Dhanu", "Makara", "Kumbha", "Meena",
]
RASHIS_HI = [
    "मेष", "वृषभ", "मिथुन", "कर्क", "सिंह", "कन्या",
    "तुला", "वृश्चिक", "धनु", "मकर", "कुंभ", "मीन",
]
RASHIS_WESTERN = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]
RASHI_LORDS = [
    "Mangal", "Shukra", "Budha", "Chandra", "Surya", "Budha",
    "Shukra", "Mangal", "Guru", "Shani", "Shani", "Guru",
]

# Nakshatra names + lords — already in panchang_service but duplicated here
# to keep this file self-contained
NAKSHATRA_NAMES_EN = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira",
    "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha",
    "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati",
    "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha",
    "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
]
NAKSHATRA_LORDS = [
    "Ketu", "Shukra", "Surya", "Chandra", "Mangal",
    "Rahu", "Guru", "Shani", "Budha", "Ketu",
    "Shukra", "Surya", "Chandra", "Mangal", "Rahu",
    "Guru", "Shani", "Budha", "Ketu", "Shukra",
    "Surya", "Chandra", "Mangal", "Rahu", "Guru",
    "Shani", "Budha",
]

# Planets we calculate (Ketu added separately as Rahu + 180°)
PLANETS: list[dict] = [
    {"name": "Sun",      "hindi": "सूर्य",   "key": "Surya"},
    {"name": "Moon",     "hindi": "चंद्र",   "key": "Chandra"},
    {"name": "Mars",     "hindi": "मंगल",    "key": "Mangal"},
    {"name": "Mercury",  "hindi": "बुध",     "key": "Budha"},
    {"name": "Jupiter",  "hindi": "गुरु",    "key": "Guru"},
    {"name": "Venus",    "hindi": "शुक्र",   "key": "Shukra"},
    {"name": "Saturn",   "hindi": "शनि",     "key": "Shani"},
    {"name": "Rahu",     "hindi": "राहु",    "key": "Rahu"},  # mean node
]

# Vimshottari Dasha order + years. Total 120 years, repeats.
VIMSHOTTARI_SEQ = [
    ("Ketu",    7),
    ("Shukra", 20),
    ("Surya",   6),
    ("Chandra",10),
    ("Mangal",  7),
    ("Rahu",   18),
    ("Guru",   16),
    ("Shani",  19),
    ("Budha",  17),
]

# Nakshatra → first Vimshottari lord mapping.
# Pattern: Ashwini=Ketu, Bharani=Venus, Krittika=Sun, Rohini=Moon, …
# Repeats every 9 nakshatras.
NAKSHATRA_TO_VIMSHOTTARI_IDX = [i % 9 for i in range(27)]


# ============================================================================
# Low-level helpers
# ============================================================================

def _planet_id(name: str) -> int:
    """Map a planet name to the pyswisseph constant."""
    table = {
        "Sun":     swe.SUN,
        "Moon":    swe.MOON,
        "Mars":    swe.MARS,
        "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER,
        "Venus":   swe.VENUS,
        "Saturn":  swe.SATURN,
        "Rahu":    swe.MEAN_NODE,
    }
    return table[name]


def _parse_birth_dt(date_str: str, time_str: Optional[str]) -> datetime:
    """YYYY-MM-DD plus HH:MM → local naive datetime.

    When the birth time is missing we fall back to noon — birth-chart
    positions are still close enough for the slow-moving grahas (Jupiter,
    Saturn, Rahu). The Ascendant and Moon will be approximate; caller
    should surface a warning when time was missing.
    """
    if not time_str or time_str.strip() == "":
        time_str = "12:00"
    # Accept HH:MM or HH:MM:SS
    time_parts = time_str.strip().split(":")
    while len(time_parts) < 3:
        time_parts.append("0")
    hh, mm, ss = (int(p) for p in time_parts[:3])
    y, m, d = (int(p) for p in date_str.split("-"))
    return datetime(y, m, d, hh, mm, ss)


def _julian_day(local_dt: datetime, tz_offset_hours: float) -> float:
    ut_dt = local_dt - timedelta(hours=tz_offset_hours)
    hour_frac = ut_dt.hour + ut_dt.minute / 60.0 + ut_dt.second / 3600.0
    return swe.julday(ut_dt.year, ut_dt.month, ut_dt.day, hour_frac)


def _rashi_idx_from_longitude(lng: float) -> int:
    return int(lng // 30) % 12


def _nakshatra_pada_from_longitude(lng: float) -> tuple[int, int, float]:
    """Returns (nakshatra_index 0-26, pada 1-4, fraction 0-1 within nakshatra)."""
    span = 360.0 / 27  # 13° 20'
    idx = int(lng // span)
    offset = lng - idx * span
    pada = int(offset // (span / 4)) + 1
    fraction = offset / span
    return idx, pada, fraction


# ============================================================================
# Ascendant and planet positions
# ============================================================================

def _sidereal_ascendant(jd_ut: float, lat: float, lng: float) -> float:
    """
    Returns the sidereal Ascendant longitude in degrees.

    swe.houses_ex gives us the tropical Ascendant; we subtract the Lahiri
    ayanamsa at the same JD to convert to sidereal. We use whole-sign
    houses, so the specific house-system argument doesn't matter — we only
    read ascmc[0] (the Ascendant).
    """
    _, ascmc = swe.houses_ex(jd_ut, lat, lng, b"W", 0)
    tropical_asc = ascmc[0]
    ayanamsa = swe.get_ayanamsa_ut(jd_ut)
    return (tropical_asc - ayanamsa) % 360.0


def _sidereal_planet_longitude(jd_ut: float, planet_name: str) -> float:
    pid = _planet_id(planet_name)
    flags = swe.FLG_SIDEREAL
    result = swe.calc_ut(jd_ut, pid, flags)
    return float(result[0][0])


def _planet_record(name: str, hindi: str, longitude: float, asc_rashi_idx: int) -> dict:
    """Common record builder for a planet at a given sidereal longitude."""
    rashi_idx = _rashi_idx_from_longitude(longitude)
    nak_idx, pada, _frac = _nakshatra_pada_from_longitude(longitude)
    # Whole-sign Vedic house: distance from Ascendant's rashi + 1
    house = ((rashi_idx - asc_rashi_idx) % 12) + 1
    return {
        "name": name,
        "name_hindi": hindi,
        "longitude": round(longitude, 2),
        "rashi": RASHIS_EN[rashi_idx],
        "rashi_hindi": RASHIS_HI[rashi_idx],
        "rashi_western": RASHIS_WESTERN[rashi_idx],
        "rashi_lord": RASHI_LORDS[rashi_idx],
        "nakshatra": NAKSHATRA_NAMES_EN[nak_idx],
        "nakshatra_lord": NAKSHATRA_LORDS[nak_idx],
        "pada": pada,
        "house": house,
    }


# ============================================================================
# Vimshottari Dasha
# ============================================================================

def _current_mahadasha(moon_longitude: float, birth_dt: datetime, now: Optional[datetime] = None) -> dict:
    """
    Compute current Vimshottari Mahadasha.

    The first dasha's lord is the ruling lord of the nakshatra Moon occupies at
    birth. Its duration at birth = (1 − fraction_through_nakshatra) × its full
    years. Successive dashas then play out in VIMSHOTTARI_SEQ order.

    Walks forward from birth until the elapsed time reaches today's age.
    """
    now = now or datetime.now()
    age_years = max(0.0, (now - birth_dt).total_seconds() / (365.25 * 86400.0))

    nak_idx, _pada, fraction = _nakshatra_pada_from_longitude(moon_longitude)
    seq_start = NAKSHATRA_TO_VIMSHOTTARI_IDX[nak_idx]  # index into VIMSHOTTARI_SEQ

    # Remaining years of the first dasha at birth
    first_lord, first_full_years = VIMSHOTTARI_SEQ[seq_start]
    remaining_at_birth = (1.0 - fraction) * first_full_years

    # Walk forward through dashas
    elapsed_total = 0.0
    seq_idx = seq_start
    current_lord = first_lord
    current_duration = remaining_at_birth

    while elapsed_total + current_duration < age_years:
        elapsed_total += current_duration
        seq_idx = (seq_idx + 1) % len(VIMSHOTTARI_SEQ)
        current_lord, current_duration = VIMSHOTTARI_SEQ[seq_idx]
        # For dashas AFTER the birth-dasha, the full years apply (not partial)

    years_in = age_years - elapsed_total
    years_remaining = current_duration - years_in

    end_dt = birth_dt + timedelta(days=(elapsed_total + current_duration) * 365.25)
    start_dt = birth_dt + timedelta(days=elapsed_total * 365.25)

    return {
        "lord": current_lord,
        "total_years": round(current_duration, 2),
        "years_elapsed": round(years_in, 2),
        "years_remaining": round(years_remaining, 2),
        "started_approx": start_dt.strftime("%Y-%m-%d"),
        "ends_approx": end_dt.strftime("%Y-%m-%d"),
    }


# ============================================================================
# Main entry
# ============================================================================

def compute_kundli(
    name: str,
    date_of_birth: str,
    time_of_birth: Optional[str],
    place_of_birth: str,
) -> dict:
    """
    Compute the user's Vedic birth chart from basic inputs.

    Args:
        name: Display name (echoed back in the profile block)
        date_of_birth: "YYYY-MM-DD"
        time_of_birth: "HH:MM" or "HH:MM:SS". Falls back to noon when empty,
                       in which case `time_was_missing: True` is flagged.
        place_of_birth: Free-text city. Falls back to New Delhi; the
                        resolved city is echoed back.

    Returns:
        JSON-safe dict containing Lagna, Rashi, Janma Nakshatra, all 9
        grahas and the current Mahadasha.
    """
    if not _SWE_OK:
        raise RuntimeError(
            "Astronomical engine not available — install pyswisseph on the backend"
        )

    # Location
    lat, lng, tz_offset, matched_city = resolve_location(place_of_birth or "")
    time_was_missing = not time_of_birth or time_of_birth.strip() == ""

    # Birth datetime (local) → Julian day (UT)
    birth_dt = _parse_birth_dt(date_of_birth, time_of_birth)
    jd_ut = _julian_day(birth_dt, tz_offset)

    # Sidereal Ascendant
    asc_lng = _sidereal_ascendant(jd_ut, lat, lng)
    asc_rashi_idx = _rashi_idx_from_longitude(asc_lng)
    asc_nak_idx, asc_pada, _ = _nakshatra_pada_from_longitude(asc_lng)

    # Planets (sidereal)
    planets_data: list[dict] = []
    for p in PLANETS:
        p_lng = _sidereal_planet_longitude(jd_ut, p["name"])
        planets_data.append(_planet_record(p["name"], p["hindi"], p_lng, asc_rashi_idx))

    # Ketu = Rahu + 180° sidereal
    rahu_lng = next(x["longitude"] for x in planets_data if x["name"] == "Rahu")
    ketu_lng = (rahu_lng + 180.0) % 360.0
    planets_data.append(_planet_record("Ketu", "केतु", ketu_lng, asc_rashi_idx))

    # Moon-based derived info
    moon = next(x for x in planets_data if x["name"] == "Moon")
    moon_lng = moon["longitude"]

    # Current Vimshottari Mahadasha from Moon's nakshatra
    mahadasha = _current_mahadasha(moon_lng, birth_dt)

    return {
        "profile": {
            "name": name,
            "dob": date_of_birth,
            "tob": time_of_birth or None,
            "pob": matched_city,
            "coordinates": {"lat": round(lat, 4), "lng": round(lng, 4)},
            "timezone_offset_hours": tz_offset,
        },
        "warnings": (
            ["Birth time missing — chart computed at noon; Ascendant/Moon"
             " positions are approximate."]
            if time_was_missing else []
        ),
        "lagna": {
            "longitude": round(asc_lng, 2),
            "rashi": RASHIS_EN[asc_rashi_idx],
            "rashi_hindi": RASHIS_HI[asc_rashi_idx],
            "rashi_western": RASHIS_WESTERN[asc_rashi_idx],
            "rashi_lord": RASHI_LORDS[asc_rashi_idx],
            "nakshatra": NAKSHATRA_NAMES_EN[asc_nak_idx],
            "nakshatra_lord": NAKSHATRA_LORDS[asc_nak_idx],
            "pada": asc_pada,
        },
        "rashi": {
            # Moon sign — the classical "rashi" question people ask
            "name": moon["rashi"],
            "name_hindi": moon["rashi_hindi"],
            "lord": moon["rashi_lord"],
        },
        "janma_nakshatra": {
            "name": moon["nakshatra"],
            "lord": moon["nakshatra_lord"],
            "pada": moon["pada"],
        },
        "planets": planets_data,
        "mahadasha_current": mahadasha,
        "engine": "Swiss Ephemeris (Lahiri sidereal · whole-sign houses)",
    }
