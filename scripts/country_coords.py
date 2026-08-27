"""
ISO 3166-1 alpha-2 → country name + approximate centroid.

Kept small on purpose: coverage focuses on Europe (Eurostat reporting countries),
plus major worldwide partners that show up in flight corridors. Extend as needed.
"""

COUNTRIES: dict[str, dict] = {
    # EU reporting countries
    "AT": {"name": "Austria",        "lat": 47.52,  "lng": 14.55},
    "BE": {"name": "Belgium",        "lat": 50.50,  "lng": 4.47},
    "BG": {"name": "Bulgaria",       "lat": 42.73,  "lng": 25.49},
    "CY": {"name": "Cyprus",         "lat": 35.13,  "lng": 33.43},
    "CZ": {"name": "Czechia",        "lat": 49.82,  "lng": 15.47},
    "DE": {"name": "Germany",        "lat": 51.17,  "lng": 10.45},
    "DK": {"name": "Denmark",        "lat": 56.26,  "lng": 9.50},
    "EE": {"name": "Estonia",        "lat": 58.60,  "lng": 25.01},
    "EL": {"name": "Greece",         "lat": 39.07,  "lng": 21.82},  # Eurostat uses EL
    "GR": {"name": "Greece",         "lat": 39.07,  "lng": 21.82},
    "ES": {"name": "Spain",          "lat": 40.46,  "lng": -3.75},
    "FI": {"name": "Finland",        "lat": 61.92,  "lng": 25.75},
    "FR": {"name": "France",         "lat": 46.23,  "lng": 2.21},
    "HR": {"name": "Croatia",        "lat": 45.10,  "lng": 15.20},
    "HU": {"name": "Hungary",        "lat": 47.16,  "lng": 19.50},
    "IE": {"name": "Ireland",        "lat": 53.14,  "lng": -7.69},
    "IT": {"name": "Italy",          "lat": 41.87,  "lng": 12.57},
    "LT": {"name": "Lithuania",      "lat": 55.17,  "lng": 23.88},
    "LU": {"name": "Luxembourg",     "lat": 49.82,  "lng": 6.13},
    "LV": {"name": "Latvia",         "lat": 56.88,  "lng": 24.60},
    "MT": {"name": "Malta",          "lat": 35.94,  "lng": 14.38},
    "NL": {"name": "Netherlands",    "lat": 52.13,  "lng": 5.29},
    "PL": {"name": "Poland",         "lat": 51.92,  "lng": 19.15},
    "PT": {"name": "Portugal",       "lat": 39.40,  "lng": -8.22},
    "RO": {"name": "Romania",        "lat": 45.94,  "lng": 24.97},
    "SE": {"name": "Sweden",         "lat": 60.13,  "lng": 18.64},
    "SI": {"name": "Slovenia",       "lat": 46.15,  "lng": 14.99},
    "SK": {"name": "Slovakia",       "lat": 48.67,  "lng": 19.70},
    # EFTA + non-EU EEA
    "CH": {"name": "Switzerland",    "lat": 46.82,  "lng": 8.23},
    "IS": {"name": "Iceland",        "lat": 64.96,  "lng": -19.02},
    "LI": {"name": "Liechtenstein",  "lat": 47.17,  "lng": 9.56},
    "NO": {"name": "Norway",         "lat": 60.47,  "lng": 8.47},
    # UK + others in Europe
    "GB": {"name": "United Kingdom", "lat": 55.38,  "lng": -3.44},
    "UK": {"name": "United Kingdom", "lat": 55.38,  "lng": -3.44},
    "AL": {"name": "Albania",        "lat": 41.15,  "lng": 20.17},
    "BA": {"name": "Bosnia & Herz.", "lat": 43.92,  "lng": 17.68},
    "ME": {"name": "Montenegro",     "lat": 42.71,  "lng": 19.37},
    "MK": {"name": "North Macedonia","lat": 41.61,  "lng": 21.75},
    "RS": {"name": "Serbia",         "lat": 44.02,  "lng": 21.01},
    "TR": {"name": "Türkiye",        "lat": 38.96,  "lng": 35.24},
    "UA": {"name": "Ukraine",        "lat": 48.38,  "lng": 31.17},
    "MD": {"name": "Moldova",        "lat": 47.41,  "lng": 28.37},
    "XK": {"name": "Kosovo",         "lat": 42.60,  "lng": 20.90},
    # Major global partners
    "US": {"name": "United States",  "lat": 39.82,  "lng": -98.58},
    "CA": {"name": "Canada",         "lat": 56.13,  "lng": -106.35},
    "MX": {"name": "Mexico",         "lat": 23.63,  "lng": -102.55},
    "BR": {"name": "Brazil",         "lat": -14.24, "lng": -51.93},
    "AR": {"name": "Argentina",      "lat": -38.42, "lng": -63.62},
    "CN": {"name": "China",          "lat": 35.86,  "lng": 104.20},
    "JP": {"name": "Japan",          "lat": 36.20,  "lng": 138.25},
    "KR": {"name": "South Korea",    "lat": 35.91,  "lng": 127.77},
    "IN": {"name": "India",          "lat": 20.59,  "lng": 78.96},
    "TH": {"name": "Thailand",       "lat": 15.87,  "lng": 100.99},
    "SG": {"name": "Singapore",      "lat": 1.35,   "lng": 103.82},
    "HK": {"name": "Hong Kong",      "lat": 22.30,  "lng": 114.17},
    "VN": {"name": "Vietnam",        "lat": 14.06,  "lng": 108.28},
    "ID": {"name": "Indonesia",      "lat": -0.79,  "lng": 113.92},
    "PH": {"name": "Philippines",    "lat": 12.88,  "lng": 121.77},
    "AU": {"name": "Australia",      "lat": -25.27, "lng": 133.77},
    "NZ": {"name": "New Zealand",    "lat": -40.90, "lng": 174.89},
    "AE": {"name": "UAE",            "lat": 23.42,  "lng": 53.85},
    "SA": {"name": "Saudi Arabia",   "lat": 23.89,  "lng": 45.08},
    "QA": {"name": "Qatar",          "lat": 25.35,  "lng": 51.18},
    "IL": {"name": "Israel",         "lat": 31.05,  "lng": 34.85},
    "EG": {"name": "Egypt",          "lat": 26.82,  "lng": 30.80},
    "MA": {"name": "Morocco",        "lat": 31.79,  "lng": -7.09},
    "TN": {"name": "Tunisia",        "lat": 33.89,  "lng": 9.54},
    "ZA": {"name": "South Africa",   "lat": -30.56, "lng": 22.94},
    "NG": {"name": "Nigeria",        "lat": 9.08,   "lng": 8.68},
    "KE": {"name": "Kenya",          "lat": -0.02,  "lng": 37.91},
    "RU": {"name": "Russia",         "lat": 61.52,  "lng": 105.32},
    "GE": {"name": "Georgia",        "lat": 42.32,  "lng": 43.36},
    "AM": {"name": "Armenia",        "lat": 40.07,  "lng": 45.04},
    "AZ": {"name": "Azerbaijan",     "lat": 40.14,  "lng": 47.58},
    "KZ": {"name": "Kazakhstan",     "lat": 48.02,  "lng": 66.92},
    "UZ": {"name": "Uzbekistan",     "lat": 41.38,  "lng": 64.59},
}


def lookup(code: str) -> dict | None:
    if not code:
        return None
    return COUNTRIES.get(code.upper())
