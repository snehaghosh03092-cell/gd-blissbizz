"""GSTIN format validation: syntax, state code and mod-36 checksum.

A GSTIN that passes ``validate_gstin`` is only **format valid**. It is NOT proof that the business
is registered;
only an authorised verification service can establish that (none is integrated yet).
"""

import re
from dataclasses import dataclass

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# 2-digit state code + PAN (5 letters, 4 digits, 1 letter) + entity number + 'Z' + checksum
_PATTERN = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$")

# GST state / union-territory codes (25 is the legacy Daman & Diu code, still seen on old
# registrations)
STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}


@dataclass(frozen=True)
class GstinResult:
    ok: bool
    normalized: str | None = None
    state_code: str | None = None
    state_name: str | None = None
    pan: str | None = None
    error: str | None = (
        None  # machine-readable: bad_length | bad_format | unknown_state | bad_checksum
    )


def checksum_char(first14: str) -> str:
    total = 0
    for index, char in enumerate(first14):
        product = _ALPHABET.index(char) * (1 if index % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _ALPHABET[(36 - total % 36) % 36]


def with_checksum(first14: str) -> str:
    """Complete a 14-character GSTIN prefix (used to build sample data)."""
    return first14 + checksum_char(first14)


def validate_gstin(value: str) -> GstinResult:
    gstin = (value or "").strip().upper()
    if len(gstin) != 15:
        return GstinResult(False, error="bad_length")
    match = _PATTERN.match(gstin)
    if match is None:
        return GstinResult(False, error="bad_format")
    state_code, pan = match.group(1), match.group(2)
    if state_code not in STATE_CODES:
        return GstinResult(False, error="unknown_state")
    if checksum_char(gstin[:14]) != gstin[14]:
        return GstinResult(False, error="bad_checksum")
    return GstinResult(True, gstin, state_code, STATE_CODES[state_code], pan)
