"""Reference data for the seed: Indian states/cities/names and the sample GST rate versions.

Everything here is FICTIONAL SAMPLE data for local development. Rates must be verified by a chartered accountant
before any real use. Addresses, phone numbers and e-mail addresses are made up; e-mail uses the reserved
``example.com`` / ``*.example`` domains so nothing can reach a real person.
"""

from datetime import date
from uuid import NAMESPACE_URL, uuid5

DEV_PASSWORD = "REDACTED-EXAMPLE-CREDENTIAL"  # 18 chars (min is 15); dev seed accounts only

# state code -> (state name, [(city, postal prefix)])
STATES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "29": ("Karnataka", [("Bengaluru", "560"), ("Mysuru", "570")]),
    "27": ("Maharashtra", [("Mumbai", "400"), ("Pune", "411"), ("Nagpur", "440")]),
    "07": ("Delhi", [("New Delhi", "110")]),
    "36": ("Telangana", [("Hyderabad", "500")]),
    "33": ("Tamil Nadu", [("Chennai", "600"), ("Coimbatore", "641")]),
    "19": ("West Bengal", [("Kolkata", "700")]),
    "24": ("Gujarat", [("Ahmedabad", "380"), ("Surat", "395")]),
    "08": ("Rajasthan", [("Jaipur", "302")]),
    "09": ("Uttar Pradesh", [("Lucknow", "226"), ("Noida", "201")]),
    "32": ("Kerala", [("Kochi", "682")]),
    "04": ("Chandigarh", [("Chandigarh", "160")]),
    "23": ("Madhya Pradesh", [("Indore", "452")]),
    "21": ("Odisha", [("Bhubaneswar", "751")]),
    "10": ("Bihar", [("Patna", "800")]),
    "18": ("Assam", [("Guwahati", "781")]),
    "06": ("Haryana", [("Gurugram", "122")]),
}
# relative customer share outside the home state
STATE_WEIGHTS = {
    "29": 9,
    "27": 12,
    "07": 10,
    "36": 7,
    "33": 7,
    "19": 5,
    "24": 6,
    "08": 3,
    "09": 6,
    "32": 3,
    "04": 2,
    "23": 3,
    "21": 2,
    "10": 2,
    "18": 1,
    "06": 4,
}

FIRST_NAMES = [
    "Aarav",
    "Vivaan",
    "Aditya",
    "Arjun",
    "Rohan",
    "Ishaan",
    "Kabir",
    "Rahul",
    "Karthik",
    "Nikhil",
    "Siddharth",
    "Varun",
    "Yash",
    "Dev",
    "Manav",
    "Anaya",
    "Diya",
    "Ira",
    "Kavya",
    "Meera",
    "Neha",
    "Priya",
    "Riya",
    "Saanvi",
    "Sneha",
    "Tanvi",
    "Zoya",
    "Ananya",
    "Pooja",
    "Shreya",
    "Farhan",
    "Imran",
    "Sana",
    "Ayesha",
    "Gurpreet",
    "Harleen",
    "Joseph",
    "Maria",
    "Lakshmi",
    "Suresh",
]
LAST_NAMES = [
    "Sharma",
    "Verma",
    "Gupta",
    "Iyer",
    "Nair",
    "Reddy",
    "Rao",
    "Patel",
    "Shah",
    "Mehta",
    "Singh",
    "Kaur",
    "Khan",
    "Das",
    "Bose",
    "Banerjee",
    "Chatterjee",
    "Joshi",
    "Kulkarni",
    "Deshmukh",
    "Pillai",
    "Menon",
    "Naidu",
    "Agarwal",
    "Jain",
    "Malhotra",
    "Kapoor",
    "Bhatt",
    "Thakur",
    "Mishra",
]
STREETS = [
    "MG Road",
    "Station Road",
    "Park Street",
    "Gandhi Nagar",
    "Lake View Road",
    "Temple Street",
    "Nehru Colony",
    "Residency Road",
    "Market Lane",
    "Green Park",
]

_SAMPLE_NOTE = "SAMPLE seed data; verify with a chartered accountant before real use"
RATE_START = date(2017, 7, 1)
REFORM_DATE = date(
    2025, 9, 22
)  # sample: slab restructuring (12%/28% retired, 40% added); verify before use

# (tax_class, cgst, sgst, igst, cess, effective_from, effective_to)
RATE_ROWS = [
    ("GST_0", 0, 0, 0, 0, RATE_START, None),
    ("GST_5", 2.5, 2.5, 5, 0, RATE_START, None),
    ("GST_12", 6, 6, 12, 0, RATE_START, date(2025, 9, 21)),
    ("GST_18", 9, 9, 18, 0, RATE_START, None),
    ("GST_28", 14, 14, 28, 0, RATE_START, date(2025, 9, 21)),
    ("GST_40", 20, 20, 40, 0, REFORM_DATE, None),
]


def rate_version_id(tax_class: str, effective_from: date) -> str:
    return str(uuid5(NAMESPACE_URL, f"blissbiz-seed-tax/{tax_class}/{effective_from.isoformat()}"))


def tax_rate_rows() -> list[dict]:
    return [
        {
            "id": rate_version_id(c, f),
            "tax_class": c,
            "cgst_rate": cg,
            "sgst_rate": sg,
            "igst_rate": ig,
            "cess_rate": cess,
            "effective_from": f,
            "effective_to": t,
            "source_note": _SAMPLE_NOTE,
        }
        for c, cg, sg, ig, cess, f, t in RATE_ROWS
    ]
