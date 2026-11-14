"""Notifier/AcsEmailNotifier/LogNotifier/build_notifier seam for outbound email.
azure.communication.email is imported lazily inside AcsEmailNotifier.send so this module stays
importable without the `email` extra installed."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from beehive.email_routing import EmailConfigurationError
from beehive.localization import Localizer

_LOGGER = logging.getLogger(__name__)

# Bounds on one ACS send. The SDK otherwise sleeps for whatever Retry-After ACS returns and polls
# the delivery status with no deadline, so a throttled status endpoint once parked the digest job
# until the jobs worker's watchdog killed it, and every restart resent the same email.
_ACS_CONNECTION_TIMEOUT_SECONDS = 10
_ACS_READ_TIMEOUT_SECONDS = 60
_ACS_REQUEST_RETRIES = 3
_ACS_REQUEST_TIMEOUT_SECONDS = 120
_ACS_MAX_RETRY_AFTER_SECONDS = 30.0
_ACS_STATUS_WAIT_SECONDS = 120.0
# Slack for the status request that is in flight when the polling deadline passes.
_ACS_STATUS_JOIN_GRACE_SECONDS = _ACS_REQUEST_TIMEOUT_SECONDS + 10


class EmailStatusWaitExpired(RuntimeError):
    """ACS accepted the email, but its delivery status did not settle before the deadline."""


def _bounded_retry_policy():
    from azure.core.pipeline.policies import RetryPolicy

    class _BoundedRetryPolicy(RetryPolicy):
        def get_retry_after(self, response):
            delay = super().get_retry_after(response)
            return None if delay is None else min(delay, _ACS_MAX_RETRY_AFTER_SECONDS)

    return _BoundedRetryPolicy(
        retry_total=_ACS_REQUEST_RETRIES,
        retry_backoff_max=int(_ACS_MAX_RETRY_AFTER_SECONDS),
        timeout=_ACS_REQUEST_TIMEOUT_SECONDS,
    )


def _bounded_status_polling(endpoint: str, deadline_seconds: float):
    from azure.core.polling.base_polling import LROBasePolling

    class _BoundedStatusPolling(LROBasePolling):
        """Polls like the SDK default, but caps each Retry-After and stops at a deadline, which
        ends the poller thread instead of leaving it sleeping in the background."""

        def __init__(self) -> None:
            super().__init__(
                5,
                lro_options={"final-state-via": "azure-async-operation"},
                path_format_arguments={"endpoint": endpoint},
            )
            self._deadline = time.monotonic() + deadline_seconds

        def _extract_delay(self) -> float:
            return min(super()._extract_delay(), _ACS_MAX_RETRY_AFTER_SECONDS)

        def _delay(self) -> None:
            delay = self._extract_delay()
            if time.monotonic() + delay > self._deadline:
                raise EmailStatusWaitExpired(
                    f"delivery status still pending after {deadline_seconds:.0f}s")
            self._sleep(delay)

    return _BoundedStatusPolling()


def _connection_string_endpoint(connection_string: str) -> str:
    for part in connection_string.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "endpoint":
            return value.strip().rstrip("/")
    return ""


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, plain_text: str, html: str | None = None,
             *, to_addr: str | None = None) -> None:
        ...


class LogNotifier(Notifier):
    def send(self, subject: str, plain_text: str, html: str | None = None,
             *, to_addr: str | None = None) -> None:
        recipient = f"[TO: {to_addr}] " if to_addr else ""
        print(f"{recipient}[ALERT] {subject}\n{plain_text}")


class AcsEmailNotifier(Notifier):
    def __init__(self, connection_string: str, to_addr: str | None, from_addr: str):
        self._connection_string = connection_string
        self._to_addr = to_addr
        self._from_addr = from_addr

    def send(self, subject: str, plain_text: str, html: str | None = None,
             *, to_addr: str | None = None) -> None:
        recipient = to_addr or self._to_addr
        if recipient is None:
            raise EmailConfigurationError("No email recipient is configured")
        from azure.communication.email import EmailClient
        client = EmailClient.from_connection_string(
            self._connection_string,
            retry_policy=_bounded_retry_policy(),
            connection_timeout=_ACS_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=_ACS_READ_TIMEOUT_SECONDS,
        )
        content = {"subject": subject, "plainText": plain_text}
        if html is not None:
            content["html"] = html
        message = {
            "senderAddress": self._from_addr,
            "recipients": {"to": [{"address": recipient}]},
            "content": content,
        }
        # begin_send returns once ACS has accepted the message; from here on it counts as sent.
        poller = client.begin_send(
            message,
            polling=_bounded_status_polling(
                _connection_string_endpoint(self._connection_string),
                _ACS_STATUS_WAIT_SECONDS,
            ),
        )
        try:
            poller.result(timeout=_ACS_STATUS_WAIT_SECONDS + _ACS_STATUS_JOIN_GRACE_SECONDS)
        except Exception as exc:
            from azure.core.polling.base_polling import OperationFailed
            if isinstance(exc.__cause__, OperationFailed):
                raise
            # ACS already accepted the message; only the status poll failed. Raising here would
            # make the caller resend, and every resend reaches the inbox as a duplicate.
            _LOGGER.warning("ACS accepted email to %s but its status poll failed: %s",
                            recipient, exc)
            return
        if not poller.done():
            _LOGGER.warning("ACS accepted email to %s; stopped waiting for its delivery status",
                            recipient)


def build_notifier(env: dict, default_to_addr: str | None = None) -> Notifier:
    conn = env.get("ACS_CONNECTION_STRING")
    if not conn:
        return LogNotifier()
    return AcsEmailNotifier(
        connection_string=conn,
        to_addr=default_to_addr if default_to_addr is not None
        else env.get("DIGEST_EMAIL_TO"),
        from_addr=env.get("DIGEST_EMAIL_FROM", "beehive@example.com"),
    )


def format_llm_failure(localizer: Localizer, channel_name: str, error: str) -> tuple[str, str]:
    product = localizer.text("common.product_name")
    subject = localizer.text(
        "background.llm_failure_subject", product=product, channel=channel_name)
    body = localizer.text(
        "background.llm_failure_body", channel=channel_name, error=error)
    return subject, body
