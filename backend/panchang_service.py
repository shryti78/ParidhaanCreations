"""
Panchang calculation service.

Uses the Swiss Ephemeris (pyswisseph) for real astronomical positions —
the same engine powering Drik Panchang and most trusted Hindu calendars.
Everything in here is deterministic math:

  - Tithi, Nakshatra, Yoga, Karana  → computed from Sun/Moon longitudes
                                      at local sunrise
  - Sunrise, Sunset                  → Swiss Ephemeris rise/set
  - Rahu Kaal, Gulika Kaal           → classical weekday-segment rule
                                      applied to the daylight window
  - Abhijit Muhurat                  → midday ± 24 minutes

No LLM, no external API, no network calls at request time. Ephemeris
data up to the current era is bundled with pyswisseph (Moshier fallback
is used if ephemeris files aren't present, with ~1 arcminute accuracy
over several centuries — plenty for Panchang).

Entry point: `compute_panchang(date_str, location_str)` →
structured dict safe to serialise as JSON.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import swisseph as swe
    # Use Moshier semi-analytical model — no ephemeris files needed, still
    # accurate to about 1 arcminute for the next few centuries.
    swe.set_ephe_path("")
    _SWE_OK = True
except Exception as e:  # pragma: no cover
    logger.warning("pyswisseph not available: %s. Panchang will error.", e)
    swe = None  # type: ignore
    _SWE_OK = False


# ============================================================================
# Reference data
# ============================================================================

TITHI_NAMES_EN = [
    "Pratipada", "Dvitiya", "Tritiya", "Chaturthi", "Panchami",
    "Shashthi", "Saptami", "Ashtami", "Navami", "Dashami",
    "Ekadashi", "Dvadashi", "Trayodashi", "Chaturdashi", "Purnima",
]
TITHI_NAMES_HI = [
    "प्रतिपदा", "द्वितीया", "तृतीया", "चतुर्थी", "पंचमी",
    "षष्ठी", "सप्तमी", "अष्टमी", "नवमी", "दशमी",
    "एकादशी", "द्वादशी", "त्रयोदशी", "चतुर्दशी", "पूर्णिमा",
]
# Krishna 15 is Amavasya, replaces Purnima above

NAKSHATRA_NAMES_EN = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira",
    "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha",
    "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati",
    "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha",
    "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha",
    "Purva Bhadrapada", "Uttara Bhadrapada", "Revati",
]
NAKSHATRA_NAMES_HI = [
    "अश्विनी", "भरणी", "कृत्तिका", "रोहिणी", "मृगशिरा",
    "आर्द्रा", "पुनर्वसु", "पुष्य", "आश्लेषा", "मघा",
    "पूर्वाफाल्गुनी", "उत्तराफाल्गुनी", "हस्त", "चित्रा", "स्वाती",
    "विशाखा", "अनुराधा", "ज्येष्ठा", "मूल", "पूर्वाषाढ़ा",
    "उत्तराषाढ़ा", "श्रवण", "धनिष्ठा", "शतभिषा",
    "पूर्वाभाद्रपद", "उत्तराभाद्रपद", "रेवती",
]
# Nakshatra lords in Vimshottari dasha order
NAKSHATRA_LORDS = [
    "Ketu", "Shukra", "Surya", "Chandra", "Mangal",
    "Rahu", "Guru", "Shani", "Budha", "Ketu",
    "Shukra", "Surya", "Chandra", "Mangal", "Rahu",
    "Guru", "Shani", "Budha", "Ketu", "Shukra",
    "Surya", "Chandra", "Mangal", "Rahu", "Guru",
    "Shani", "Budha",
]

YOGA_NAMES_EN = [
    "Vishkumbha", "Priti", "Ayushman", "Saubhagya", "Shobhana",
    "Atiganda", "Sukarma", "Dhriti", "Shula", "Ganda",
    "Vriddhi", "Dhruva", "Vyaghata", "Harshana", "Vajra",
    "Siddhi", "Vyatipata", "Variyan", "Parigha", "Shiva",
    "Siddha", "Sadhya", "Shubha", "Shukla", "Brahma",
    "Indra", "Vaidhriti",
]
YOGA_NAMES_HI = [
    "विष्कम्भ", "प्रीति", "आयुष्मान", "सौभाग्य", "शोभन",
    "अतिगण्ड", "सुकर्मा", "धृति", "शूल", "गण्ड",
    "वृद्धि", "ध्रुव", "व्याघात", "हर्षण", "वज्र",
    "सिद्धि", "व्यतिपात", "वरीयान", "परिघ", "शिव",
    "सिद्ध", "साध्य", "शुभ", "शुक्ल", "ब्रह्मा",
    "इन्द्र", "वैधृति",
]

# Karana: 7 movable + 4 fixed. Half-tithi index (1-60) maps as:
#   1   → Kimstughna (fixed, 2nd half of Krishna 14... wait)
#
# Standard mapping (0-indexed half-tithi):
#   0            → Kimstughna
#   1-56         → cycle of 7 movable (Bava, Balava, ..., Vishti), 8 rounds
#   57           → Shakuni
#   58           → Chatushpada
#   59           → Naga
MOVABLE_KARANAS = ["Bava", "Balava", "Kaulava", "Taitila", "Garaja", "Vanija", "Vishti (Bhadra)"]
FIXED_KARANAS = {0: "Kimstughna", 57: "Shakuni", 58: "Chatushpada", 59: "Naga"}

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_HI = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]

# Rahu Kaal: 1/8 of daylight, which segment by weekday.
# 0-indexed segment (0 = 1st 1/8 of day after sunrise).
# Python weekday(): Mon=0, Tue=1, ..., Sun=6
RAHU_KAAL_SEGMENT = {0: 1, 1: 6, 2: 4, 3: 5, 4: 3, 5: 2, 6: 7}

# Gulika Kaal: same idea, different mapping
GULIKA_KAAL_SEGMENT = {0: 5, 1: 4, 2: 3, 3: 2, 4: 1, 5: 0, 6: 6}


# ============================================================================
# Cities — lat / lng / IANA-equivalent UTC offset (hours)
# ============================================================================
# All Indian cities = IST (+5.5). A handful of diaspora cities with their
# own offsets. Case-insensitive, substring match on the first token.
# ============================================================================

IST = 5.5

CITIES: dict[str, tuple[float, float, float]] = {
    # India — top 40
    "delhi":       (28.6139,  77.2090,  IST),
    "new delhi":   (28.6139,  77.2090,  IST),
    "mumbai":      (19.0760,  72.8777,  IST),
    "bombay":      (19.0760,  72.8777,  IST),
    "kolkata":     (22.5726,  88.3639,  IST),
    "calcutta":    (22.5726,  88.3639,  IST),
    "chennai":     (13.0827,  80.2707,  IST),
    "madras":      (13.0827,  80.2707,  IST),
    "bangalore":   (12.9716,  77.5946,  IST),
    "bengaluru":   (12.9716,  77.5946,  IST),
    "hyderabad":   (17.3850,  78.4867,  IST),
    "ahmedabad":   (23.0225,  72.5714,  IST),
    "pune":        (18.5204,  73.8567,  IST),
    "surat":       (21.1702,  72.8311,  IST),
    "jaipur":      (26.9124,  75.7873,  IST),
    "lucknow":     (26.8467,  80.9462,  IST),
    "kanpur":      (26.4499,  80.3319,  IST),
    "nagpur":      (21.1458,  79.0882,  IST),
    "indore":      (22.7196,  75.8577,  IST),
    "bhopal":      (23.2599,  77.4126,  IST),
    "patna":       (25.5941,  85.1376,  IST),
    "vadodara":    (22.3072,  73.1812,  IST),
    "ghaziabad":   (28.6692,  77.4538,  IST),
    "ludhiana":    (30.9010,  75.8573,  IST),
    "agra":        (27.1767,  78.0081,  IST),
    "nashik":      (19.9975,  73.7898,  IST),
    "varanasi":    (25.3176,  82.9739,  IST),
    "benares":     (25.3176,  82.9739,  IST),
    "amritsar":    (31.6340,  74.8723,  IST),
    "chandigarh":  (30.7333,  76.7794,  IST),
    "coimbatore":  (11.0168,  76.9558,  IST),
    "kochi":       (9.9312,   76.2673,  IST),
    "cochin":      (9.9312,   76.2673,  IST),
    "thiruvananthapuram": (8.5241, 76.9366, IST),
    "trivandrum":  (8.5241,   76.9366,  IST),
    "vrindavan":   (27.5820,  77.7064,  IST),
    "mathura":     (27.4924,  77.6737,  IST),
    "barsana":     (27.6500,  77.3833,  IST),
    "ayodhya":     (26.7922,  82.1998,  IST),
    "haridwar":    (29.9457,  78.1642,  IST),
    "rishikesh":   (30.0869,  78.2676,  IST),
    "ujjain":      (23.1793,  75.7849,  IST),
    "tirupati":    (13.6288,  79.4192,  IST),
    "puri":        (19.8135,  85.8312,  IST),
    "dwarka":      (22.2394,  68.9678,  IST),
    "shirdi":      (19.7645,  74.4769,  IST),
    "guwahati":    (26.1445,  91.7362,  IST),
    "bhubaneswar": (20.2961,  85.8245,  IST),
    "ranchi":      (23.3441,  85.3096,  IST),
    "dehradun":    (30.3165,  78.0322,  IST),
    "jammu":       (32.7266,  74.8570,  IST),
    "srinagar":    (34.0837,  74.7973,  IST),
    # Diaspora / common international
    "london":      (51.5074,  -0.1278,  0.0),
    "new york":    (40.7128, -74.0060, -5.0),
    "dubai":       (25.2048,  55.2708,  4.0),
    "singapore":   (1.3521,  103.8198,  8.0),
    "sydney":      (-33.8688, 151.2093, 10.0),
    "toronto":     (43.6532, -79.3832, -5.0),
}


def resolve_location(location: str) -> tuple[float, float, float, str]:
    """
    Returns (lat, lng, tz_offset_hours, matched_city_name).
    Falls back to New Delhi when the string doesn't match.
    """
    if not location:
        lat, lng, tz = CITIES["delhi"]
        return lat, lng, tz, "New Delhi (default)"

    key = location.lower().strip()
    # Try exact first, then substring in the first comma-separated token
    if key in CITIES:
        lat, lng, tz = CITIES[key]
        return lat, lng, tz, key.title()

    first_token = key.split(",")[0].strip()
    for city, coords in CITIES.items():
        if city == first_token or city in first_token or first_token in city:
            lat, lng, tz = coords
            return lat, lng, tz, city.title()

    lat, lng, tz = CITIES["delhi"]
    return lat, lng, tz, f"{location} → New Delhi (not in city database)"


# ============================================================================
# Low-level ephemeris helpers
# ============================================================================

def _julian_day(local_dt: datetime, tz_offset_hours: float) -> float:
    """Local datetime → Julian Day in Universal Time."""
    ut_dt = local_dt - timedelta(hours=tz_offset_hours)
    hour_frac = ut_dt.hour + ut_dt.minute / 60.0 + ut_dt.second / 3600.0
    return swe.julday(ut_dt.year, ut_dt.month, ut_dt.day, hour_frac)


def _jd_to_local(jd: float, tz_offset_hours: float) -> datetime:
    """Julian Day (UT) → local datetime."""
    year, month, day, hour = swe.revjul(jd)
    h = int(hour)
    m_frac = (hour - h) * 60.0
    m = int(m_frac)
    s = int(round((m_frac - m) * 60.0))
    # Guard against s=60 rounding
    if s == 60:
        s = 0; m += 1
    if m == 60:
        m = 0; h += 1
    ut = datetime(year, month, day, h, m, s, tzinfo=timezone.utc)
    return (ut + timedelta(hours=tz_offset_hours)).replace(tzinfo=None)


def _sun_moon_longitudes(jd: float) -> tuple[float, float]:
    """Returns (sun_longitude_degrees, moon_longitude_degrees) at given JD."""
    sun = swe.calc_ut(jd, swe.SUN)[0][0]
    moon = swe.calc_ut(jd, swe.MOON)[0][0]
    return float(sun), float(moon)


def _sunrise_sunset_jd(date: date_cls, lat: float, lng: float, tz_offset: float) -> tuple[float, float]:
    """
    Returns (sunrise_jd_ut, sunset_jd_ut) for the given local date.

    pyswisseph 2.10.x API for rise_trans is:
        rise_trans(tjd_ut, body, rsmi, geopos, atpress=0, attemp=0, horhgt=0, flags=0)
                                 ^^^^  ^^^^^^
                              3rd arg  tuple(lng, lat, alt_m)

    Returns (retflag, (jd,)). retflag < 0 means the body did not rise/set
    in the search window (true polar regions only — never happens for any
    realistic city that this app accepts as a location).
    """
    # Start search a few hours before local midnight UT-equivalent so that
    # the local day's sunrise / sunset are both inside the search window.
    local_midnight = datetime(date.year, date.month, date.day, 0, 0, 0)
    jd_start = _julian_day(local_midnight, tz_offset) - 0.25  # back off 6 hours

    rise_flag = getattr(swe, "CALC_RISE", 1)
    set_flag = getattr(swe, "CALC_SET", 2)
    geopos = (lng, lat, 0.0)  # (longitude_east, latitude_north, altitude_m)

    r_ret = swe.rise_trans(jd_start, swe.SUN, rise_flag, geopos, 0, 0, 0, 0)
    s_ret = swe.rise_trans(jd_start, swe.SUN, set_flag, geopos, 0, 0, 0, 0)

    # pyswisseph 2.x returns (retflag, (jd,)). retflag==0 → success.
    r_flag, r_tup = r_ret[0], r_ret[1]
    s_flag, s_tup = s_ret[0], s_ret[1]
    if r_flag < 0 or s_flag < 0 or not r_tup or not s_tup:
        raise RuntimeError(
            f"Sun rise/set not found for {date.isoformat()} at "
            f"({lat:.2f}, {lng:.2f}) — flags r={r_flag} s={s_flag}"
        )
    return r_tup[0], s_tup[0]


# ============================================================================
# Panchang element calculations
# ============================================================================

def _tithi(sun_long: float, moon_long: float) -> dict:
    """Tithi = (Moon longitude - Sun longitude) / 12°, 1-30."""
    diff = (moon_long - sun_long) % 360.0
    idx = int(diff // 12)  # 0-29
    paksha = "Shukla" if idx < 15 else "Krishna"
    paksha_hi = "शुक्ल पक्ष" if paksha == "Shukla" else "कृष्ण पक्ष"
    name_slot = idx % 15
    if paksha == "Krishna" and name_slot == 14:
        name_en = "Amavasya"
        name_hi = "अमावस्या"
    else:
        name_en = TITHI_NAMES_EN[name_slot]
        name_hi = TITHI_NAMES_HI[name_slot]
    progress_pct = round((diff % 12) / 12 * 100, 1)
    return {
        "number": idx + 1,
        "name": name_en,
        "name_hindi": name_hi,
        "paksha": paksha,
        "paksha_hindi": paksha_hi,
        "progress_percent": progress_pct,
    }


def _nakshatra(moon_long: float) -> dict:
    span = 360.0 / 27  # 13° 20'
    idx = int(moon_long // span)  # 0-26
    pada = int((moon_long % span) // (span / 4)) + 1  # 1-4
    return {
        "number": idx + 1,
        "name": NAKSHATRA_NAMES_EN[idx],
        "name_hindi": NAKSHATRA_NAMES_HI[idx],
        "lord": NAKSHATRA_LORDS[idx],
        "pada": pada,
    }


def _yoga(sun_long: float, moon_long: float) -> dict:
    total = (sun_long + moon_long) % 360.0
    span = 360.0 / 27
    idx = int(total // span)  # 0-26
    return {
        "number": idx + 1,
        "name": YOGA_NAMES_EN[idx],
        "name_hindi": YOGA_NAMES_HI[idx],
    }


def _karana(sun_long: float, moon_long: float) -> dict:
    diff = (moon_long - sun_long) % 360.0
    idx = int(diff // 6)  # 0-59 half-tithis
    if idx in FIXED_KARANAS:
        name = FIXED_KARANAS[idx]
    else:
        # Half-tithis 1..56 cycle through 7 movable karanas 8 times
        movable_idx = (idx - 1) % 7
        name = MOVABLE_KARANAS[movable_idx]
    return {"number": idx + 1, "name": name}


def _rahu_kaal(sunrise: datetime, sunset: datetime, weekday: int) -> dict:
    total = sunset - sunrise
    segment = total / 8
    seg_idx = RAHU_KAAL_SEGMENT[weekday]
    start = sunrise + segment * seg_idx
    end = start + segment
    return {"start": _fmt_time(start), "end": _fmt_time(end)}


def _gulika_kaal(sunrise: datetime, sunset: datetime, weekday: int) -> dict:
    total = sunset - sunrise
    segment = total / 8
    seg_idx = GULIKA_KAAL_SEGMENT[weekday]
    start = sunrise + segment * seg_idx
    end = start + segment
    return {"start": _fmt_time(start), "end": _fmt_time(end)}


def _abhijit_muhurat(sunrise: datetime, sunset: datetime) -> dict:
    midday = sunrise + (sunset - sunrise) / 2
    start = midday - timedelta(minutes=24)
    end = midday + timedelta(minutes=24)
    return {"start": _fmt_time(start), "end": _fmt_time(end)}


def _fmt_time(dt: datetime) -> str:
    """12-hour format with AM/PM, no leading zero."""
    return dt.strftime("%I:%M %p").lstrip("0")


# ============================================================================
# Public entry point
# ============================================================================

def compute_panchang(date_str: Optional[str], location_str: str) -> dict:
    """
    Compute the full Panchang for a given date + location.

    Args:
        date_str: YYYY-MM-DD, or None for today (in the location's local time).
        location_str: free-text city name. Falls back to New Delhi.

    Returns:
        A JSON-serialisable dict with all panchang elements.

    Raises:
        RuntimeError if pyswisseph is not available on the server.
    """
    if not _SWE_OK:
        raise RuntimeError(
            "Astronomical engine not available — install pyswisseph on the backend"
        )

    # Resolve location first so we have the right timezone for "today"
    lat, lng, tz_offset, matched_city = resolve_location(location_str)

    # Date → local date object
    if date_str:
        the_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        # "today" = today in the location's local timezone
        the_date = (datetime.now(timezone.utc) + timedelta(hours=tz_offset)).date()

    # Sunrise / sunset (in Julian Day UT, then convert to local)
    sr_jd, ss_jd = _sunrise_sunset_jd(the_date, lat, lng, tz_offset)
    sunrise_local = _jd_to_local(sr_jd, tz_offset)
    sunset_local = _jd_to_local(ss_jd, tz_offset)

    # Panchang elements are traditionally computed at sunrise
    sun_long, moon_long = _sun_moon_longitudes(sr_jd)

    tithi = _tithi(sun_long, moon_long)
    nakshatra = _nakshatra(moon_long)
    yoga = _yoga(sun_long, moon_long)
    karana = _karana(sun_long, moon_long)

    # Python weekday(): Mon=0 … Sun=6 — matches our constants
    weekday_idx = the_date.weekday()

    rahu = _rahu_kaal(sunrise_local, sunset_local, weekday_idx)
    gulika = _gulika_kaal(sunrise_local, sunset_local, weekday_idx)
    abhijit = _abhijit_muhurat(sunrise_local, sunset_local)

    day_minutes = int((sunset_local - sunrise_local).total_seconds() // 60)

    return {
        "date": the_date.strftime("%d %B %Y"),
        "date_iso": the_date.isoformat(),
        "weekday": WEEKDAYS_EN[weekday_idx],
        "weekday_hindi": WEEKDAYS_HI[weekday_idx],
        "location": matched_city,
        "coordinates": {"lat": round(lat, 4), "lng": round(lng, 4)},
        "tithi": tithi,
        "nakshatra": nakshatra,
        "yoga": yoga,
        "karana": karana,
        "sunrise": _fmt_time(sunrise_local),
        "sunset": _fmt_time(sunset_local),
        "day_duration_minutes": day_minutes,
        "rahu_kaal": rahu,
        "gulika_kaal": gulika,
        "abhijit_muhurat": abhijit,
        "engine": "Swiss Ephemeris (pyswisseph)",
    }
