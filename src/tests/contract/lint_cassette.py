"""Tape gate: fail a fresh recording BEFORE it becomes the replay baseline.

The record run is the real debugging run (live responses); the replay run is
only the regression gate. This linter sits between them: a cassette that
recorded auth failures or upstream server errors is poison — replaying it
just reproduces a broken baseline deterministically. Run it right after
recording:

    uv run --project src/backend python src/tests/contract/lint_cassette.py

Verdicts:
  * 401/403 from any host          -> POISON (auth was broken at record time)
  * 5xx from any host              -> POISON (upstream transient; re-record)
  * "not trusted by this database" -> POISON (wrong-tenant token)
  * leaked bearer/secret patterns  -> POISON (scrubber gap — fix conftest)
  * 400/404/409/422                -> fine (legitimate recorded semantics)
"""

import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import yaml

CASSETTE = (
    Path(__file__).parent
    / "cassettes"
    / "src.tests.contract.test_chat_flow_contract"
    / "chat_flow.yaml"
)

_POISON_BODY_MARKERS = (
    "not trusted by this database account",
    "AADSTS",  # any AAD error body recorded verbatim
    "Lifetime validation failed",
)

_SECRET_PATTERNS = (
    re.compile(r"Bearer eyJ", re.IGNORECASE),  # unredacted JWT
    re.compile(
        r'"(client_secret|primaryMasterKey|secondaryMasterKey)"\s*:\s*"(?!\[REDACTED\])[^"]{8,}'
    ),
)


def main() -> int:
    if not CASSETTE.exists():
        print(f"POISON: no cassette at {CASSETTE} — did the recording run?")
        return 1

    with CASSETTE.open() as f:
        data = yaml.safe_load(f) or {}
    interactions = data.get("interactions", [])
    if not interactions:
        print("POISON: cassette has 0 interactions — recording captured nothing.")
        return 1

    poison: list[str] = []
    tally: Counter = Counter()

    for i in interactions:
        uri = i["request"]["uri"]
        host = uri.split("/")[2] if "://" in uri else uri
        code = i["response"]["status"]["code"]
        tally[(host, code)] += 1

        if code in (401, 403) or code >= 500:
            poison.append(f"{code} {i['request']['method']} {uri}")

        resp_body = i["response"].get("body", {}).get("string") or ""
        if isinstance(resp_body, bytes):
            resp_body = resp_body.decode("utf-8", "replace")

        req_body = i["request"].get("body") or ""
        if isinstance(req_body, bytes):
            req_body = req_body.decode("utf-8", "replace")

        header_blob = repr(i["request"].get("headers", {})) + repr(
            i["response"].get("headers", {})
        )

        for marker in _POISON_BODY_MARKERS:
            if marker in resp_body:
                poison.append(f"body-marker '{marker}' in {uri}")
        for pat in _SECRET_PATTERNS:
            if pat.search(resp_body) or pat.search(req_body) or pat.search(header_blob):
                poison.append(f"secret-leak pattern {pat.pattern!r} in {uri}")

    print(f"{len(interactions)} interactions:")
    for (host, code), n in sorted(tally.items()):
        print(f"  {host:55} {code}: {n}")

    if poison:
        print("\nPOISON — this tape must NOT become the replay baseline:")
        for p in poison[:20]:
            print(f"  {p}")
        if len(poison) > 20:
            print(f"  ... and {len(poison) - 20} more")
        print("\nFix the cause (auth/tenant/scrubber), rm the cassette, re-record.")
        return 1

    print("\nTape is clean — safe to use as the replay baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
