"""Integration: client_credentials token refresh against real Keycloak.

Proves the two things the unit test cannot: (1) Keycloak issues a genuinely new token on
refresh, and (2) Lakekeeper accepts the refreshed token for a real operation.

No sleeps / short-lived realm tweaks: setting refresh_margin larger than the token lifespan
makes every acquisition fall past the refresh threshold, forcing a real refresh round-trip
on each use. The expiry-timing threshold itself is covered deterministically by the unit
test (test_auth.py::test_client_credentials_auto_refreshes_near_expiry).
"""

from __future__ import annotations

import pytest

from pylakekeeper import Client, ClientCredentials

pytestmark = pytest.mark.integration


def test_token_refresh_against_real_keycloak(stack):
    auth = ClientCredentials(
        token_url=stack.token_url,
        client_id=stack.client_id,
        client_secret=stack.client_secret,
        refresh_margin=24 * 3600,  # >> token lifespan -> always re-acquire
    )
    with Client(
        stack.base_url, stack.warehouse_id, auth, project_id=stack.project_id
    ) as client:
        # A real catalog operation succeeds with the freshly acquired token.
        list(client.generic_tables.list(stack.namespace))

        # Two acquisitions each re-fetch from Keycloak and yield distinct tokens
        # (different jti) — i.e. a refresh genuinely happened.
        token_a = auth.auth_header()
        token_b = auth.auth_header()
        assert token_a.startswith("Bearer ")
        assert token_a != token_b

        # The refreshed token is accepted by Lakekeeper for another real operation.
        list(client.generic_tables.list(stack.namespace))
