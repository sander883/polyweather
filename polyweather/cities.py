from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    code: str
    name: str
    station: str         # ICAO station that Polymarket resolves on
    latitude: float
    longitude: float
    timezone: str
    region: str          # us | eu | asia | ca | sa | oc — used to pick HRRR vs ECMWF


# Coordinates target the airport station that Polymarket weather markets
# resolve on (matches alteregoeth/weatherbot). City-center coords would shift
# the forecast 3-8°F off the resolution point on edge buckets.
CITIES: dict[str, City] = {
    # ── United States (HRRR-eligible, °F) ─────────────────────────────────
    "NYC": City("NYC", "New York",   "KLGA", 40.7772,  -73.8726, "America/New_York",   "us"),
    "LAX": City("LAX", "Los Angeles","KLAX", 33.9416, -118.4085, "America/Los_Angeles","us"),
    "ORD": City("ORD", "Chicago",    "KORD", 41.9742,  -87.9073, "America/Chicago",    "us"),
    "MIA": City("MIA", "Miami",      "KMIA", 25.7959,  -80.2870, "America/New_York",   "us"),
    "DAL": City("DAL", "Dallas",     "KDAL", 32.8471,  -96.8518, "America/Chicago",    "us"),
    "SEA": City("SEA", "Seattle",    "KSEA", 47.4502, -122.3088, "America/Los_Angeles","us"),
    "ATL": City("ATL", "Atlanta",    "KATL", 33.6407,  -84.4277, "America/New_York",   "us"),

    # ── Europe (ECMWF only) ───────────────────────────────────────────────
    "LON": City("LON", "London",     "EGLC", 51.5048,    0.0495, "Europe/London",      "eu"),
    "PAR": City("PAR", "Paris",      "LFPG", 48.9962,    2.5979, "Europe/Paris",       "eu"),
    "MUC": City("MUC", "Munich",     "EDDM", 48.3537,   11.7750, "Europe/Berlin",      "eu"),
    "ANK": City("ANK", "Ankara",     "LTAC", 40.1281,   32.9951, "Europe/Istanbul",    "eu"),

    # ── Asia (ECMWF only) ─────────────────────────────────────────────────
    "SEL": City("SEL", "Seoul",      "RKSI", 37.4691,  126.4505, "Asia/Seoul",         "asia"),
    "TYO": City("TYO", "Tokyo",      "RJTT", 35.7647,  140.3864, "Asia/Tokyo",         "asia"),
    "SHA": City("SHA", "Shanghai",   "ZSPD", 31.1443,  121.8083, "Asia/Shanghai",      "asia"),
    "SIN": City("SIN", "Singapore",  "WSSS",  1.3502,  103.9940, "Asia/Singapore",     "asia"),
    "LKO": City("LKO", "Lucknow",    "VILK", 26.7606,   80.8893, "Asia/Kolkata",       "asia"),
    "TLV": City("TLV", "Tel Aviv",   "LLBG", 32.0114,   34.8867, "Asia/Jerusalem",     "asia"),

    # ── Other regions ─────────────────────────────────────────────────────
    "YYZ": City("YYZ", "Toronto",       "CYYZ", 43.6772,  -79.6306, "America/Toronto",                   "ca"),
    "GRU": City("GRU", "Sao Paulo",     "SBGR", -23.4356, -46.4731, "America/Sao_Paulo",                 "sa"),
    "EZE": City("EZE", "Buenos Aires",  "SAEZ", -34.8222, -58.5358, "America/Argentina/Buenos_Aires",    "sa"),
    "WLG": City("WLG", "Wellington",    "NZWN", -41.3272, 174.8052, "Pacific/Auckland",                  "oc"),
}


def get_city(code: str) -> City:
    key = code.upper()
    if key not in CITIES:
        raise KeyError(f"Unknown city code: {code!r}. Known: {sorted(CITIES)}")
    return CITIES[key]
