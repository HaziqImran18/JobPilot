"""Structured location, workplace and remote-eligibility extraction.

A job posting mixes up to four different places:

* where the employer is headquartered (``company_location``);
* where the work is advertised (``job_location``);
* where a remote employee is allowed to live (``eligible_locations``);
* the place the candidate actually asked about.

Keeping those separate is what makes "remote jobs in Toronto" return roles a
Toronto-based candidate may do, instead of roles whose office happens to be in
Toronto. The helpers here are deterministic and evidence-based: when a posting
does not state its remote scope we report ``unknown`` rather than assuming it is
available worldwide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobpilot.core.utils import normalize_text

REMOTE_WORDS = ("remote", "anywhere", "worldwide", "work from home", "wfh", "distributed")
REMOTE_ONLY_TERMS = {"remote", "anywhere", "worldwide", "work from home", "wfh", "online"}


@dataclass(frozen=True)
class Place:
    city: str
    region: str
    country: str

    @property
    def label(self) -> str:
        return ", ".join(part for part in (self.city, self.region, self.country) if part)


@dataclass(frozen=True)
class ResolvedLocation:
    """A user location normalised to city / region / country."""

    original: str = ""
    kind: str = "unknown"  # city | region | country | remote | unknown
    city: str = ""
    region: str = ""
    country: str = ""
    remote_only: bool = False

    @property
    def known(self) -> bool:
        return self.kind in {"city", "region", "country"} and bool(
            self.city or self.region or self.country
        )

    @property
    def display(self) -> str:
        if self.remote_only:
            return "Remote"
        if self.city:
            return ", ".join(part for part in (self.city, self.country) if part)
        if self.region:
            return ", ".join(part for part in (self.region, self.country) if part)
        if self.country:
            return self.country
        return self.original

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "city": self.city,
            "region": self.region,
            "country": self.country,
            "display": self.display,
            "remote_only": self.remote_only,
        }


# Country aliases. Two-letter codes are only used when they are unambiguous
# enough for a short location string; "in" (India) is deliberately omitted.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "Pakistan": ("pakistan", "pk", "pak"),
    "Canada": ("canada", "ca"),
    "United Kingdom": ("united kingdom", "uk", "gb", "great britain", "britain", "england"),
    "United States": ("united states", "usa", "us", "u.s.", "united states of america"),
    "United Arab Emirates": ("united arab emirates", "uae", "emirates"),
    "Saudi Arabia": ("saudi arabia", "ksa", "saudi"),
    "Qatar": ("qatar",),
    "Kuwait": ("kuwait",),
    "Australia": ("australia", "au"),
    "India": ("india",),
    "Singapore": ("singapore",),
    "Germany": ("germany", "deutschland"),
    "France": ("france",),
    "Netherlands": ("netherlands", "holland"),
    "Ireland": ("ireland",),
    "Spain": ("spain",),
    "Turkey": ("turkey", "turkiye"),
    "Malaysia": ("malaysia",),
    "China": ("china",),
    "Japan": ("japan",),
    "Brazil": ("brazil",),
    "South Africa": ("south africa",),
    "New Zealand": ("new zealand",),
}

_REGION_ALIASES: dict[str, tuple[str, str]] = {
    "punjab": ("Punjab", "Pakistan"),
    "sindh": ("Sindh", "Pakistan"),
    "khyber pakhtunkhwa": ("Khyber Pakhtunkhwa", "Pakistan"),
    "kpk": ("Khyber Pakhtunkhwa", "Pakistan"),
    "balochistan": ("Balochistan", "Pakistan"),
    "islamabad capital territory": ("Islamabad Capital Territory", "Pakistan"),
    "ict": ("Islamabad Capital Territory", "Pakistan"),
    "azad kashmir": ("Azad Kashmir", "Pakistan"),
    "gilgit baltistan": ("Gilgit-Baltistan", "Pakistan"),
    "ontario": ("Ontario", "Canada"),
    "quebec": ("Quebec", "Canada"),
    "british columbia": ("British Columbia", "Canada"),
    "alberta": ("Alberta", "Canada"),
    "manitoba": ("Manitoba", "Canada"),
    "nova scotia": ("Nova Scotia", "Canada"),
    "england": ("England", "United Kingdom"),
    "scotland": ("Scotland", "United Kingdom"),
    "wales": ("Wales", "United Kingdom"),
    "northern ireland": ("Northern Ireland", "United Kingdom"),
    "california": ("California", "United States"),
    "new york state": ("New York", "United States"),
    "texas": ("Texas", "United States"),
    "washington": ("Washington", "United States"),
    "massachusetts": ("Massachusetts", "United States"),
    "illinois": ("Illinois", "United States"),
    "florida": ("Florida", "United States"),
    "georgia": ("Georgia", "United States"),
    "colorado": ("Colorado", "United States"),
    "new south wales": ("New South Wales", "Australia"),
    "victoria": ("Victoria", "Australia"),
    "queensland": ("Queensland", "Australia"),
    "western australia": ("Western Australia", "Australia"),
    "karnataka": ("Karnataka", "India"),
    "maharashtra": ("Maharashtra", "India"),
    "telangana": ("Telangana", "India"),
    "tamil nadu": ("Tamil Nadu", "India"),
    "dubai": ("Dubai", "United Arab Emirates"),
    "abu dhabi": ("Abu Dhabi", "United Arab Emirates"),
    "riyadh": ("Riyadh", "Saudi Arabia"),
    "makkah": ("Makkah", "Saudi Arabia"),
}

# Upper-case administrative codes at the end of a location. Postal abbreviations
# are ambiguous in both directions ("CA" is California and Canada, "IN" is India
# and Indiana), so each code keeps every plausible reading and the city name
# decides. A code must never silently contradict the city it is attached to.
_ADMIN_CODE_HINTS: dict[str, tuple[tuple[str, str], ...]] = {
    # Countries
    "PK": (("", "Pakistan"),),
    "PAK": (("", "Pakistan"),),
    "IN": (("", "India"),),
    "IND": (("", "India"),),
    "US": (("", "United States"),),
    "USA": (("", "United States"),),
    "CA": (("California", "United States"), ("", "Canada")),
    "CAN": (("", "Canada"),),
    "UK": (("", "United Kingdom"),),
    "GB": (("", "United Kingdom"),),
    "GBR": (("", "United Kingdom"),),
    "AE": (("", "United Arab Emirates"),),
    "UAE": (("", "United Arab Emirates"),),
    "SA": (("", "Saudi Arabia"), ("South Australia", "Australia")),
    "KSA": (("", "Saudi Arabia"),),
    "QA": (("", "Qatar"),),
    "KW": (("", "Kuwait"),),
    "AU": (("", "Australia"),),
    "AUS": (("", "Australia"),),
    "FR": (("", "France"),),
    "FRA": (("", "France"),),
    "DEU": (("", "Germany"),),
    "NLD": (("", "Netherlands"),),
    "IE": (("", "Ireland"),),
    "ES": (("", "Spain"),),
    "TR": (("", "Turkey"),),
    "MY": (("", "Malaysia"),),
    "SG": (("", "Singapore"),),
    "CN": (("", "China"),),
    "JP": (("", "Japan"),),
    "BR": (("", "Brazil"),),
    "ZA": (("", "South Africa"),),
    "NZ": (("", "New Zealand"),),
    # US states
    "AL": (("Alabama", "United States"),),
    "AK": (("Alaska", "United States"),),
    "AZ": (("Arizona", "United States"),),
    "AR": (("Arkansas", "United States"),),
    "CO": (("Colorado", "United States"),),
    "CT": (("Connecticut", "United States"),),
    "DE": (("Delaware", "United States"), ("", "Germany")),
    "FL": (("Florida", "United States"),),    "GA": (("Georgia", "United States"),),
    "HI": (("Hawaii", "United States"),),
    "ID": (("Idaho", "United States"),),
    "IL": (("Illinois", "United States"),),
    "IA": (("Iowa", "United States"),),
    "KS": (("Kansas", "United States"),),
    "KY": (("Kentucky", "United States"),),
    "LA": (("Louisiana", "United States"),),
    "ME": (("Maine", "United States"),),
    "MD": (("Maryland", "United States"),),
    "MA": (("Massachusetts", "United States"),),
    "MI": (("Michigan", "United States"),),
    "MN": (("Minnesota", "United States"),),
    "MS": (("Mississippi", "United States"),),
    "MO": (("Missouri", "United States"),),
    "MT": (("Montana", "United States"),),
    "NE": (("Nebraska", "United States"),),
    "NV": (("Nevada", "United States"),),
    "NH": (("New Hampshire", "United States"),),
    "NJ": (("New Jersey", "United States"),),
    "NM": (("New Mexico", "United States"),),
    "NY": (("New York", "United States"),),
    "NC": (("North Carolina", "United States"),),
    "ND": (("North Dakota", "United States"),),
    "OH": (("Ohio", "United States"),),
    "OK": (("Oklahoma", "United States"),),
    "OR": (("Oregon", "United States"),),
    "PA": (("Pennsylvania", "United States"),),
    "RI": (("Rhode Island", "United States"),),
    "SC": (("South Carolina", "United States"),),
    "SD": (("South Dakota", "United States"),),
    "TN": (("Tennessee", "United States"),),
    "TX": (("Texas", "United States"),),
    "UT": (("Utah", "United States"),),
    "VT": (("Vermont", "United States"),),
    "VA": (("Virginia", "United States"),),
    "WA": (("Washington", "United States"), ("Western Australia", "Australia")),
    "WV": (("West Virginia", "United States"),),
    "WI": (("Wisconsin", "United States"),),
    "WY": (("Wyoming", "United States"),),
    "DC": (("District of Columbia", "United States"),),
    # Canadian provinces
    "ON": (("Ontario", "Canada"),),
    "QC": (("Quebec", "Canada"),),
    "BC": (("British Columbia", "Canada"),),
    "AB": (("Alberta", "Canada"),),
    "MB": (("Manitoba", "Canada"),),
    "NS": (("Nova Scotia", "Canada"),),
    "NB": (("New Brunswick", "Canada"),),
    "PE": (("Prince Edward Island", "Canada"),),
    "SK": (("Saskatchewan", "Canada"),),
    "NL": (("Newfoundland", "Canada"), ("", "Netherlands")),
    # Australian states
    "NSW": (("New South Wales", "Australia"),),
    "VIC": (("Victoria", "Australia"),),
    "QLD": (("Queensland", "Australia"),),
    "TAS": (("Tasmania", "Australia"),),
    "ACT": (("Australian Capital Territory", "Australia"),),
}

# (country, region, city, aliases)
_CITIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # Pakistan
    ("Pakistan", "Punjab", "Lahore", ("lahore", "lāhore")),
    ("Pakistan", "Sindh", "Karachi", ("karachi", "karāchi")),
    ("Pakistan", "Islamabad Capital Territory", "Islamabad", ("islamabad",)),
    ("Pakistan", "Punjab", "Rawalpindi", ("rawalpindi", "pindi")),
    ("Pakistan", "Punjab", "Faisalabad", ("faisalabad", "lyallpur")),
    ("Pakistan", "Punjab", "Multan", ("multan",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Peshawar", ("peshawar",)),
    ("Pakistan", "Balochistan", "Quetta", ("quetta",)),
    ("Pakistan", "Sindh", "Hyderabad", ("hyderabad",)),
    ("Pakistan", "Punjab", "Gujranwala", ("gujranwala",)),
    ("Pakistan", "Punjab", "Sialkot", ("sialkot",)),
    ("Pakistan", "Punjab", "Bahawalpur", ("bahawalpur",)),
    ("Pakistan", "Punjab", "Sargodha", ("sargodha",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Abbottabad", ("abbottabad",)),
    ("Pakistan", "Sindh", "Sukkur", ("sukkur",)),
    ("Pakistan", "Sindh", "Larkana", ("larkana",)),
    ("Pakistan", "Punjab", "Jhelum", ("jhelum",)),
    ("Pakistan", "Punjab", "Okara", ("okara",)),
    ("Pakistan", "Punjab", "Sahiwal", ("sahiwal",)),
    ("Pakistan", "Punjab", "Wah Cantt", ("wah cantt", "wah")),
    ("Pakistan", "Punjab", "Taxila", ("taxila",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Mardan", ("mardan",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Nowshera", ("nowshera",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Charsadda", ("charsadda",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Dera Ismail Khan", ("dera ismail khan", "d.i. khan")),
    ("Pakistan", "Punjab", "Dera Ghazi Khan", ("dera ghazi khan", "d.g. khan")),
    ("Pakistan", "Punjab", "Rahim Yar Khan", ("rahim yar khan",)),
    ("Pakistan", "Punjab", "Jhang", ("jhang",)),
    ("Pakistan", "Punjab", "Sheikhupura", ("sheikhupura",)),
    ("Pakistan", "Punjab", "Kasur", ("kasur",)),
    ("Pakistan", "Punjab", "Gujrat", ("gujrat",)),
    ("Pakistan", "Punjab", "Mandi Bahauddin", ("mandi bahauddin",)),
    ("Pakistan", "Punjab", "Chiniot", ("chiniot",)),
    ("Pakistan", "Punjab", "Muzaffargarh", ("muzaffargarh",)),
    ("Pakistan", "Punjab", "Attock", ("attock",)),
    ("Pakistan", "Punjab", "Hafizabad", ("hafizabad",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Bannu", ("bannu",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Kohat", ("kohat",)),
    ("Pakistan", "Khyber Pakhtunkhwa", "Swabi", ("swabi",)),
    ("Pakistan", "Gilgit-Baltistan", "Gilgit", ("gilgit",)),
    ("Pakistan", "Gilgit-Baltistan", "Skardu", ("skardu",)),
    ("Pakistan", "Azad Kashmir", "Muzaffarabad", ("muzaffarabad",)),
    ("Pakistan", "Azad Kashmir", "Mirpur", ("mirpur",)),
    ("Pakistan", "Azad Kashmir", "Kotli", ("kotli",)),
    ("Pakistan", "Balochistan", "Gwadar", ("gwadar",)),
    # Canada
    ("Canada", "Ontario", "Toronto", ("toronto",)),
    ("Canada", "Ontario", "Ottawa", ("ottawa",)),
    ("Canada", "Ontario", "Mississauga", ("mississauga",)),
    ("Canada", "Ontario", "Waterloo", ("waterloo",)),
    ("Canada", "Ontario", "Kitchener", ("kitchener",)),
    ("Canada", "Ontario", "Hamilton", ("hamilton",)),
    ("Canada", "Quebec", "Montreal", ("montreal", "montréal")),
    ("Canada", "British Columbia", "Vancouver", ("vancouver",)),
    ("Canada", "British Columbia", "Victoria", ("victoria",)),
    ("Canada", "Alberta", "Calgary", ("calgary",)),
    ("Canada", "Alberta", "Edmonton", ("edmonton",)),
    ("Canada", "Manitoba", "Winnipeg", ("winnipeg",)),
    ("Canada", "Nova Scotia", "Halifax", ("halifax",)),
    # United Kingdom
    ("United Kingdom", "England", "London", ("london",)),
    ("United Kingdom", "England", "Manchester", ("manchester",)),
    ("United Kingdom", "England", "Birmingham", ("birmingham",)),
    ("United Kingdom", "England", "Leeds", ("leeds",)),
    ("United Kingdom", "England", "Bristol", ("bristol",)),
    ("United Kingdom", "England", "Cambridge", ("cambridge",)),
    ("United Kingdom", "England", "Oxford", ("oxford",)),
    ("United Kingdom", "England", "Newcastle", ("newcastle",)),
    ("United Kingdom", "England", "Sheffield", ("sheffield",)),
    ("United Kingdom", "England", "Nottingham", ("nottingham",)),
    ("United Kingdom", "Scotland", "Edinburgh", ("edinburgh",)),
    ("United Kingdom", "Scotland", "Glasgow", ("glasgow",)),
    ("United Kingdom", "Wales", "Cardiff", ("cardiff",)),
    ("United Kingdom", "Northern Ireland", "Belfast", ("belfast",)),
    # United States
    ("United States", "New York", "New York", ("new york", "nyc", "new york city")),
    ("United States", "California", "San Francisco", ("san francisco", "sf")),
    ("United States", "California", "Los Angeles", ("los angeles", "la")),
    ("United States", "California", "San Diego", ("san diego",)),
    ("United States", "California", "San Jose", ("san jose",)),
    ("United States", "California", "Palo Alto", ("palo alto",)),
    ("United States", "California", "Mountain View", ("mountain view",)),
    ("United States", "Washington", "Seattle", ("seattle",)),
    ("United States", "Texas", "Austin", ("austin",)),
    ("United States", "Texas", "Dallas", ("dallas",)),
    ("United States", "Texas", "Houston", ("houston",)),
    ("United States", "Massachusetts", "Boston", ("boston",)),
    ("United States", "Illinois", "Chicago", ("chicago",)),
    ("United States", "Georgia", "Atlanta", ("atlanta",)),
    ("United States", "Colorado", "Denver", ("denver",)),
    ("United States", "Florida", "Miami", ("miami",)),
    ("United States", "District of Columbia", "Washington", ("washington dc", "washington, d.c.")),
    ("United States", "Arizona", "Phoenix", ("phoenix",)),
    ("United States", "Pennsylvania", "Philadelphia", ("philadelphia",)),
    # Middle East
    ("United Arab Emirates", "Dubai", "Dubai", ("dubai",)),
    ("United Arab Emirates", "Abu Dhabi", "Abu Dhabi", ("abu dhabi",)),
    ("United Arab Emirates", "Sharjah", "Sharjah", ("sharjah",)),
    ("Saudi Arabia", "Riyadh", "Riyadh", ("riyadh",)),
    ("Saudi Arabia", "Makkah", "Jeddah", ("jeddah",)),
    ("Saudi Arabia", "Eastern Province", "Dammam", ("dammam",)),
    ("Qatar", "Doha", "Doha", ("doha",)),
    ("Kuwait", "Al Asimah", "Kuwait City", ("kuwait city",)),
    # Asia-Pacific
    ("Australia", "New South Wales", "Sydney", ("sydney",)),
    ("Australia", "Victoria", "Melbourne", ("melbourne",)),
    ("Australia", "Queensland", "Brisbane", ("brisbane",)),
    ("Australia", "Western Australia", "Perth", ("perth",)),
    ("Australia", "South Australia", "Adelaide", ("adelaide",)),
    ("Australia", "Australian Capital Territory", "Canberra", ("canberra",)),
    ("India", "Maharashtra", "Mumbai", ("mumbai", "bombay")),
    ("India", "Delhi", "New Delhi", ("new delhi", "delhi")),
    ("India", "Karnataka", "Bengaluru", ("bengaluru", "bangalore")),
    ("India", "Telangana", "Hyderabad", ("hyderabad",)),
    ("India", "Tamil Nadu", "Chennai", ("chennai", "madras")),
    ("India", "Maharashtra", "Pune", ("pune",)),
    ("India", "West Bengal", "Kolkata", ("kolkata", "calcutta")),
    ("India", "Uttar Pradesh", "Noida", ("noida",)),
    ("India", "Haryana", "Gurugram", ("gurugram", "gurgaon")),
    ("Singapore", "Singapore", "Singapore", ("singapore",)),
    ("Malaysia", "Kuala Lumpur", "Kuala Lumpur", ("kuala lumpur", "kl")),
    ("Japan", "Tokyo", "Tokyo", ("tokyo",)),
    ("China", "Shanghai", "Shanghai", ("shanghai",)),
    ("China", "Beijing", "Beijing", ("beijing",)),
    ("China", "Guangdong", "Shenzhen", ("shenzhen",)),
    # Europe
    ("Germany", "Berlin", "Berlin", ("berlin",)),
    ("Germany", "Bavaria", "Munich", ("munich", "münchen")),
    ("Germany", "Hesse", "Frankfurt", ("frankfurt",)),
    ("Germany", "Hamburg", "Hamburg", ("hamburg",)),
    ("France", "Ile-de-France", "Paris", ("paris",)),
    ("Netherlands", "North Holland", "Amsterdam", ("amsterdam",)),
    ("Ireland", "Leinster", "Dublin", ("dublin",)),
    ("Spain", "Madrid", "Madrid", ("madrid",)),
    ("Spain", "Catalonia", "Barcelona", ("barcelona",)),
    ("Turkey", "Istanbul", "Istanbul", ("istanbul",)),
    ("Brazil", "Sao Paulo", "Sao Paulo", ("sao paulo", "são paulo")),
    ("South Africa", "Gauteng", "Johannesburg", ("johannesburg", "joburg")),
    ("South Africa", "Western Cape", "Cape Town", ("cape town",)),
    ("New Zealand", "Auckland", "Auckland", ("auckland",)),
    # Same name as the UK city. Inserted last so a bare "London" stays the UK;
    # "London, ON" is resolved through the country hint above.
    ("Canada", "Ontario", "London", ("london",)),
)

_CITY_INDEX: dict[str, list[Place]] = {}
for _country, _region, _city, _aliases in _CITIES:
    for _alias in _aliases:
        _CITY_INDEX.setdefault(normalize_text(_alias), []).append(
            Place(_city, _region, _country)
        )

_COUNTRY_INDEX: dict[str, str] = {}
for _country, _aliases in _COUNTRY_ALIASES.items():
    for _alias in _aliases:
        _COUNTRY_INDEX[normalize_text(_alias)] = _country

_WORLDWIDE = re.compile(
    r"\b(?:worldwide|anywhere|work from anywhere|fully global|global remote|"
    r"remote anywhere|anywhere on earth|location independent)\b",
    re.IGNORECASE,
)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE = re.compile(r"\b(?:on-?site|in[- ]office|office[- ]based)\b", re.IGNORECASE)
_REMOTE = re.compile(
    r"\b(?:remote|work from home|wfh|telecommut\w*|distributed team|virtual position|"
    r"worldwide|anywhere|online)\b",
    re.IGNORECASE,
)
_REGION_WORDS = {
    "worldwide": "Worldwide",
    "global": "Worldwide",
    "emea": "EMEA",
    "europe": "Europe",
    "european union": "European Union",
    "eu": "European Union",
    "apac": "APAC",
    "asia pacific": "APAC",
    "asia-pacific": "APAC",
    "asia": "Asia",
    "north america": "North America",
    "south america": "South America",
    "latin america": "Latin America",
    "latam": "Latin America",
    "middle east": "Middle East",
    "mena": "Middle East",
    "gcc": "GCC",
    "africa": "Africa",
    "united states": "United States",
    "canada": "Canada",
    "united kingdom": "United Kingdom",
}
_RESTRICTION = re.compile(
    r"(?:must (?:be )?(?:based|located|resident|live)\s+(?:in|within)|"
    r"only (?:open )?to (?:candidates|applicants|residents)?\s*(?:in|from)?|"
    r"eligible to work in|authori[sz]ed to work in|work authori[sz]ation in|"
    r"residents? of|citizens? of|within (?:the )?|based in|located in|"
    r"open to (?:candidates|applicants) in|remote\s*[-–—(]\s*)([^.\n;)]{2,80})",
    re.IGNORECASE,
)

_SENIOR_PATTERNS = (
    ("intern", re.compile(r"\b(?:intern|internship|trainee)\b", re.IGNORECASE)),
    ("entry", re.compile(r"\b(?:entry[- ]level|junior|graduate|fresher|associate)\b", re.IGNORECASE)),
    ("lead", re.compile(r"\b(?:lead|principal|staff|head of|director|vp|chief)\b", re.IGNORECASE)),
    ("senior", re.compile(r"\bsenior\b", re.IGNORECASE)),
    ("mid", re.compile(r"\b(?:mid[- ]level|intermediate|experienced)\b", re.IGNORECASE)),
)
_YEARS_REQ = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(?P<max>\d{1,2})?\s*years?",
    re.IGNORECASE,
)


@dataclass
class RemoteInfo:
    workplace_type: str = "unknown"  # remote | hybrid | onsite | unknown
    job_location: str = ""
    company_location: str = ""
    eligible_locations: list[str] = field(default_factory=list)
    remote_scope: str = "unknown"  # worldwide | country | region | city | unknown
    location_confidence: str = "unknown"  # verified | inferred | unknown
    evidence: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "workplace_type": self.workplace_type,
            "job_location": self.job_location,
            "company_location": self.company_location,
            "eligible_locations": self.eligible_locations,
            "remote_scope": self.remote_scope,
            "location_confidence": self.location_confidence,
            "evidence": self.evidence,
        }


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _has_alias(text: str, alias: str) -> bool:
    if not alias:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))


def _country_in(text: str) -> str:
    matches = [
        (len(alias), country)
        for alias, country in _COUNTRY_INDEX.items()
        if _has_alias(text, alias)
    ]
    return max(matches, default=(0, ""))[1]


def _region_in(text: str, country_hint: str = "") -> tuple[str, str]:
    matches: list[tuple[int, str, str]] = []
    for alias, (region, country) in _REGION_ALIASES.items():
        if _has_alias(text, alias) and (not country_hint or country == country_hint):
            matches.append((len(alias), region, country))
    if not matches and country_hint:
        for alias, (region, country) in _REGION_ALIASES.items():
            if _has_alias(text, alias) and country == country_hint:
                matches.append((len(alias), region, country))
    if not matches:
        return "", ""
    _, region, country = max(matches)
    return region, country


def _city_in(text: str, country_hint: str = "") -> Place | None:
    candidates: list[tuple[int, Place]] = []
    for alias, places in _CITY_INDEX.items():
        if not _has_alias(text, alias):
            continue
        for place in places:
            if country_hint and place.country != country_hint:
                continue
            candidates.append((len(alias), place))
    if not candidates and not country_hint:
        return None
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _trailing_code_hint(original: str) -> tuple[tuple[str, str], ...]:
    """Read the postal/state code at the end of a location, if any.

    Only upper-case codes are considered. Lower-case "ca" in prose is not a
    location signal, while "San Francisco, CA" and "Karachi, PK" are.
    """
    parts = [part.strip() for part in re.split(r"[,/|]", original) if part.strip()]
    if not parts:
        return ()
    words = parts[-1].split()
    if not words:
        return ()
    token = words[-1].strip(" .()")
    if not re.fullmatch(r"[A-Z]{2,3}", token):
        return ()
    return _ADMIN_CODE_HINTS.get(token, ())


def resolve_location(value: str | None) -> ResolvedLocation:
    """Resolve free text to a city, region or country without a Lahore default."""
    original = _clean(value)
    lowered = normalize_text(original)
    if not lowered:
        return ResolvedLocation(original=original)

    if lowered in REMOTE_ONLY_TERMS or (
        any(word in lowered for word in REMOTE_WORDS)
        and not _country_in(lowered)
        and not _city_in(lowered)
        and not _region_in(lowered)[0]
    ):
        return ResolvedLocation(original=original, kind="remote", remote_only=True)

    # Strip remote/hybrid noise but keep parenthesised eligibility ("Remote (Canada)").
    probe = re.sub(r"\b(?:remote|hybrid|work from home|wfh)\b", " ", lowered)
    probe = re.sub(r"\s+", " ", probe).strip(" ,-–—()/")

    # An upper-case state/country code is more precise than a city name alone:
    # "Lahore, MN" is not Lahore, Pakistan, and "London, ON" is in Canada.
    code_candidates = _trailing_code_hint(original)
    if code_candidates:
        for _region, country in code_candidates:
            city = _city_in(probe, country)
            if city:
                return ResolvedLocation(
                    original=original,
                    kind="city",
                    city=city.city,
                    region=city.region,
                    country=city.country,
                    remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
                )
        if len(code_candidates) == 1:
            region, country = code_candidates[0]
            if region:
                return ResolvedLocation(
                    original=original,
                    kind="region",
                    region=region,
                    country=country,
                    remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
                )
            return ResolvedLocation(
                original=original,
                kind="country",
                country=country,
                remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
            )
        # The code has several readings and no city disambiguated it.
        return ResolvedLocation(original=original, kind="unknown")

    country = _country_in(probe)
    city = _city_in(probe, country)
    region, region_country = _region_in(probe, country)
    if not country and region_country:
        country = region_country
    if not country and city:
        country = city.country

    if city:
        # "London, Ontario" and "London, Canada" resolve to Canada, not the UK.
        if city.city == "London" and country == "Canada":
            city = Place("London", "Ontario", "Canada")
        return ResolvedLocation(
            original=original,
            kind="city",
            city=city.city,
            region=city.region,
            country=city.country,
            remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
        )
    if region:
        return ResolvedLocation(
            original=original,
            kind="region",
            region=region,
            country=country,
            remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
        )
    if country:
        return ResolvedLocation(
            original=original,
            kind="country",
            country=country,
            remote_only=bool(any(word in lowered for word in REMOTE_WORDS)),
        )
    return ResolvedLocation(original=original, kind="unknown")


def _places_in_text(text: str) -> list[str]:
    """Return canonical place names mentioned in a short restriction window."""
    found: list[tuple[int, str]] = []
    lowered = normalize_text(text)
    for alias, places in _CITY_INDEX.items():
        if _has_alias(lowered, alias):
            found.extend((lowered.find(alias), place.label) for place in places)
    for alias, country in _COUNTRY_INDEX.items():
        if _has_alias(lowered, alias):
            found.append((lowered.find(alias), country))
    for alias, (region, _country) in _REGION_ALIASES.items():
        if _has_alias(lowered, alias):
            found.append((lowered.find(alias), region))
    seen: set[str] = set()
    ordered: list[str] = []
    for _position, name in sorted(found):
        key = normalize_text(name)
        if key and key not in seen and name not in {"Worldwide", "Global"}:
            seen.add(key)
            ordered.append(name)
    return ordered


def _company_location(description: str) -> str:
    match = re.search(
        r"(?:headquarters|headquartered|company hq|hq)\s*(?:is|:)?\s*(?:in|at)\s+"
        r"([A-Z][A-Za-z .'-]{2,50})",
        description or "",
    )
    return _clean(match.group(1)) if match else ""


def _clean_job_location(location: str) -> str:
    cleaned = re.sub(
        r"\b(?:fully\s+)?(?:remote|hybrid|work from home|wfh|anywhere|distributed)\b",
        " ",
        location or "",
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\(\s*\)|\[\s*\]", " ", cleaned)
    cleaned = re.sub(r"^[\s,;/|–—-]+|[\s,;/|–—-]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if normalize_text(cleaned) in {"", "in", "worldwide", "anywhere"}:
        return ""
    return cleaned


def _remote_scope_from_location(location: str, remote: bool) -> tuple[str, list[str]]:
    if not remote:
        return "unknown", []
    lowered = normalize_text(location)
    if not lowered:
        return "unknown", []
    if _WORLDWIDE.search(location):
        return "worldwide", ["Worldwide"]
    # Remove the word remote before resolving the remaining place.
    probe = re.sub(r"\b(?:remote|hybrid|work from home|wfh)\b", " ", location, flags=re.I)
    place = resolve_location(probe)
    if place.known:
        scope = "city" if place.city else "region" if place.region else "country"
        label = ", ".join(part for part in (place.city, place.region, place.country) if part)
        return scope, [label or place.display]
    region_names = [
        label
        for word, label in _REGION_WORDS.items()
        if _has_alias(lowered, word) and label not in {"United States", "Canada", "United Kingdom"}
    ]
    if region_names:
        return "region", list(dict.fromkeys(region_names))
    return "unknown", []


def parse_remote_info(
    location: str,
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    raw_remote: bool | None = None,
) -> RemoteInfo:
    """Extract workplace type and the *employee* eligibility area.

    ``remote`` alone means "remote, scope unknown". A country, region or city
    must be stated in the location or in a restriction sentence before it is
    treated as verified eligibility.
    """
    location = _clean(location)
    tag_text = " ".join(tags or [])
    early = f"{title}\n{location}\n{tag_text}\n{description[:1200]}"

    hybrid = bool(_HYBRID.search(early))
    onsite_explicit = bool(_ONSITE.search(early))
    remote_signal = bool(_REMOTE.search(early)) or raw_remote is True
    if hybrid:
        workplace = "hybrid"
    elif raw_remote is True:
        workplace = "remote"
    elif raw_remote is False:
        # An explicit on-site flag from the source wins over a stray "online"
        # in the title or a description mentioning distributed teams.
        workplace = "onsite"
    elif remote_signal:
        workplace = "remote"
    elif onsite_explicit or location:
        workplace = "onsite"
    else:
        workplace = "unknown"

    is_remote = workplace in {"remote", "hybrid"}
    scope, eligible = _remote_scope_from_location(location, is_remote)
    confidence = "verified" if scope != "unknown" else ("inferred" if is_remote else "unknown")

    evidence_parts: list[str] = []
    if _WORLDWIDE.search(early):
        scope, eligible = "worldwide", ["Worldwide"]
        confidence = "verified"
        evidence_parts.append("posting says work from anywhere/worldwide")

    # Restriction sentences in the body. These are what separate a worldwide
    # remote role from one that only accepts candidates in Canada or the UK.
    body = description[:5000]
    for match in _RESTRICTION.finditer(body):
        window = match.group(1) or ""
        places = _places_in_text(window)
        if not places:
            continue
        lowered = window.lower()
        regions = [label for word, label in _REGION_WORDS.items() if _has_alias(lowered, word)]
        if regions:
            scope, eligible = "region", list(dict.fromkeys(regions))
            confidence = "verified"
            evidence_parts.append(match.group(0).strip()[:120])
            continue
        if places:
            eligible = list(dict.fromkeys([*eligible, *places]))
            if any(p in _COUNTRY_ALIASES for p in places):
                scope = "country"
            elif any(p in {v[0] for v in _REGION_ALIASES.values()} for p in places):
                scope = "region"
            else:
                scope = "city"
            confidence = "verified"
            evidence_parts.append(match.group(0).strip()[:120])

    # Parenthesised or dashed eligibility in the advertised location.
    location_match = re.search(r"(?:remote|hybrid)\s*[-–—(]\s*([^)\n;]{2,60})", location, re.I)
    if location_match:
        places = _places_in_text(location_match.group(1))
        if places:
            eligible = list(dict.fromkeys([*places, *eligible]))
            scope = "country" if any(p in _COUNTRY_ALIASES for p in places) else "region"
            confidence = "verified"
            evidence_parts.append(location_match.group(0).strip())

    if is_remote and scope == "unknown":
        eligible = []
        confidence = "unknown" if not location else "inferred"
    if not is_remote:
        scope, eligible = "unknown", []
        # Remote scope does not apply here; confidence instead describes the
        # advertised job location, which is the field users filter on.
        confidence = (
            "verified"
            if resolve_location(_clean_job_location(location)).known
            else "unknown"
        )

    return RemoteInfo(
        workplace_type=workplace,
        job_location=_clean_job_location(location),
        company_location=_company_location(description),
        eligible_locations=eligible[:8],
        remote_scope=scope,
        location_confidence=confidence,
        evidence="; ".join(evidence_parts)[:400],
    )


def infer_seniority(title: str, description: str = "") -> str:
    text = f"{title} {description[:1600]}"
    for label, pattern in _SENIOR_PATTERNS:
        if pattern.search(title):
            return label
    years = [
        int(match.group("min"))
        for match in _YEARS_REQ.finditer(text)
        if int(match.group("min")) <= 20
    ]
    if years:
        required = min(years)
        if required <= 1:
            return "entry"
        if required <= 3:
            return "mid"
        if required <= 6:
            return "senior"
        return "lead"
    for label, pattern in _SENIOR_PATTERNS:
        if pattern.search(text):
            return label
    return "mid"


def job_place(job_location: str) -> ResolvedLocation:
    """Resolve the advertised location of a job (never its HQ)."""
    return resolve_location(job_location)


def _eligible_contains(place: ResolvedLocation, eligible: list[str]) -> str:
    for value in eligible:
        resolved = resolve_location(value)
        if resolved.kind == "remote":
            continue
        # Respect the granularity of the eligibility entry. A city in the same
        # country is not eligible for a different city, while a country-wide
        # remote role is.
        if resolved.city:
            if place.city and normalize_text(place.city) == normalize_text(resolved.city):
                return "city"
            continue
        if resolved.region:
            if place.region and normalize_text(place.region) == normalize_text(resolved.region):
                return "region"
            continue
        if resolved.country:
            if place.country and normalize_text(place.country) == normalize_text(
                resolved.country
            ):
                return "country"
            continue
        # Free-text eligibility such as "EU" or "EMEA".
        if place.country and normalize_text(place.country) in normalize_text(value):
            return "country"
        if place.region and normalize_text(place.region) in normalize_text(value):
            return "region"
        if place.city and normalize_text(place.city) in normalize_text(value):
            return "city"
    return ""


def location_match(
    request: ResolvedLocation,
    *,
    job_location: str,
    workplace_type: str = "",
    remote_scope: str = "unknown",
    eligible_locations: list[str] | None = None,
) -> tuple[str, str]:
    """Return ``(tier, explanation)`` for a job against a requested place.

    ``tier`` values: exact, region, country, remote_eligible, remote_worldwide,
    remote, remote_unknown, none. Only the first six are verified matches.
    """
    eligible = eligible_locations or []
    remote = workplace_type in {"remote", "hybrid"}
    if request.remote_only:
        if workplace_type == "remote":
            return "remote", "Remote role."
        if workplace_type == "hybrid":
            return "remote", "Hybrid role with remote work."
        return "none", "On-site role does not match a remote-only search."

    if not request.known:
        return "none", "Location could not be resolved."

    if remote:
        if remote_scope == "worldwide":
            return "remote_worldwide", "Remote role open worldwide."
        tier = _eligible_contains(request, eligible)
        if tier:
            return "remote_eligible", f"Remote role open to candidates in {request.display}."
        advertised = job_place(job_location)
        if advertised.known:
            if request.city and advertised.city and normalize_text(request.city) == normalize_text(
                advertised.city
            ):
                return "exact", "Advertised location matches the requested city."
            if request.country and advertised.country == request.country:
                # A remote role advertised in the requested country is usable by
                # a candidate there, even when the specific city is not listed.
                return "country", "Remote role advertised in the requested country."
        if remote_scope == "unknown" or not eligible:
            # Unknown eligibility is not a verified match, but at country level a
            # remote role is still worth surfacing (it may accept anyone).
            if not request.city:
                return "remote_unknown", "Remote eligibility is not stated by the source."
            return "none", "Remote eligibility does not include the requested city."
        return "none", "Remote eligibility does not include the requested location."

    advertised = job_place(job_location)
    if not advertised.known:
        return "none", "Advertised location is unknown."
    if request.city and advertised.city and normalize_text(request.city) == normalize_text(
        advertised.city
    ):
        return "exact", "Advertised city matches."
    if request.region and advertised.region and normalize_text(request.region) == normalize_text(
        advertised.region
    ):
        return "region", "Same region."
    if request.country and advertised.country == request.country:
        if request.city:
            return "country", "Same country, different city."
        return "country", "Same country."
    if request.region and advertised.country == request.country:
        return "country", "Same country, different region."
    return "none", "Advertised location is outside the requested area."


def eligibility_label(
    workplace_type: str,
    remote_scope: str,
    eligible_locations: list[str] | None,
    job_location: str = "",
) -> str:
    if workplace_type == "remote":
        scope = remote_scope or "unknown"
        if scope == "worldwide":
            return "Remote · worldwide"
        if scope in {"country", "region", "city"} and eligible_locations:
            return f"Remote · {', '.join(eligible_locations[:2])}"
        return "Remote · eligibility unknown"
    if workplace_type == "hybrid":
        place = job_location or "location not stated"
        return f"Hybrid · {place}"
    if workplace_type == "onsite":
        return f"On-site · {job_location}" if job_location else "On-site"
    return "Workplace type unknown"


__all__ = [
    "Place",
    "RemoteInfo",
    "ResolvedLocation",
    "eligibility_label",
    "infer_seniority",
    "job_place",
    "location_match",
    "parse_remote_info",
    "resolve_location",
]
