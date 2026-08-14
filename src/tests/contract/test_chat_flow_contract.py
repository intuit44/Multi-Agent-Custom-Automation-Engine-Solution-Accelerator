"""Generated contract coverage for the chat / artifact lane.

Every request below is derived by schemathesis from the app's own OpenAPI
schema — types, required fields, enums, formats and the declared response
codes. Nothing here enumerates a case by hand, so the coverage grows with the
schema instead of with hand-written files.

The eight operations under ``/api/v4/chat/`` are the lane the artifact
defects live in: upload → stream → generated_file → download → session
reload.

What a failure means:

* ``server_error``            — the operation 500s on input its own schema
                                declares valid;
* ``status_code_conformance`` — it answers with a code the schema never
                                declares (the 410 on download-file is exactly
                                this shape);
* ``response_schema_conformance`` — the body does not match the model the
                                frontend is generated against.
"""

import os

import pytest
import schemathesis
from hypothesis import HealthCheck, settings

from app import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)

CHAT_FLOW = schema.include(path_regex=r"^/api/v4/")

# Replay is free, so generate broadly. Recording is not: each example is a
# real Foundry invocation and a real Cosmos write, so a recording pass takes
# one example per operation — enough to capture each upstream endpoint once,
# which is all the loose `match_on` in conftest needs.
_MAX_EXAMPLES = int(os.environ.get("CONTRACT_MAX_EXAMPLES", "20"))


@pytest.mark.default_cassette("chat_flow.yaml")
@pytest.mark.vcr
@CHAT_FLOW.parametrize()
@settings(
    derandomize=True,
    max_examples=_MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
def test_chat_flow(case):
    case.call_and_validate()
