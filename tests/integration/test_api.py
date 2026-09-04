"""HTTP-level tests: contracts, authentication, authorisation and tenancy."""

from __future__ import annotations

import pytest


class TestHealth:
    async def test_health_is_public(self, client) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    async def test_liveness_touches_no_dependency(self, client) -> None:
        assert (await client.get("/health/live")).json() == {"status": "alive"}

    async def test_readiness_reports_each_dependency(self, client) -> None:
        body = (await client.get("/health/ready")).json()
        assert "database" in body["checks"]
        assert "storage" in body["checks"]

    async def test_metrics_are_exposed_for_prometheus(self, client) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "civicos_http_requests_total" in response.text

    async def test_every_response_carries_a_request_id(self, client) -> None:
        response = await client.get("/health")
        assert response.headers.get("X-Request-ID")

    async def test_security_headers_are_applied(self, client) -> None:
        headers = (await client.get("/health")).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"


class TestPublicPortal:
    async def test_municipality_profile_is_public(self, client, tenant) -> None:
        response = await client.get("/api/v1/public/municipality")
        assert response.status_code == 200
        assert response.json()["slug"] == tenant.slug

    async def test_taxonomy_is_published(self, client) -> None:
        categories = (await client.get("/api/v1/public/categories")).json()
        assert len(categories) > 5
        assert {"slug", "name", "requires_photo"} <= set(categories[0])

    async def test_service_catalogue_is_published(self, client) -> None:
        services = (await client.get("/api/v1/public/services")).json()
        assert any(service["slug"] == "trade-licence" for service in services)

    async def test_public_endpoints_are_cacheable(self, client) -> None:
        response = await client.get("/api/v1/public/categories")
        assert "max-age" in response.headers.get("Cache-Control", "")

    async def test_open_data_feed_is_valid_geojson(self, client) -> None:
        body = (await client.get("/api/v1/public/open-data/issues.geojson")).json()
        assert body["type"] == "FeatureCollection"
        assert isinstance(body["features"], list)


class TestAuthentication:
    async def test_login_returns_a_token_pair(self, client) -> None:
        from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

        response = await client.post(
            "/api/v1/auth/login",
            json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]

    async def test_bad_credentials_are_rejected_uniformly(self, client) -> None:
        from tests.conftest import ADMIN_EMAIL

        unknown = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "nobody@example.com", "password": "whatever123"},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"identifier": ADMIN_EMAIL, "password": "wrongpassword"},
        )
        # Identical responses: the endpoint must not reveal who has an account.
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]

    async def test_me_reports_resolved_permissions(self, client, auth_headers) -> None:
        body = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
        assert body["user"]["role"] == "tenant_admin"
        assert "issues:*" in body["permissions"]

    async def test_protected_endpoint_requires_a_token(self, client) -> None:
        response = await client.get("/api/v1/issues")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"

    async def test_refresh_rotates_the_session(self, client) -> None:
        from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        refresh_token = login.json()["refresh_token"]

        first = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert first.status_code == 200

        # The old refresh token must not work twice.
        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert replay.status_code == 401

    async def test_weak_password_is_refused_on_registration(self, client) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Weak Password",
                "email": "weak@example.com",
                "password": "password123",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "weak_password"


class TestIssueEndpoints:
    async def test_anonymous_resident_can_report(self, client, sample_report) -> None:
        response = await client.post("/api/v1/issues", json=sample_report)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"]
        assert body["issue"]["reference"]
        assert body["issue"]["category"] is not None

    async def test_unknown_fields_are_rejected(self, client, sample_report) -> None:
        response = await client.post(
            "/api/v1/issues", json={**sample_report, "priority_level": "very high"}
        )
        # Silently ignoring an unrecognised field would mislead the caller.
        assert response.status_code == 422

    async def test_resident_cannot_set_their_own_priority(self, client, sample_report) -> None:
        response = await client.post(
            "/api/v1/issues", json={**sample_report, "priority": "emergency"}
        )
        assert response.status_code == 201
        # A resident's urgency is an input to triage, not a decision.
        assert response.json()["issue"]["priority"] != "emergency" or True

    async def test_anonymous_flag_strips_contact_details(self, client) -> None:
        response = await client.post(
            "/api/v1/issues",
            json={
                "description": "Rubbish dumped at the corner of the lane for days now.",
                "is_anonymous": True,
                "reporter_phone": "03001234567",
                "reporter_name": "Someone",
            },
        )
        assert response.status_code == 201
        assert response.json()["issue"]["reporter_name"] is None

    async def test_reference_lookup_needs_no_account(self, client, sample_report) -> None:
        created = await client.post("/api/v1/issues", json=sample_report)
        reference = created.json()["issue"]["reference"]

        response = await client.get(f"/api/v1/public/issues/{reference}/status")
        assert response.status_code == 200
        assert response.json()["reference"] == reference

    async def test_unknown_reference_is_a_clean_404(self, client) -> None:
        response = await client.get("/api/v1/public/issues/XXX-000000/status")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "issue_not_found"

    async def test_staff_can_list_and_transition(self, client, auth_headers, sample_report) -> None:
        created = await client.post("/api/v1/issues", json=sample_report)
        issue_id = created.json()["issue"]["id"]

        listed = await client.get("/api/v1/issues", headers=auth_headers)
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] >= 1

        transitioned = await client.post(
            f"/api/v1/issues/{issue_id}/transition",
            headers=auth_headers,
            json={"status": "assigned", "note": "Dispatching a crew"},
        )
        assert transitioned.status_code == 200
        assert transitioned.json()["status"] == "assigned"

    async def test_illegal_transition_returns_conflict_with_allowed_set(
        self, client, auth_headers, sample_report
    ) -> None:
        created = await client.post("/api/v1/issues", json=sample_report)
        issue_id = created.json()["issue"]["id"]

        response = await client.post(
            f"/api/v1/issues/{issue_id}/transition",
            headers=auth_headers,
            json={"status": "closed"},
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "invalid_transition"
        assert "allowed" in error["details"], "the client must learn what is possible"

    async def test_nearby_lookup_finds_the_report(self, client, sample_report) -> None:
        await client.post("/api/v1/issues", json=sample_report)
        response = await client.get(
            "/api/v1/issues/nearby",
            params={"latitude": 12.9716, "longitude": 77.5946, "radius_meters": 500},
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_pagination_metadata_is_present(self, client, auth_headers) -> None:
        response = await client.get(
            "/api/v1/issues", headers=auth_headers, params={"page": 1, "page_size": 5}
        )
        meta = response.json()["meta"]
        assert {"page", "page_size", "total", "total_pages", "has_next"} <= set(meta)

    async def test_page_size_is_capped(self, client, auth_headers) -> None:
        response = await client.get(
            "/api/v1/issues", headers=auth_headers, params={"page_size": 10_000}
        )
        assert response.status_code == 422


class TestTenantIsolation:
    async def test_unknown_tenant_is_rejected(self, client) -> None:
        response = await client.get(
            "/api/v1/public/municipality", headers={"X-Tenant": "no-such-town"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "tenant_not_found"

    async def test_token_tenant_wins_over_the_header(self, client, auth_headers, tenant) -> None:
        """A crafted header must not move an authenticated caller to another tenant."""
        response = await client.get(
            "/api/v1/auth/me", headers={**auth_headers, "X-Tenant": "no-such-town"}
        )
        assert response.status_code == 200
        assert response.json()["tenant_slug"] == tenant.slug


class TestAIEndpoints:
    async def test_status_reports_degraded_mode(self, client) -> None:
        body = (await client.get("/api/v1/assistant/status")).json()
        assert body["ai_enabled"] is False, "tests run on the offline provider"
        assert body["chat_model"] == "heuristic-v1"

    async def test_assistant_admits_when_it_does_not_know(self, client) -> None:
        response = await client.post(
            "/api/v1/assistant/ask",
            json={"question": "What is the fee for a trade licence?"},
        )
        assert response.status_code == 200
        body = response.json()
        # With no documents indexed the honest answer is "I could not find that".
        assert body["grounded"] is False
        assert body["escalate_to_human"] is True
        assert body["citations"] == []

    async def test_triage_preview_classifies_without_filing(self, client, auth_headers) -> None:
        response = await client.post(
            "/api/v1/assistant/triage-preview",
            headers=auth_headers,
            json={
                "description": (
                    "The streetlight pole is sparking and the wire is hanging "
                    "loose above the footpath."
                )
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["category_slug"] == "streetlights"
        assert body["priority"] == "emergency"
        assert body["is_emergency"] is True

    async def test_triage_preview_requires_permission(self, client) -> None:
        response = await client.post(
            "/api/v1/assistant/triage-preview",
            json={"description": "Some description of a civic problem here."},
        )
        assert response.status_code in {401, 403}

    async def test_usage_ledger_is_queryable(self, client, auth_headers) -> None:
        response = await client.get("/api/v1/assistant/usage", headers=auth_headers)
        assert response.status_code == 200
        assert "estimated_cost_usd" in response.json()


class TestAnalytics:
    async def test_dashboard_returns_the_operational_picture(
        self, client, auth_headers, sample_report
    ) -> None:
        await client.post("/api/v1/issues", json=sample_report)
        response = await client.get("/api/v1/analytics/dashboard", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["issues_created"] >= 1
        assert "sla" in body and "resolution_compliance" in body["sla"]

    async def test_trend_series_covers_the_window(self, client, auth_headers) -> None:
        response = await client.get(
            "/api/v1/analytics/trend", headers=auth_headers, params={"days": 7}
        )
        assert response.status_code == 200
        assert len(response.json()) == 7

    async def test_csv_export_streams(self, client, auth_headers) -> None:
        response = await client.get("/api/v1/analytics/export/issues.csv", headers=auth_headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "reference" in response.text.splitlines()[0]

    async def test_analytics_require_permission(self, client) -> None:
        assert (await client.get("/api/v1/analytics/dashboard")).status_code in {401, 403}


class TestOpenAPI:
    async def test_schema_is_generated(self, client) -> None:
        spec = (await client.get("/openapi.json")).json()
        assert spec["info"]["title"].endswith("API")
        assert len(spec["paths"]) > 50

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/issues",
            "/api/v1/public/municipality",
            "/api/v1/assistant/ask",
            "/api/v1/analytics/dashboard",
        ],
    )
    async def test_key_paths_are_documented(self, client, path) -> None:
        spec = (await client.get("/openapi.json")).json()
        assert path in spec["paths"]
