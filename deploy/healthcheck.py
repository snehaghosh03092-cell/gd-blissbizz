"""Container health without curl. Gateway readiness verifies all seven services and their DBs."""

import sys
from urllib.request import urlopen

path = "/api/ready" if len(sys.argv) > 1 and sys.argv[1] == "gateway" else "/health"
with urlopen("http://127.0.0.1:8000" + path, timeout=10) as response:
    if response.status != 200:
        raise SystemExit(1)
