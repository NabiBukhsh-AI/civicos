"""Unit tests for the core primitives everything else rests on."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from civicos.core.clock import (
    add_business_minutes,
    business_minutes_between,
    ensure_utc,
    humanize_delta,
)
from civicos.core.geo import (
    Point,
    bounding_box,
    encode_geohash,
    grid_cell,
    haversine_meters,
    is_valid_coordinate,
    point_in_polygon,
)
from civicos.core.permissions import Role, is_staff, permissions_for
from civicos.core.security import (
    generate_api_key,
    generate_reference,
    hash_api_key,
    hash_password,
    mask_contact,
    validate_password_strength,
    verify_password,
)
from civicos.core.text import (
    contains_pii,
    cosine_similarity,
    jaccard_similarity,
    redact_pii,
    slugify,
    summarise_for_title,
)
from civicos.domain.enums import ISSUE_TRANSITIONS, IssueStatus, Priority


class TestGeo:
    def test_haversine_matches_known_distance(self) -> None:
        # Greenwich to Paris is ~343 km.
        london = Point(51.4779, -0.0015)
        paris = Point(48.8566, 2.3522)
        distance = haversine_meters(london, paris)
        assert 335_000 < distance < 350_000

    def test_haversine_is_symmetric_and_zero_for_same_point(self) -> None:
        a, b = Point(24.5, 67.1), Point(24.6, 67.2)
        assert haversine_meters(a, b) == pytest.approx(haversine_meters(b, a))
        assert haversine_meters(a, a) == pytest.approx(0.0, abs=1e-6)

    def test_bounding_box_contains_points_within_radius(self) -> None:
        centre = Point(12.9716, 77.5946)
        box = bounding_box(centre, 1000)
        assert box.contains(centre)
        # A point 500 m north must fall inside a 1 km box.
        near = Point(centre.latitude + 0.0045, centre.longitude)
        assert box.contains(near)

    def test_null_island_is_rejected(self) -> None:
        assert not is_valid_coordinate(0.0, 0.0)
        assert not is_valid_coordinate(None, 10.0)
        assert not is_valid_coordinate(95.0, 10.0)
        assert is_valid_coordinate(12.97, 77.59)

    def test_geohash_prefixes_agree_for_nearby_points(self) -> None:
        a = encode_geohash(12.9716, 77.5946, 7)
        b = encode_geohash(12.9717, 77.5947, 7)
        assert a[:5] == b[:5]

    def test_grid_cell_buckets_nearby_points_together(self) -> None:
        cell_a = grid_cell(12.9716, 77.5946, 250)
        cell_b = grid_cell(12.9717, 77.5947, 250)
        far = grid_cell(13.5, 78.2, 250)
        assert cell_a == cell_b
        assert cell_a != far

    def test_point_in_polygon(self) -> None:
        square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
        assert point_in_polygon(Point(0.5, 0.5), square)
        assert not point_in_polygon(Point(1.5, 0.5), square)


class TestClock:
    def test_ensure_utc_makes_naive_values_aware(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0)
        assert ensure_utc(naive).tzinfo is UTC

    def test_business_minutes_skip_the_weekend(self) -> None:
        # Saturday 10:00 to Monday 10:00, Sunday non-working.
        start = datetime(2026, 1, 3, 10, 0, tzinfo=UTC)
        end = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
        minutes = business_minutes_between(start, end, timezone="UTC", weekend_days=frozenset({6}))
        # Saturday 10:00-17:00 (420) + Monday 09:00-10:00 (60); Sunday skipped.
        assert minutes == 480

    def test_add_business_minutes_lands_in_working_hours(self) -> None:
        # Friday 16:00 + 4 working hours rolls into the next working day.
        start = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
        due = add_business_minutes(
            start,
            240,
            timezone="UTC",
            workday_start=time(9, 0),
            workday_end=time(17, 0),
            weekend_days=frozenset({6}),
        )
        assert due > start
        assert time(9, 0) <= due.time() <= time(17, 0)

    def test_humanize_delta(self) -> None:
        assert humanize_delta(timedelta(minutes=38)) == "38m"
        assert humanize_delta(timedelta(hours=3, minutes=10)) == "3h 10m"
        assert humanize_delta(timedelta(days=2, hours=4)) == "2d 4h"


class TestText:
    def test_slugify(self) -> None:
        assert slugify("Municipal Committee No. 3") == "municipal-committee-no-3"
        assert slugify("!!!") == "item"

    def test_redact_pii_removes_contact_details(self) -> None:
        text = "Call me on 0300-1234567 or email a.b@example.com, CNIC 42101-1234567-1"
        redacted = redact_pii(text)
        assert "0300" not in redacted
        assert "example.com" not in redacted
        assert "42101" not in redacted
        assert "Call me on" in redacted

    def test_contains_pii(self) -> None:
        assert contains_pii("reach me at 03001234567")
        assert not contains_pii("the drain is blocked")

    def test_similarity_scores(self) -> None:
        a = "garbage not collected on street five"
        b = "street five garbage uncollected"
        c = "streetlight is broken near the park"
        assert jaccard_similarity(a, b) > jaccard_similarity(a, c)

    def test_cosine_similarity_edges(self) -> None:
        assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
        assert cosine_similarity([], [1]) == 0.0

    def test_summarise_for_title_takes_first_sentence(self) -> None:
        body = "The drain is blocked. It has been like this for weeks."
        assert summarise_for_title(body) == "The drain is blocked."


class TestSecurity:
    def test_password_round_trip(self) -> None:
        hashed = hash_password("CorrectHorse123")
        assert verify_password("CorrectHorse123", hashed)
        assert not verify_password("wrong", hashed)

    def test_long_passphrases_are_not_truncated(self) -> None:
        """bcrypt silently truncates at 72 bytes; pre-hashing must prevent that."""
        base = "a" * 80
        hashed = hash_password(base + "ONE")
        assert not verify_password(base + "TWO", hashed)

    def test_weak_passwords_are_rejected(self) -> None:
        from civicos.core.errors import ValidationError

        with pytest.raises(ValidationError):
            validate_password_strength("short")
        with pytest.raises(ValidationError):
            validate_password_strength("password123")
        validate_password_strength("Str0ngEnoughPass")

    def test_api_key_hash_is_verifiable(self) -> None:
        plaintext, prefix, hashed = generate_api_key()
        assert plaintext.startswith("civ_")
        assert prefix in plaintext
        assert hash_api_key(plaintext) == hashed

    def test_reference_uses_unambiguous_alphabet(self) -> None:
        reference = generate_reference("abc")
        assert reference.startswith("ABC-")
        # O/0 and I/1 are excluded because these are read out over the phone.
        assert not set("OI") & set(reference.split("-")[1])

    def test_mask_contact(self) -> None:
        assert mask_contact("03001234567").endswith("567")
        assert "@" in str(mask_contact("someone@example.com"))
        assert mask_contact(None) is None


class TestPermissions:
    def test_roles_map_to_permissions(self) -> None:
        assert permissions_for(Role.CITIZEN)
        assert "issues:create" in permissions_for(Role.CITIZEN)
        assert "issues:assign" not in permissions_for(Role.CITIZEN)
        assert "issues:assign" in permissions_for(Role.SUPERVISOR)

    def test_superadmin_holds_wildcards(self) -> None:
        assert "issues:*" in permissions_for(Role.SUPER_ADMIN)

    def test_staff_classification(self) -> None:
        assert is_staff(Role.SUPERVISOR)
        assert not is_staff(Role.CITIZEN)
        assert not is_staff("nonsense")

    def test_unknown_role_grants_nothing(self) -> None:
        assert permissions_for("mayor-of-nowhere") == frozenset()


class TestWorkflow:
    def test_every_status_has_a_transition_entry(self) -> None:
        for status in IssueStatus:
            assert status in ISSUE_TRANSITIONS

    def test_terminal_statuses_only_reopen(self) -> None:
        assert ISSUE_TRANSITIONS[IssueStatus.CLOSED] == frozenset({IssueStatus.REOPENED})

    def test_no_transition_targets_an_unknown_status(self) -> None:
        for targets in ISSUE_TRANSITIONS.values():
            for target in targets:
                assert isinstance(target, IssueStatus)

    def test_priority_weights_are_ordered(self) -> None:
        weights = [priority.weight for priority in Priority]
        assert weights == sorted(weights)
