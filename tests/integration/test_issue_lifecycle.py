"""End-to-end tests for the report lifecycle - the platform's core promise."""

from __future__ import annotations

import pytest

from civicos.core.context import Actor, set_actor
from civicos.domain.enums import IssueStatus, Priority, SLAState
from civicos.repositories.issues import IssueFilters, IssueRepository
from civicos.services import issue_service, sla_service


@pytest.fixture(autouse=True)
def _system_actor() -> None:
    set_actor(Actor(kind="system", display_name="tests", is_superadmin=True))


class TestIntake:
    async def test_report_is_persisted_classified_and_routed(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description=(
                    "Sewage is overflowing from the manhole outside the school "
                    "gate and children walk through it every morning."
                ),
                latitude=12.9716,
                longitude=77.5946,
                address="Outside the school gate",
            ),
        )
        issue = outcome.issue

        assert outcome.created
        assert issue.reference.startswith(tenant.slug[:3].upper())
        assert issue.category is not None, "a report must be categorised"
        assert issue.department_id is not None, "a report must reach a department"
        assert issue.resolution_due_at is not None, "an SLA must be committed to"
        assert issue.embedding, "an embedding is needed for dedupe and search"
        assert issue.status in {IssueStatus.TRIAGED, IssueStatus.ASSIGNED}

    async def test_short_descriptions_are_rejected(self, session, tenant) -> None:
        from civicos.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await issue_service.report_issue(
                session, tenant, issue_service.IssueDraft(description="broken")
            )

    async def test_personal_data_is_stripped_from_the_stored_description(
        self, session, tenant
    ) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description=(
                    "The streetlight outside my house is broken, call me on "
                    "0300-1234567 to arrange access."
                ),
                latitude=12.97,
                longitude=77.59,
            ),
        )
        assert "0300" not in outcome.issue.description
        # The verbatim original is retained for the case file.
        assert outcome.issue.description_original is not None
        assert "0300" in outcome.issue.description_original

    async def test_ai_opinion_is_recorded_separately_from_the_decision(
        self, session, tenant
    ) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Garbage has not been collected on our street for ten days.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        issue = outcome.issue
        assert issue.ai_triaged_at is not None
        assert issue.ai_category_slug is not None
        assert issue.ai_confidence is not None
        # The model's view and the municipality's decision are distinct fields.
        assert hasattr(issue, "ai_priority") and hasattr(issue, "priority")

    async def test_invalid_coordinates_are_discarded_with_a_warning(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Something is wrong at this location, please inspect.",
                latitude=0.0,
                longitude=0.0,
            ),
        )
        assert outcome.issue.latitude is None
        assert any("coordinates" in warning.lower() for warning in outcome.warnings)

    async def test_emergency_category_forces_emergency_priority(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="There is a fire in the abandoned building on the main road.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        assert outcome.issue.priority is Priority.EMERGENCY


class TestDeduplication:
    async def test_identical_resubmission_is_not_stored_twice(self, session, tenant) -> None:
        draft = issue_service.IssueDraft(
            description="The drain on Station Road is blocked and overflowing.",
            latitude=12.9716,
            longitude=77.5946,
        )
        first = await issue_service.report_issue(session, tenant, draft)
        second = await issue_service.report_issue(session, tenant, draft)

        assert first.created
        assert not second.created, "a double submission must not create a second record"
        assert second.issue.id == first.issue.id

    async def test_nearby_similar_report_is_merged(self, session, tenant) -> None:
        first = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Sewage overflowing from the manhole on Station Road.",
                latitude=12.9716,
                longitude=77.5946,
            ),
        )
        second = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Manhole on Station Road is overflowing with sewage.",
                latitude=12.9717,
                longitude=77.5947,
            ),
        )

        assert second.merged_into is not None
        assert second.merged_into.id == first.issue.id
        assert second.issue.status is IssueStatus.DUPLICATE
        await session.refresh(first.issue)
        assert first.issue.confirmations >= 1, "corroboration must be counted"

    async def test_different_problem_at_same_place_is_not_merged(self, session, tenant) -> None:
        await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Sewage overflowing from the manhole on Station Road.",
                latitude=12.9716,
                longitude=77.5946,
            ),
        )
        second = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="The streetlight pole here has been dark for a month.",
                latitude=12.9716,
                longitude=77.5946,
            ),
        )
        assert second.merged_into is None
        assert second.issue.status is not IssueStatus.DUPLICATE


class TestWorkflow:
    async def test_happy_path_to_closure(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Pothole on the main road is causing motorcycles to swerve.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        issue = outcome.issue

        for status in (
            IssueStatus.ASSIGNED,
            IssueStatus.IN_PROGRESS,
            IssueStatus.RESOLVED,
            IssueStatus.VERIFIED,
            IssueStatus.CLOSED,
        ):
            if issue.status is status:
                continue
            await issue_service.transition(session, tenant, issue, status, note="step")

        assert issue.status is IssueStatus.CLOSED
        assert issue.resolved_at is not None
        assert issue.first_response_at is not None, "the response clock must stop"

    async def test_illegal_transition_is_refused(self, session, tenant) -> None:
        from civicos.core.errors import WorkflowError

        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Broken bench in the park needs replacing please.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        with pytest.raises(WorkflowError):
            await issue_service.transition(session, tenant, outcome.issue, IssueStatus.CLOSED)

    async def test_rejection_requires_a_reason(self, session, tenant) -> None:
        from civicos.core.errors import ValidationError

        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Complaint about something outside municipal remit entirely.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        with pytest.raises(ValidationError):
            await issue_service.transition(session, tenant, outcome.issue, IssueStatus.REJECTED)

    async def test_reopening_resets_the_resolution_clock(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Street sweeping has been skipped on our lane repeatedly.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        issue = outcome.issue
        if issue.status is not IssueStatus.ASSIGNED:
            await issue_service.transition(session, tenant, issue, IssueStatus.ASSIGNED)
        await issue_service.transition(session, tenant, issue, IssueStatus.RESOLVED)
        await issue_service.transition(session, tenant, issue, IssueStatus.REOPENED)

        assert issue.reopened_count == 1
        assert issue.resolved_at is None
        assert issue.sla_resolution_state is SLAState.ON_TRACK

    async def test_timeline_records_every_step(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Water supply has been out in our block since Tuesday.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        await issue_service.transition(
            session, tenant, outcome.issue, IssueStatus.ASSIGNED, note="dispatching"
        )
        await session.commit()

        events = await IssueRepository(session, tenant.id).timeline(outcome.issue.id)
        kinds = {str(event.event_type) for event in events}
        assert "created" in kinds
        assert "status_changed" in kinds


class TestSLA:
    async def test_targets_are_set_at_intake(self, session, tenant) -> None:
        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Rubbish is piling up beside the market and smells badly.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        issue = outcome.issue
        assert issue.response_due_at is not None
        assert issue.resolution_due_at is not None
        assert issue.resolution_due_at > issue.response_due_at

    async def test_emergency_sla_ignores_office_hours(self, session, tenant) -> None:
        from datetime import UTC, datetime, timedelta

        # A Saturday night emergency must still get a short, calendar-time deadline.
        saturday_night = datetime(2026, 1, 3, 23, 50, tzinfo=UTC)
        targets = await sla_service.compute_targets(
            session,
            tenant.id,
            category_id=None,
            priority=Priority.EMERGENCY,
            municipality=tenant,
            start=saturday_night,
        )
        assert targets.business_hours_only is False
        assert targets.response_due_at - saturday_night <= timedelta(hours=1)

    async def test_breach_is_detected_and_recorded(self, session, tenant) -> None:
        from datetime import timedelta

        from civicos.core.clock import utcnow

        outcome = await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Blocked storm drain is flooding the lane after rain.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        issue = outcome.issue
        issue.response_due_at = utcnow() - timedelta(hours=2)
        issue.resolution_due_at = utcnow() - timedelta(hours=1)

        breached = await sla_service.refresh(session, issue)

        assert breached, "an overdue issue must be reported as breached"
        assert issue.sla_resolution_state is SLAState.BREACHED
        assert issue.is_breached


class TestQueries:
    async def test_filters_narrow_the_queue(self, session, tenant) -> None:
        await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="Streetlight outside the clinic has been dark for weeks.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        await session.commit()

        repository = IssueRepository(session, tenant.id)
        from civicos.core.pagination import PageParams

        page = PageParams(page=1, page_size=10)
        open_items, open_total = await repository.search(IssueFilters(open_only=True), page)
        breached_items, breached_total = await repository.search(
            IssueFilters(breached_only=True), page
        )

        assert open_total >= 1
        assert all(issue.status.is_open for issue in open_items)
        assert breached_total == 0 and not breached_items

    async def test_tenant_isolation(self, session, tenant, session_factory) -> None:
        """A repository must never return another municipality's records."""
        from civicos.db.seed import seed_demo_tenant

        other = await seed_demo_tenant(
            session,
            slug="othertown",
            name="Other Town",
            admin_email="admin@othertown.example",
            admin_password="OtherPassword123!",
        )
        await issue_service.report_issue(
            session,
            tenant,
            issue_service.IssueDraft(
                description="A report that belongs to the first municipality only.",
                latitude=12.97,
                longitude=77.59,
            ),
        )
        await session.commit()

        from civicos.core.pagination import PageParams

        page = PageParams(page=1, page_size=50)
        _, other_total = await IssueRepository(session, other.id).search(IssueFilters(), page)
        _, own_total = await IssueRepository(session, tenant.id).search(IssueFilters(), page)

        assert own_total >= 1
        assert other_total == 0, "tenant scoping must not leak records"
