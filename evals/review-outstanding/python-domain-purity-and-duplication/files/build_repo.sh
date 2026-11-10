#!/usr/bin/env bash
# Python CLI with hexagonal-ish layout (domain is pure, adapters do I/O).
# Uncommitted change adds a "late shipments" report that: imports requests into the
# domain layer, duplicates date parsing that exists in app/utils/dates.py (twice),
# has a boolean flag param + deep nesting, and a unittest-style test where siblings use pytest.
# No remote, no CLAUDE.md, no spec. Usage: build_repo.sh <target-dir>
set -eu
T="$1"; mkdir -p "$T"; cd "$T"
git init -q -b main .; git config user.email dev@example.com; git config user.name Dev
mkdir -p app/domain app/adapters app/utils tests

cat > README.md <<'EOF'
# shiptrack

Small CLI for shipment reporting.

## Layout
- `app/domain/` — pure business logic. No I/O, no network, no third-party imports.
- `app/adapters/` — everything that talks to the outside world (HTTP, files).
- `app/cli.py` — wires adapters into domain functions.
- `app/utils/` — shared helpers.
EOF

cat > app/__init__.py <<'EOF'
EOF
cat > app/domain/__init__.py <<'EOF'
EOF
cat > app/adapters/__init__.py <<'EOF'
EOF
cat > app/utils/__init__.py <<'EOF'
EOF

cat > app/utils/dates.py <<'EOF'
from datetime import date, datetime


def parse_iso_date(value: str) -> date:
    """Parse 'YYYY-MM-DD' or a full ISO timestamp into a date."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def days_between(start: date, end: date) -> int:
    return (end - start).days
EOF

cat > app/domain/models.py <<'EOF'
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Shipment:
    id: str
    carrier: str
    promised: date
    delivered: date | None
EOF

cat > app/domain/on_time.py <<'EOF'
from app.domain.models import Shipment
from app.utils.dates import days_between


def on_time_rate(shipments: list[Shipment]) -> float:
    delivered = [s for s in shipments if s.delivered is not None]
    if not delivered:
        return 0.0
    on_time = [s for s in delivered if days_between(s.promised, s.delivered) <= 0]
    return len(on_time) / len(delivered)
EOF

cat > app/adapters/shipments_api.py <<'EOF'
import requests

from app.domain.models import Shipment
from app.utils.dates import parse_iso_date

BASE_URL = "https://api.example.com"


def fetch_shipments(customer_id: str) -> list[Shipment]:
    resp = requests.get(f"{BASE_URL}/customers/{customer_id}/shipments", timeout=10)
    resp.raise_for_status()
    return [
        Shipment(
            id=row["id"],
            carrier=row["carrier"],
            promised=parse_iso_date(row["promised"]),
            delivered=parse_iso_date(row["delivered"]) if row.get("delivered") else None,
        )
        for row in resp.json()
    ]
EOF

cat > app/cli.py <<'EOF'
import argparse

from app.adapters.shipments_api import fetch_shipments
from app.domain.on_time import on_time_rate


def main() -> None:
    parser = argparse.ArgumentParser(prog="shiptrack")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rate = sub.add_parser("on-time")
    rate.add_argument("customer_id")
    args = parser.parse_args()

    if args.cmd == "on-time":
        print(f"{on_time_rate(fetch_shipments(args.customer_id)):.1%}")


if __name__ == "__main__":
    main()
EOF

cat > tests/conftest.py <<'EOF'
from datetime import date

import pytest

from app.domain.models import Shipment


@pytest.fixture
def shipments():
    return [
        Shipment("a", "ups", date(2026, 1, 1), date(2026, 1, 1)),
        Shipment("b", "ups", date(2026, 1, 1), date(2026, 1, 4)),
        Shipment("c", "fedex", date(2026, 1, 2), None),
    ]
EOF

cat > tests/test_on_time.py <<'EOF'
from app.domain.on_time import on_time_rate


def test_on_time_rate(shipments):
    assert on_time_rate(shipments) == 0.5


def test_empty():
    assert on_time_rate([]) == 0.0
EOF

cat > pyproject.toml <<'EOF'
[project]
name = "shiptrack"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["requests>=2.31"]

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

git add -A; git commit -qm "on-time rate report"

# ---- Uncommitted change: late shipments report ----
cat > app/domain/late.py <<'EOF'
from datetime import datetime, date

import requests

from app.domain.models import Shipment


def late_shipments(customer_id, include_undelivered, today=None):
    resp = requests.get(f"https://api.example.com/customers/{customer_id}/shipments", timeout=10)
    data = resp.json()
    result = []
    today = today or date.today()
    for row in data:
        promised = datetime.fromisoformat(row["promised"].replace("Z", "+00:00")).date()
        if row.get("delivered"):
            delivered = datetime.fromisoformat(row["delivered"].replace("Z", "+00:00")).date()
            if (delivered - promised).days > 0:
                result.append({"id": row["id"], "carrier": row["carrier"], "days_late": (delivered - promised).days})
        else:
            if include_undelivered:
                if (today - promised).days > 0:
                    result.append({"id": row["id"], "carrier": row["carrier"], "days_late": (today - promised).days})
    result.sort(key=lambda r: r["days_late"], reverse=True)
    return result
EOF

python3 - <<'EOF'
import re
p = "app/cli.py"
s = open(p).read()
s = s.replace("from app.domain.on_time import on_time_rate\n",
              "from app.domain.late import late_shipments\nfrom app.domain.on_time import on_time_rate\n")
s = s.replace('    rate.add_argument("customer_id")\n',
              '    rate.add_argument("customer_id")\n    late = sub.add_parser("late")\n    late.add_argument("customer_id")\n    late.add_argument("--all", action="store_true")\n')
s = s.replace('        print(f"{on_time_rate(fetch_shipments(args.customer_id)):.1%}")\n',
              '        print(f"{on_time_rate(fetch_shipments(args.customer_id)):.1%}")\n    elif args.cmd == "late":\n        for r in late_shipments(args.customer_id, args.all):\n            print(f"{r[\'id\']}\\t{r[\'carrier\']}\\t{r[\'days_late\']}d")\n')
open(p, "w").write(s)
EOF

cat > tests/test_late.py <<'EOF'
import unittest
from datetime import date
from unittest.mock import patch

from app.domain.late import late_shipments


class TestLate(unittest.TestCase):
    @patch("app.domain.late.requests.get")
    def test_late(self, get):
        get.return_value.json.return_value = [
            {"id": "a", "carrier": "ups", "promised": "2026-01-01", "delivered": "2026-01-03"},
            {"id": "b", "carrier": "ups", "promised": "2026-01-01", "delivered": None},
        ]
        result = late_shipments("c1", True, today=date(2026, 1, 5))
        self.assertEqual([r["id"] for r in result], ["b", "a"])
EOF
echo "built $T"
