"""Classifies a card's free-text `address` into a state/metro region, for
the Dashboard's region-mix chart only. No `Company.hq_city`/`hq_country`
writer exists anywhere in this codebase (both are always NULL), and this
deliberately does not add one — region is derived at analytics query time,
not persisted, to avoid a schema change for a first cut (see
.claude/specs/16-dashboard-analytics.md's "Region classification" section).

Fixed keyword taxonomy, same "module-level data, not inline" convention as
industry_classification.py/scoring.py/designation.py.
"""

# Major Indian states/metro regions, most-specific-city-first within each
# tuple so a metro name (e.g. "Mumbai") doesn't get shadowed by its state.
# Order across regions has no effect on matching (first matching region in
# the address wins; ties can't occur since keyword sets don't overlap).
_REGION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Maharashtra": ("mumbai", "pune", "nagpur", "nashik", "thane", "maharashtra"),
    "Delhi NCR": ("new delhi", "delhi", "gurugram", "gurgaon", "noida", "faridabad", "ghaziabad"),
    "Karnataka": ("bengaluru", "bangalore", "mysuru", "mysore", "karnataka"),
    "Tamil Nadu": ("chennai", "coimbatore", "madurai", "tamil nadu", "tamilnadu"),
    "Gujarat": ("ahmedabad", "surat", "vadodara", "rajkot", "gujarat"),
    "Telangana": ("hyderabad", "telangana"),
    "West Bengal": ("kolkata", "west bengal"),
    "Rajasthan": ("jaipur", "jodhpur", "udaipur", "rajasthan"),
    "Uttar Pradesh": ("lucknow", "kanpur", "agra", "varanasi", "uttar pradesh"),
    "Punjab": ("ludhiana", "amritsar", "chandigarh", "punjab"),
    "Haryana": ("haryana", "panipat", "ambala"),
    "Kerala": ("kochi", "cochin", "thiruvananthapuram", "kerala"),
    "Madhya Pradesh": ("indore", "bhopal", "madhya pradesh"),
    "Andhra Pradesh": ("visakhapatnam", "vijayawada", "andhra pradesh"),
    "Bihar": ("patna", "bihar"),
}

_UNCLASSIFIED_REGION_LABEL = "Unclassified"


def classify_region(address: str | None) -> str:
    """Case-insensitive substring match against major Indian states/metro
    regions. Returns "Unclassified" if the address is blank or matches
    nothing — never raises."""
    if not address or not address.strip():
        return _UNCLASSIFIED_REGION_LABEL
    lowered = address.lower()
    for region, keywords in _REGION_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return region
    return _UNCLASSIFIED_REGION_LABEL
