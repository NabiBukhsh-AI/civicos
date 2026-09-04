"""Executive briefings.

Turns the analytics tables into the two-minute readout an administrator or
council actually reads. The model is given *only* numbers that were computed in
SQL - it composes and prioritises, it never estimates. That division is what
keeps a generated briefing safe to circulate.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from civicos.ai.prompts import BRIEFING_SYSTEM
from civicos.ai.schemas import ExecutiveBriefing
from civicos.ai.types import CompletionRequest, Message
from civicos.ai.usage import UsageContext, run_completion

logger = structlog.get_logger(__name__)


async def compose_briefing(
    statistics: dict[str, Any],
    *,
    period_label: str,
    municipality_name: str,
    audience: str = "administrator",
    usage: UsageContext | None = None,
) -> ExecutiveBriefing:
    """Write a briefing from a pre-computed statistics payload."""
    prompt = "\n\n".join(
        [
            f"Municipality: {municipality_name}",
            f"Period: {period_label}",
            f"Audience: {audience}",
            "Statistics (the only figures you may use):",
            json.dumps(statistics, indent=2, default=str),
            (
                "Write the briefing. Lead with changes and decisions needed. "
                "Every number you state must appear above verbatim."
            ),
        ]
    )

    request = CompletionRequest(
        messages=[Message(role="user", content=prompt)],
        system=BRIEFING_SYSTEM,
        response_schema=ExecutiveBriefing.response_schema(),
        schema_name="executive_briefing",
        max_output_tokens=2000,
        temperature=0.2,
    )

    result = await run_completion(request, capability="briefing", usage=usage)

    try:
        return ExecutiveBriefing.model_validate(result.parsed or {})
    except Exception as exc:
        logger.warning("briefing_validation_failed", error=str(exc))
        return _deterministic_briefing(statistics, period_label, municipality_name)


def _deterministic_briefing(
    statistics: dict[str, Any], period_label: str, municipality_name: str
) -> ExecutiveBriefing:
    """A real briefing assembled from the numbers, with no model involved.

    Used when AI is unavailable. It is deliberately not a placeholder: an
    administrator still gets the figures that matter, just without prose.
    """
    from civicos.ai.schemas import BriefingSection

    totals = statistics.get("totals", {})
    sla = statistics.get("sla", {})
    created = totals.get("issues_created", 0)
    resolved = totals.get("issues_resolved", 0)
    open_now = totals.get("open_issues", 0)
    breached = sla.get("resolution_breached", 0)

    key_points = [
        f"{created} report(s) received, {resolved} resolved.",
        f"{open_now} report(s) currently open.",
    ]
    if breached:
        key_points.append(f"{breached} report(s) have breached their resolution deadline.")
    if backlog := statistics.get("oldest_open_days"):
        key_points.append(f"Oldest open report is {backlog} day(s) old.")

    sections = [
        BriefingSection(
            heading="Workload",
            body=(
                f"{created} new report(s) and {resolved} resolution(s) in {period_label}. "
                f"Net change in the open queue: {created - resolved:+d}."
            ),
        )
    ]
    if by_category := statistics.get("top_categories"):
        listed = ", ".join(f"{item.get('label')} ({item.get('count')})" for item in by_category[:5])
        sections.append(BriefingSection(heading="Leading categories", body=listed))
    if hotspots := statistics.get("hotspots"):
        listed = ", ".join(
            f"{item.get('label', 'area')} ({item.get('count')})" for item in hotspots[:5]
        )
        sections.append(BriefingSection(heading="Concentrations", body=listed))

    return ExecutiveBriefing(
        headline=f"{municipality_name}: {created} reports, {resolved} resolved ({period_label})",
        key_points=key_points,
        sections=sections,
        risks=(
            [f"{breached} report(s) past their deadline require escalation."] if breached else []
        ),
        recommended_actions=(
            ["Review breached reports with the responsible department heads."] if breached else []
        ),
        period_label=period_label,
    )


def render_markdown(briefing: ExecutiveBriefing) -> str:
    """Render a briefing for email, WhatsApp or a printed council pack."""
    lines = [f"# {briefing.headline}", ""]
    if briefing.key_points:
        lines.append("## At a glance")
        lines.extend(f"- {point}" for point in briefing.key_points)
        lines.append("")
    for section in briefing.sections:
        lines.extend([f"## {section.heading}", section.body, ""])
    if briefing.risks:
        lines.append("## Risks")
        lines.extend(f"- {risk}" for risk in briefing.risks)
        lines.append("")
    if briefing.recommended_actions:
        lines.append("## Recommended actions")
        lines.extend(f"- {action}" for action in briefing.recommended_actions)
        lines.append("")
    lines.append(f"_Period: {briefing.period_label}_")
    return "\n".join(lines)
