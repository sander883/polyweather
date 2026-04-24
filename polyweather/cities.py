from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    code: str
    name: str
    station: str
    latitude: float
    longitude: float
    timezone: str


CITIES: dict[str, City] = {
    "NYC": City("NYC", "New York", "JFK", 40.6413, -73.7781, "America/New_York"),
    "LAX": City("LAX", "Los Angeles", "LAX", 33.9416, -118.4085, "America/Los_Angeles"),
    "ORD": City("ORD", "Chicago", "ORD", 41.9742, -87.9073, "America/Chicago"),
}


def get_city(code: str) -> City:
    key = code.upper()
    if key not in CITIES:
        raise KeyError(f"Unknown city code: {code!r}. Known: {sorted(CITIES)}")
    return CITIES[key]
