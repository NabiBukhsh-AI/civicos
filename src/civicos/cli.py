"""``civicos`` command line.

Operational tasks an administrator actually runs: create the schema, onboard a
municipality, mint an admin account, ingest documents, run the SLA sweep,
inspect configuration. Every command is explicit - nothing here happens as a
side effect of starting the server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from civicos import __version__

app = typer.Typer(
    name="civicos",
    help="CivicOS - municipal operations platform.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database schema and seeding.", no_args_is_help=True)
tenant_app = typer.Typer(help="Municipality onboarding and configuration.", no_args_is_help=True)
docs_app = typer.Typer(help="Knowledge-base document ingestion.", no_args_is_help=True)
ops_app = typer.Typer(help="Scheduled operational jobs.", no_args_is_help=True)

app.add_typer(db_app, name="db")
app.add_typer(tenant_app, name="tenant")
app.add_typer(docs_app, name="docs")
app.add_typer(ops_app, name="ops")

console = Console()


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@app.command()
def version() -> None:
    """Print the version and the resolved configuration summary."""
    from civicos.core.config import get_settings

    settings = get_settings()
    table = Table(title=f"CivicOS {__version__}", show_header=False)
    table.add_row("Environment", str(settings.environment))
    table.add_row("Database", settings.database.url.split("@")[-1])
    table.add_row("AI provider", settings.ai.provider)
    table.add_row("Chat model", settings.ai.chat_model)
    table.add_row("Embedding model", settings.ai.embedding_model)
    table.add_row("Vector backend", settings.rag.vector_backend)
    table.add_row("Storage", settings.storage.backend)
    table.add_row("Default tenant", settings.tenancy.default_tenant_slug)
    console.print(table)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on code changes.")] = False,
    workers: Annotated[int, typer.Option(help="Worker processes.")] = 1,
) -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run(
        "civicos.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=None if reload else workers,
        log_config=None,
    )


# ------------------------------------------------------------------ database ---


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, revision)
    console.print(f"[green]Database upgraded to {revision}.[/green]")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument(help="Target revision.")]) -> None:
    """Roll back Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.downgrade(config, revision)
    console.print(f"[yellow]Database downgraded to {revision}.[/yellow]")


@db_app.command("create-all")
def db_create_all(
    confirm: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Create tables directly from the models.

    For local development and tests only - production schema changes go through
    Alembic so they are reviewable and reversible.
    """
    from civicos.core.config import get_settings

    settings = get_settings()
    if settings.is_production and not confirm:
        console.print("[red]Refusing to run create-all in production. Use 'db upgrade'.[/red]")
        raise typer.Exit(code=1)

    async def _create() -> int:
        import civicos.domain  # noqa: F401  (registers the tables)
        from civicos.db.base import Base
        from civicos.db.session import dispose_engine, get_engine

        engine = get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        count = len(Base.metadata.tables)
        await dispose_engine()
        return count

    console.print(f"[green]Created {_run(_create())} tables.[/green]")


@db_app.command("seed")
def db_seed(
    slug: Annotated[str, typer.Option(help="Municipality identifier.")] = "demo",
    name: Annotated[str, typer.Option(help="Display name.")] = "Demo Municipality",
    admin_email: Annotated[str, typer.Option(help="Administrator email.")] = "admin@example.gov",
    admin_password: Annotated[str, typer.Option(help="Administrator password.")] = "ChangeMe123!",
    with_assets: Annotated[bool, typer.Option(help="Add sample assets.")] = True,
) -> None:
    """Create a fully configured municipality for local development."""

    async def _seed() -> str:
        from civicos.db.seed import seed_demo_assets, seed_demo_tenant
        from civicos.db.session import dispose_engine, session_scope

        async with session_scope() as session:
            tenant = await seed_demo_tenant(
                session,
                slug=slug,
                name=name,
                admin_email=admin_email,
                admin_password=admin_password,
            )
            if with_assets:
                await seed_demo_assets(session, tenant)
        await dispose_engine()
        return tenant.slug

    created = _run(_seed())
    console.print(f"[green]Seeded municipality '{created}'.[/green]")
    console.print(f"  Sign in: {admin_email} / {admin_password}")
    console.print(f"  Send requests with header: X-Tenant: {created}")


# -------------------------------------------------------------------- tenant ---


@tenant_app.command("create")
def tenant_create(
    slug: Annotated[str, typer.Argument(help="URL-safe identifier.")],
    name: Annotated[str, typer.Argument(help="Display name.")],
    tier: Annotated[str, typer.Option(help="town, district, union_council...")] = "town",
    timezone: Annotated[str, typer.Option(help="IANA timezone.")] = "UTC",
    language: Annotated[str, typer.Option(help="Default language code.")] = "en",
    currency: Annotated[str, typer.Option(help="ISO currency code.")] = "USD",
) -> None:
    """Onboard a municipality with the default configuration."""

    async def _create() -> dict[str, int]:
        from civicos.core.text import slugify
        from civicos.db.seed import seed_tenant_defaults, tenant_exists
        from civicos.db.session import dispose_engine, session_scope
        from civicos.domain.enums import MunicipalityTier
        from civicos.domain.tenancy import Municipality

        identifier = slugify(slug)
        async with session_scope() as session:
            if await tenant_exists(session, identifier):
                console.print(f"[red]'{identifier}' already exists.[/red]")
                raise typer.Exit(code=1)
            tenant = Municipality(
                slug=identifier,
                name=name,
                tier=MunicipalityTier(tier),
                timezone=timezone,
                default_language=language,
                supported_languages=[language],
                currency_code=currency.upper(),
            )
            session.add(tenant)
            await session.flush()
            created = await seed_tenant_defaults(session, tenant)
        await dispose_engine()
        return created

    created = _run(_create())
    console.print(f"[green]Created municipality '{slug}'.[/green]")
    for key, value in created.items():
        console.print(f"  {key}: {value}")


@tenant_app.command("list")
def tenant_list() -> None:
    """List every municipality on this deployment."""

    async def _list() -> list[tuple[str, str, str, int]]:
        from sqlalchemy import func, select

        from civicos.db.session import dispose_engine, session_scope
        from civicos.domain.issues import Issue
        from civicos.domain.tenancy import Municipality

        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(
                        Municipality.slug,
                        Municipality.name,
                        Municipality.status,
                        func.count(Issue.id),
                    )
                    .outerjoin(Issue, Issue.tenant_id == Municipality.id)
                    .where(Municipality.deleted_at.is_(None))
                    .group_by(Municipality.slug, Municipality.name, Municipality.status)
                    .order_by(Municipality.name)
                )
            ).all()
        await dispose_engine()
        return [(slug, name, str(status), int(count)) for slug, name, status, count in rows]

    table = Table(title="Municipalities")
    table.add_column("Slug")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Reports", justify="right")
    for row in _run(_list()):
        table.add_row(*[str(value) for value in row])
    console.print(table)


@tenant_app.command("create-admin")
def tenant_create_admin(
    slug: Annotated[str, typer.Argument(help="Municipality identifier.")],
    email: Annotated[str, typer.Option(prompt=True)],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True)],
    superadmin: Annotated[bool, typer.Option(help="Grant platform-wide access.")] = False,
) -> None:
    """Create (or promote) an administrator account."""

    async def _create() -> str:
        from civicos.core.permissions import Role
        from civicos.db.seed import ensure_superadmin
        from civicos.db.session import dispose_engine, session_scope
        from civicos.services.auth_service import get_tenant_by_slug, register

        async with session_scope() as session:
            tenant = await get_tenant_by_slug(session, slug)
            if superadmin:
                user = await ensure_superadmin(session, tenant.id, email=email, password=password)
            else:
                user = await register(
                    session,
                    tenant,
                    full_name="Municipal Administrator",
                    email=email,
                    password=password,
                    role=Role.TENANT_ADMIN,
                    created_by_staff=True,
                )
        await dispose_engine()
        return str(user.id)

    console.print(f"[green]Administrator ready: {email} (id {_run(_create())}).[/green]")


@tenant_app.command("api-key")
def tenant_api_key(
    slug: Annotated[str, typer.Argument(help="Municipality identifier.")],
    name: Annotated[str, typer.Option(help="What this key is for.")] = "integration",
    days: Annotated[int, typer.Option(help="Validity in days.")] = 365,
) -> None:
    """Mint an API key. The plaintext is printed once and never stored."""

    async def _issue() -> str:
        from civicos.db.session import dispose_engine, session_scope
        from civicos.services.auth_service import get_tenant_by_slug, issue_api_key

        async with session_scope() as session:
            tenant = await get_tenant_by_slug(session, slug)
            _, plaintext = await issue_api_key(session, tenant, name=name, expires_in_days=days)
        await dispose_engine()
        return plaintext

    console.print("[green]API key created. Store it now - it cannot be shown again:[/green]")
    console.print(f"  {_run(_issue())}")


# ------------------------------------------------------------------ documents ---


@docs_app.command("ingest")
def docs_ingest(
    slug: Annotated[str, typer.Argument(help="Municipality identifier.")],
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    visibility: Annotated[str, typer.Option(help="public, internal or restricted.")] = "internal",
    document_type: Annotated[str, typer.Option(help="bylaw, policy, budget...")] = "other",
    recursive: Annotated[bool, typer.Option(help="Recurse into directories.")] = True,
) -> None:
    """Index documents into a municipality's knowledge base.

    The replacement for the old one-off embedding script: tenant-scoped,
    permission-aware, idempotent, and it reports exactly what failed.
    """

    async def _ingest() -> tuple[int, int, list[str]]:
        from civicos.ai.rag.ingest import checksum, find_by_checksum, ingest_document
        from civicos.db.session import dispose_engine, session_scope
        from civicos.domain.enums import DocumentType, Visibility
        from civicos.domain.knowledge import Document
        from civicos.integrations.storage import get_storage, verify_declared_type
        from civicos.services.auth_service import get_tenant_by_slug

        files = _collect_files(paths, recursive)
        if not files:
            return 0, 0, ["No readable files found."]

        succeeded = failed = 0
        problems: list[str] = []
        storage = get_storage()

        async with session_scope() as session:
            tenant = await get_tenant_by_slug(session, slug)
            for path in files:
                data = path.read_bytes()
                digest = checksum(data)
                if await find_by_checksum(session, tenant.id, digest):
                    problems.append(f"{path.name}: already indexed, skipped")
                    continue

                content_type = verify_declared_type(data, _guess_type(path))
                stored = await storage.save(
                    data,
                    tenant_id=tenant.id,
                    filename=path.name,
                    content_type=content_type,
                    folder="documents",
                )
                document = Document(
                    tenant_id=tenant.id,
                    title=path.stem.replace("_", " ").replace("-", " ").title(),
                    document_type=DocumentType(document_type),
                    visibility=Visibility(visibility),
                    filename=stored.filename,
                    storage_key=stored.key,
                    content_type=content_type,
                    size_bytes=stored.size_bytes,
                    checksum=digest,
                )
                session.add(document)
                await session.flush()

                result = await ingest_document(session, document, data)
                if result.succeeded:
                    succeeded += 1
                    console.print(f"  [green]OK[/green] {path.name} - {result.chunk_count} chunks")
                else:
                    failed += 1
                    problems.append(f"{path.name}: {result.error}")
                    console.print(f"  [red]FAILED[/red] {path.name}: {result.error}")

        await dispose_engine()
        return succeeded, failed, problems

    succeeded, failed, problems = _run(_ingest())
    console.print(f"\n[green]{succeeded} indexed[/green], [red]{failed} failed[/red].")
    for problem in problems:
        console.print(f"  [yellow]{problem}[/yellow]")


def _collect_files(paths: list[Path], recursive: bool) -> list[Path]:
    supported = {".pdf", ".docx", ".txt", ".md", ".csv"}
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            files.extend(
                candidate
                for candidate in sorted(path.glob(pattern))
                if candidate.is_file() and candidate.suffix.lower() in supported
            )
        elif path.is_file() and path.suffix.lower() in supported:
            files.append(path)
    return files


def _guess_type(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
    }.get(path.suffix.lower(), "application/octet-stream")


@docs_app.command("search")
def docs_search(
    slug: Annotated[str, typer.Argument(help="Municipality identifier.")],
    query: Annotated[str, typer.Argument(help="What to look for.")],
    limit: Annotated[int, typer.Option(help="Passages to return.")] = 5,
) -> None:
    """Run a retrieval query from the terminal - useful for tuning the corpus."""

    async def _search() -> list[tuple[str, str, float]]:
        from civicos.ai.rag.retriever import retrieve
        from civicos.db.session import dispose_engine, session_scope
        from civicos.domain.enums import Visibility
        from civicos.services.auth_service import get_tenant_by_slug

        async with session_scope() as session:
            tenant = await get_tenant_by_slug(session, slug)
            result = await retrieve(
                session, tenant.id, query, audience=Visibility.RESTRICTED, top_k=limit
            )
            rows = [
                (
                    passage.document.title,
                    passage.chunk.content[:200].replace("\n", " "),
                    passage.score,
                )
                for passage in result.passages
            ]
        await dispose_engine()
        return rows

    table = Table(title=f"Results for: {query}")
    table.add_column("Document")
    table.add_column("Excerpt")
    table.add_column("Score", justify="right")
    for title, excerpt, score in _run(_search()):
        table.add_row(title, excerpt, f"{score:.3f}")
    console.print(table)


# ----------------------------------------------------------------- operations ---


@ops_app.command("sla-sweep")
def ops_sla_sweep(
    slug: Annotated[str | None, typer.Option(help="Limit to one municipality.")] = None,
) -> None:
    """Re-evaluate SLA states, record breaches and escalate.

    Run every few minutes from cron or a scheduler.
    """
    from civicos.workers.jobs import run_sla_sweep

    result = _run(run_sla_sweep(tenant_slug=slug))
    console.print(
        f"[green]Checked {result['checked']} issue(s); "
        f"{result['breached']} newly breached, {result['escalated']} escalated.[/green]"
    )


@ops_app.command("rollup")
def ops_rollup(
    days: Annotated[int, typer.Option(help="How many past days to recompute.")] = 1,
) -> None:
    """Recompute the daily analytics rollup. Run nightly."""
    from civicos.workers.jobs import run_daily_rollup

    result = _run(run_daily_rollup(days=days))
    console.print(
        f"[green]Rolled up {result['days']} day(s) for {result['tenants']} tenant(s).[/green]"
    )


@ops_app.command("retry-notifications")
def ops_retry_notifications() -> None:
    """Re-attempt failed notification deliveries."""
    from civicos.workers.jobs import run_notification_retry

    result = _run(run_notification_retry())
    console.print(f"[green]{result['delivered']} notification(s) delivered.[/green]")


@ops_app.command("reindex")
def ops_reindex(
    slug: Annotated[str, typer.Argument(help="Municipality identifier.")],
) -> None:
    """Re-embed every indexed document, e.g. after changing embedding model."""
    from civicos.workers.jobs import run_reindex

    result = _run(run_reindex(slug))
    console.print(
        f"[green]{result['reindexed']} document(s) reindexed, {result['failed']} failed.[/green]"
    )


@app.command("check")
def check_config() -> None:
    """Validate configuration and dependencies before a deployment."""

    async def _check() -> dict[str, Any]:
        from civicos.ai.registry import provider_health
        from civicos.db.session import dispose_engine, ping
        from civicos.integrations.storage import get_storage

        results: dict[str, Any] = {"database": await ping()}
        try:
            results["storage"] = await get_storage().health()
        except Exception as exc:
            results["storage"] = f"error: {exc}"
        results["ai"] = await provider_health()
        await dispose_engine()
        return results

    results = _run(_check())
    table = Table(title="Configuration check", show_header=False)
    ok = True
    for key, value in results.items():
        healthy = value is True or (isinstance(value, dict) and any(value.values()))
        ok = ok and (key != "database" or value is True)
        table.add_row(key, f"[green]{value}[/green]" if healthy else f"[yellow]{value}[/yellow]")
    console.print(table)
    if not ok:
        console.print("[red]The database is unreachable - the platform will not serve.[/red]")
        raise typer.Exit(code=1)


def main() -> None:  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
