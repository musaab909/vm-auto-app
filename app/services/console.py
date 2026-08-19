"""Console session handling.

A WebMKS ticket is a bearer credential for a VM's screen and keyboard: anyone
holding it can type at the console of somebody else's server. It was previously
returned straight to the caller and the `ConsoleTicket` row's `single_use` and
`expires_at` columns were never read, so a ticket that leaked into a log, a proxy
cache or a shared screenshot stayed usable.

Now the API returns an opaque signed *handle* instead. The handle is exchanged
once, under a row lock, for the real connection details; a second exchange gets
nothing. The signature means a handle cannot be forged, and the row means it
cannot be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.drivers.base import HypervisorDriver
from app.models.compute import ConsoleTicket, Instance
from app.models.enums import InstanceStatus
from app.security import load_value, sign_value, utcnow
from app.services import audit
from app.services.errors import DriverError, InvalidStateError, NotFoundError

CONSOLE_SALT = "console-handle"
TTL_SECONDS = 120


@dataclass(slots=True)
class ConsoleSessionDetails:
    """What a redeemed handle yields. Never persisted beyond the ticket row."""

    instance_id: str
    ticket: str
    host: str | None
    port: int | None
    url: str | None
    mode: str
    viewer_url: str | None


def mint(
    session: Session,
    *,
    instance: Instance,
    user_id: str,
    driver: HypervisorDriver,
    source_ip: str | None = None,
) -> tuple[str, ConsoleTicket]:
    """Acquire a console ticket and return an opaque single-use handle for it."""
    if instance.status is not InstanceStatus.RUNNING:
        raise InvalidStateError("The console is only available while the server is running")
    if not instance.hypervisor_ref:
        raise InvalidStateError("This server has no hypervisor reference yet")

    try:
        console = driver.get_console(instance.hypervisor_ref)
    except DriverError as exc:
        raise InvalidStateError(f"Could not open a console session: {exc.message}") from exc

    now = utcnow()
    record = ConsoleTicket(
        instance_id=instance.id,
        issued_to_user_id=user_id,
        ticket=console.ticket,
        host=console.host,
        port=console.port,
        url=console.url,
        created_at=now,
        expires_at=now + timedelta(seconds=TTL_SECONDS),
        single_use=True,
    )
    session.add(record)
    session.flush()

    audit.record(
        session,
        action="instance.console_opened",
        actor_user_id=user_id,
        account_id=instance.account_id,
        target_type="instance",
        target_id=instance.id,
        source_ip=source_ip,
    )
    # The session runs with autoflush off, so the audit row would otherwise stay
    # pending and invisible to a query in the same transaction.
    session.flush()

    # Signed rather than the bare row id: an unsigned id would be guessable, and
    # the signature lets an invalid handle be rejected before touching the
    # database at all.
    return sign_value(record.id, CONSOLE_SALT), record


def redeem(session: Session, handle: str, *, user_id: str) -> ConsoleSessionDetails:
    """Exchange a handle for connection details, exactly once.

    The row is locked before `used_at` is checked and set, so two simultaneous
    redemptions cannot both succeed.
    """
    ticket_id = load_value(handle, CONSOLE_SALT, TTL_SECONDS)
    if ticket_id is None:
        raise NotFoundError("That console link is invalid or has expired")

    record = session.execute(
        select(ConsoleTicket).where(ConsoleTicket.id == ticket_id).with_for_update()
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("That console link is invalid or has expired")

    # A handle issued to one user must not work for another, even within the same
    # account: the audit log names an individual, not an organisation.
    if record.issued_to_user_id != user_id:
        raise NotFoundError("That console link is invalid or has expired")

    if record.expires_at <= utcnow():
        raise InvalidStateError("That console link has expired. Open the console again.")

    if record.single_use and record.used_at is not None:
        raise InvalidStateError("That console link has already been used. Open the console again.")

    record.used_at = utcnow()
    session.flush()

    settings = get_settings()
    return ConsoleSessionDetails(
        instance_id=record.instance_id,
        ticket=record.ticket,
        host=record.host,
        port=record.port,
        url=record.url,
        mode=settings.console_mode,
        viewer_url=_viewer_url(record),
    )


def _viewer_url(record: ConsoleTicket) -> str | None:
    """The URL a browser should open, for the configured console mode.

    `vcenter` hands off to vCenter's own HTML5 console, which needs no
    proprietary JavaScript and is the mode that works without extra assets. It
    does expose the vCenter hostname to the customer; operators who object should
    use `webmks` and supply the SDK.
    """
    settings = get_settings()
    if settings.console_mode != "vcenter":
        return None
    if not settings.vsphere_host:
        return None
    return (
        f"https://{settings.vsphere_host}/ui/webconsole.html"
        f"?vmId={record.instance_id}&vmName=console&serverGuid="
        f"&host={settings.vsphere_host}&sessionTicket={record.ticket}"
    )


def purge_expired(session: Session, *, older_than_hours: int = 24) -> int:
    """Delete spent and expired tickets.

    They are useless once expired, and each row holds a (now-invalid) credential;
    keeping them forever is storage nobody reads and residue nobody wants.
    """
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    # Ids are selected first rather than reading `rowcount` off the DELETE:
    # SQLAlchemy types that result as a plain `Result`, and the count is worth
    # reporting accurately in the maintenance log.
    doomed = (
        session.execute(select(ConsoleTicket.id).where(ConsoleTicket.expires_at < cutoff))
        .scalars()
        .all()
    )
    if not doomed:
        return 0

    session.execute(delete(ConsoleTicket).where(ConsoleTicket.id.in_(doomed)))
    session.flush()
    return len(doomed)
