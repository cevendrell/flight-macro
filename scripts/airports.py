"""
Airport lookup for major European (and heavily-connected partner) airports.

Eurostat's `avia_par_XX` datasets use ICAO codes prefixed with the ISO2 country
code, e.g. `DE_EDDF` = Frankfurt, `ES_LEMD` = Madrid. This table lets us turn
those codes into human-readable city names and coordinates for city-pair signals.

Coverage focuses on the top ~100 airports by international traffic in Europe,
plus major partner hubs. Extend as the pipeline surfaces new codes.
"""
from __future__ import annotations

# ICAO → (city, IATA, country_iso2, lat, lng)
AIRPORTS: dict[str, dict] = {
    # ── Germany ──
    "EDDF": {"city": "Frankfurt",   "iata": "FRA", "country": "DE", "lat": 50.0379, "lng":  8.5622},
    "EDDM": {"city": "Munich",      "iata": "MUC", "country": "DE", "lat": 48.3538, "lng": 11.7861},
    "EDDL": {"city": "Düsseldorf",  "iata": "DUS", "country": "DE", "lat": 51.2895, "lng":  6.7668},
    "EDDB": {"city": "Berlin",      "iata": "BER", "country": "DE", "lat": 52.3667, "lng": 13.5033},
    "EDDH": {"city": "Hamburg",     "iata": "HAM", "country": "DE", "lat": 53.6304, "lng":  9.9882},
    "EDDK": {"city": "Cologne",     "iata": "CGN", "country": "DE", "lat": 50.8659, "lng":  7.1427},
    "EDDS": {"city": "Stuttgart",   "iata": "STR", "country": "DE", "lat": 48.6899, "lng":  9.2220},
    # ── France ──
    "LFPG": {"city": "Paris CDG",   "iata": "CDG", "country": "FR", "lat": 49.0097, "lng":  2.5479},
    "LFPO": {"city": "Paris Orly",  "iata": "ORY", "country": "FR", "lat": 48.7233, "lng":  2.3794},
    "LFMN": {"city": "Nice",        "iata": "NCE", "country": "FR", "lat": 43.6584, "lng":  7.2159},
    "LFLL": {"city": "Lyon",        "iata": "LYS", "country": "FR", "lat": 45.7256, "lng":  5.0811},
    "LFBO": {"city": "Toulouse",    "iata": "TLS", "country": "FR", "lat": 43.6293, "lng":  1.3638},
    "LFMM": {"city": "Marseille",   "iata": "MRS", "country": "FR", "lat": 43.4393, "lng":  5.2214},
    # ── UK & Ireland ──
    "EGLL": {"city": "London LHR",  "iata": "LHR", "country": "GB", "lat": 51.4700, "lng": -0.4543},
    "EGKK": {"city": "London LGW",  "iata": "LGW", "country": "GB", "lat": 51.1481, "lng": -0.1903},
    "EGSS": {"city": "London STN",  "iata": "STN", "country": "GB", "lat": 51.8850, "lng":  0.2350},
    "EGLC": {"city": "London LCY",  "iata": "LCY", "country": "GB", "lat": 51.5053, "lng":  0.0553},
    "EGCC": {"city": "Manchester",  "iata": "MAN", "country": "GB", "lat": 53.3537, "lng": -2.2750},
    "EGPH": {"city": "Edinburgh",   "iata": "EDI", "country": "GB", "lat": 55.9500, "lng": -3.3725},
    "EIDW": {"city": "Dublin",      "iata": "DUB", "country": "IE", "lat": 53.4213, "lng": -6.2701},
    # ── Netherlands, Belgium, Lux ──
    "EHAM": {"city": "Amsterdam",   "iata": "AMS", "country": "NL", "lat": 52.3086, "lng":  4.7639},
    "EBBR": {"city": "Brussels",    "iata": "BRU", "country": "BE", "lat": 50.9014, "lng":  4.4844},
    "ELLX": {"city": "Luxembourg",  "iata": "LUX", "country": "LU", "lat": 49.6266, "lng":  6.2114},
    # ── Iberia ──
    "LEMD": {"city": "Madrid",      "iata": "MAD", "country": "ES", "lat": 40.4720, "lng": -3.5610},
    "LEBL": {"city": "Barcelona",   "iata": "BCN", "country": "ES", "lat": 41.2971, "lng":  2.0785},
    "LEPA": {"city": "Palma",       "iata": "PMI", "country": "ES", "lat": 39.5518, "lng":  2.7388},
    "GCLP": {"city": "Las Palmas",  "iata": "LPA", "country": "ES", "lat": 27.9319, "lng": -15.3866},
    "LEMG": {"city": "Málaga",      "iata": "AGP", "country": "ES", "lat": 36.6749, "lng": -4.4991},
    "LEVC": {"city": "Valencia",    "iata": "VLC", "country": "ES", "lat": 39.4893, "lng": -0.4816},
    "LPPT": {"city": "Lisbon",      "iata": "LIS", "country": "PT", "lat": 38.7742, "lng": -9.1342},
    "LPPR": {"city": "Porto",       "iata": "OPO", "country": "PT", "lat": 41.2481, "lng": -8.6814},
    # ── Nordics ──
    "EKCH": {"city": "Copenhagen",  "iata": "CPH", "country": "DK", "lat": 55.6180, "lng": 12.6560},
    "ESSA": {"city": "Stockholm",   "iata": "ARN", "country": "SE", "lat": 59.6519, "lng": 17.9186},
    "ENGM": {"city": "Oslo",        "iata": "OSL", "country": "NO", "lat": 60.1939, "lng": 11.1004},
    "EFHK": {"city": "Helsinki",    "iata": "HEL", "country": "FI", "lat": 60.3172, "lng": 24.9633},
    "BIKF": {"city": "Reykjavík",   "iata": "KEF", "country": "IS", "lat": 63.9850, "lng": -22.6056},
    # ── Italy ──
    "LIRF": {"city": "Rome FCO",    "iata": "FCO", "country": "IT", "lat": 41.8003, "lng": 12.2389},
    "LIMC": {"city": "Milan MXP",   "iata": "MXP", "country": "IT", "lat": 45.6306, "lng":  8.7231},
    "LIML": {"city": "Milan LIN",   "iata": "LIN", "country": "IT", "lat": 45.4451, "lng":  9.2767},
    "LIPZ": {"city": "Venice",      "iata": "VCE", "country": "IT", "lat": 45.5053, "lng": 12.3519},
    "LIRN": {"city": "Naples",      "iata": "NAP", "country": "IT", "lat": 40.8860, "lng": 14.2908},
    "LICJ": {"city": "Palermo",     "iata": "PMO", "country": "IT", "lat": 38.1810, "lng": 13.0910},
    "LICC": {"city": "Catania",     "iata": "CTA", "country": "IT", "lat": 37.4667, "lng": 15.0664},
    # ── Central & Eastern EU ──
    "LKPR": {"city": "Prague",      "iata": "PRG", "country": "CZ", "lat": 50.1008, "lng": 14.2600},
    "LOWW": {"city": "Vienna",      "iata": "VIE", "country": "AT", "lat": 48.1103, "lng": 16.5697},
    "LZIB": {"city": "Bratislava",  "iata": "BTS", "country": "SK", "lat": 48.1702, "lng": 17.2127},
    "LHBP": {"city": "Budapest",    "iata": "BUD", "country": "HU", "lat": 47.4394, "lng": 19.2611},
    "EPWA": {"city": "Warsaw",      "iata": "WAW", "country": "PL", "lat": 52.1657, "lng": 20.9671},
    "EPKK": {"city": "Kraków",      "iata": "KRK", "country": "PL", "lat": 50.0777, "lng": 19.7848},
    "EPGD": {"city": "Gdańsk",      "iata": "GDN", "country": "PL", "lat": 54.3776, "lng": 18.4662},
    "LROP": {"city": "Bucharest",   "iata": "OTP", "country": "RO", "lat": 44.5711, "lng": 26.0850},
    "LBSF": {"city": "Sofia",       "iata": "SOF", "country": "BG", "lat": 42.6952, "lng": 23.4114},
    "LGAV": {"city": "Athens",      "iata": "ATH", "country": "EL", "lat": 37.9364, "lng": 23.9445},
    "LGTS": {"city": "Thessaloniki","iata": "SKG", "country": "EL", "lat": 40.5197, "lng": 22.9709},
    # ── Balkans / SE ──
    "LDZA": {"city": "Zagreb",      "iata": "ZAG", "country": "HR", "lat": 45.7429, "lng": 16.0688},
    "LDSP": {"city": "Split",       "iata": "SPU", "country": "HR", "lat": 43.5389, "lng": 16.2980},
    "LJLJ": {"city": "Ljubljana",   "iata": "LJU", "country": "SI", "lat": 46.2237, "lng": 14.4576},
    "LYBE": {"city": "Belgrade",    "iata": "BEG", "country": "RS", "lat": 44.8184, "lng": 20.3091},
    # ── Baltics ──
    "EETN": {"city": "Tallinn",     "iata": "TLL", "country": "EE", "lat": 59.4133, "lng": 24.8328},
    "EVRA": {"city": "Riga",        "iata": "RIX", "country": "LV", "lat": 56.9236, "lng": 23.9711},
    "EYVI": {"city": "Vilnius",     "iata": "VNO", "country": "LT", "lat": 54.6341, "lng": 25.2858},
    # ── Türkiye ──
    "LTFM": {"city": "Istanbul IST","iata": "IST", "country": "TR", "lat": 41.2611, "lng": 28.7419},
    "LTFJ": {"city": "Istanbul SAW","iata": "SAW", "country": "TR", "lat": 40.8986, "lng": 29.3092},
    "LTAI": {"city": "Antalya",     "iata": "AYT", "country": "TR", "lat": 36.8987, "lng": 30.8005},
    # ── Switzerland ──
    "LSZH": {"city": "Zürich",      "iata": "ZRH", "country": "CH", "lat": 47.4647, "lng":  8.5492},
    "LSGG": {"city": "Geneva",      "iata": "GVA", "country": "CH", "lat": 46.2381, "lng":  6.1090},
    # ── Malta, Cyprus ──
    "LMML": {"city": "Malta",       "iata": "MLA", "country": "MT", "lat": 35.8575, "lng": 14.4775},
    "LCLK": {"city": "Larnaca",     "iata": "LCA", "country": "CY", "lat": 34.8751, "lng": 33.6249},
    # ── Major worldwide hubs partner airports appear as ──
    "KJFK": {"city": "New York JFK","iata": "JFK", "country": "US", "lat": 40.6413, "lng": -73.7781},
    "KEWR": {"city": "Newark",      "iata": "EWR", "country": "US", "lat": 40.6895, "lng": -74.1745},
    "KLAX": {"city": "Los Angeles", "iata": "LAX", "country": "US", "lat": 33.9416, "lng": -118.4085},
    "KORD": {"city": "Chicago",     "iata": "ORD", "country": "US", "lat": 41.9742, "lng": -87.9073},
    "KMIA": {"city": "Miami",       "iata": "MIA", "country": "US", "lat": 25.7959, "lng": -80.2870},
    "OMDB": {"city": "Dubai",       "iata": "DXB", "country": "AE", "lat": 25.2532, "lng": 55.3657},
    "OTHH": {"city": "Doha",        "iata": "DOH", "country": "QA", "lat": 25.2609, "lng": 51.6138},
    "OERK": {"city": "Riyadh",      "iata": "RUH", "country": "SA", "lat": 24.9576, "lng": 46.6988},
    "OEJN": {"city": "Jeddah",      "iata": "JED", "country": "SA", "lat": 21.6796, "lng": 39.1565},
    "ZBAA": {"city": "Beijing",     "iata": "PEK", "country": "CN", "lat": 40.0801, "lng": 116.5846},
    "ZSPD": {"city": "Shanghai",    "iata": "PVG", "country": "CN", "lat": 31.1443, "lng": 121.8083},
    "VHHH": {"city": "Hong Kong",   "iata": "HKG", "country": "HK", "lat": 22.3080, "lng": 113.9185},
    "RJTT": {"city": "Tokyo HND",   "iata": "HND", "country": "JP", "lat": 35.5494, "lng": 139.7798},
    "RJAA": {"city": "Tokyo NRT",   "iata": "NRT", "country": "JP", "lat": 35.7720, "lng": 140.3929},
    "RKSI": {"city": "Seoul ICN",   "iata": "ICN", "country": "KR", "lat": 37.4602, "lng": 126.4407},
    "WSSS": {"city": "Singapore",   "iata": "SIN", "country": "SG", "lat": 1.3644,  "lng": 103.9915},
    "VIDP": {"city": "Delhi",       "iata": "DEL", "country": "IN", "lat": 28.5562, "lng": 77.1000},
    "VABB": {"city": "Mumbai",      "iata": "BOM", "country": "IN", "lat": 19.0896, "lng": 72.8656},
    "YSSY": {"city": "Sydney",      "iata": "SYD", "country": "AU", "lat": -33.9399,"lng": 151.1753},
    "SBGR": {"city": "São Paulo",   "iata": "GRU", "country": "BR", "lat": -23.4356,"lng": -46.4731},
}


def lookup_icao(icao: str) -> dict | None:
    if not icao:
        return None
    return AIRPORTS.get(icao.upper())
