from unittest.mock import MagicMock, patch

import pytest

from beehive.email_routing import EmailConfigurationError
from beehive.localization import localizer_for
from beehive.notify import (AcsEmailNotifier, LogNotifier, build_notifier,
                                format_llm_failure)

_EN = localizer_for("en")


def test_log_notifier_prints(capsys):
    LogNotifier().send("subject", "body")
    captured = capsys.readouterr()
    assert "subject" in captured.out
    assert "body" in captured.out


def test_build_notifier_falls_back_to_log_without_acs_conn():
    notifier = build_notifier({})
    assert isinstance(notifier, LogNotifier)


def test_build_notifier_returns_acs_notifier_with_conn_string():
    notifier = build_notifier({"ACS_CONNECTION_STRING": "endpoint=https://x;accesskey=y"})
    assert isinstance(notifier, AcsEmailNotifier)


def test_acs_email_notifier_send_calls_sdk():
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr="you@example.com", from_addr="beehive@example.com")
    fake_client = MagicMock()
    fake_poller = MagicMock()
    fake_client.begin_send.return_value = fake_poller
    with patch("azure.communication.email.EmailClient.from_connection_string",
               return_value=fake_client):
        notifier.send("subject", "body")
    fake_client.begin_send.assert_called_once()
    message = fake_client.begin_send.call_args[0][0]
    assert message["recipients"]["to"][0]["address"] == "you@example.com"
    fake_poller.result.assert_called_once()


def _poll_error(cause: Exception) -> Exception:
    from azure.core.exceptions import HttpResponseError
    try:
        raise HttpResponseError(message="(Denied) Denied by the resource provider.") from cause
    except HttpResponseError as exc:
        return exc


def _acs_send_with_poll_error(error: Exception):
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr="you@example.com", from_addr="beehive@example.com")
    fake_client = MagicMock()
    fake_client.begin_send.return_value.result.side_effect = error
    with patch("azure.communication.email.EmailClient.from_connection_string",
               return_value=fake_client):
        notifier.send("subject", "body")


def test_acs_send_treats_failed_status_poll_as_sent():
    from azure.core.polling.base_polling import BadStatus
    # A 401 on the status GET after ACS accepted the POST must not raise, or callers resend.
    _acs_send_with_poll_error(_poll_error(BadStatus("Invalid return status 401 for 'GET'")))


def test_acs_send_raises_when_acs_reports_the_send_failed():
    from azure.core.exceptions import HttpResponseError
    from azure.core.polling.base_polling import OperationFailed
    with pytest.raises(HttpResponseError):
        _acs_send_with_poll_error(_poll_error(OperationFailed("Operation failed or canceled")))


def test_format_llm_failure():
    subject, body = format_llm_failure(_EN, "NZ Finance", "timeout after 120s")
    assert "NZ Finance" in subject
    assert "timeout after 120s" in body


def test_format_llm_failure_subject_includes_product_name():
    subject, _ = format_llm_failure(_EN, "NZ Finance", "timeout after 120s")
    assert "Beehive" in subject


def test_format_llm_failure_localizes_for_a_non_english_language():
    chinese = localizer_for("zh-CN")
    subject, body = format_llm_failure(chinese, "NZ Finance", "timeout after 120s")
    assert "蜂巢" in subject
    assert "NZ Finance" in subject
    assert "timeout after 120s" in body  # raw error detail is preserved untranslated


def test_acs_email_notifier_send_includes_html_when_provided():
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr="you@example.com", from_addr="beehive@example.com")
    fake_client = MagicMock()
    fake_poller = MagicMock()
    fake_client.begin_send.return_value = fake_poller
    with patch("azure.communication.email.EmailClient.from_connection_string",
               return_value=fake_client):
        notifier.send("subject", "plain body", html="<p>html body</p>")
    message = fake_client.begin_send.call_args[0][0]
    assert message["content"]["plainText"] == "plain body"
    assert message["content"]["html"] == "<p>html body</p>"


def test_acs_email_notifier_send_omits_html_key_when_not_provided():
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr="you@example.com", from_addr="beehive@example.com")
    fake_client = MagicMock()
    fake_poller = MagicMock()
    fake_client.begin_send.return_value = fake_poller
    with patch("azure.communication.email.EmailClient.from_connection_string",
               return_value=fake_client):
        notifier.send("subject", "plain body")
    message = fake_client.begin_send.call_args[0][0]
    assert "html" not in message["content"]


def test_explicit_recipient_overrides_notifier_default():
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr="default@example.com",
        from_addr="beehive@example.com")
    fake_client = MagicMock()
    fake_client.begin_send.return_value = MagicMock()
    with patch(
        "azure.communication.email.EmailClient.from_connection_string",
        return_value=fake_client,
    ):
        notifier.send("subject", "body", to_addr="channel@example.com")
    message = fake_client.begin_send.call_args.args[0]
    assert message["recipients"]["to"] == [{"address": "channel@example.com"}]


def test_acs_send_rejects_missing_default_and_explicit_recipient():
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x;accesskey=y",
        to_addr=None,
        from_addr="beehive@example.com")
    with pytest.raises(EmailConfigurationError, match="recipient"):
        notifier.send("subject", "body")


def test_build_notifier_has_no_hardcoded_personal_recipient():
    notifier = build_notifier({
        "ACS_CONNECTION_STRING": "endpoint=https://x;accesskey=y",
        "DIGEST_EMAIL_FROM": "beehive@example.com",
    })
    assert notifier._to_addr is None


def test_log_notifier_prints_explicit_recipient(capsys):
    LogNotifier().send("subject", "body", to_addr="channel@example.com")
    assert "[TO: channel@example.com]" in capsys.readouterr().out


def test_acs_send_bounds_the_status_wait_and_retries():
    from azure.core.polling.base_polling import LROBasePolling
    notifier = AcsEmailNotifier(
        connection_string="endpoint=https://x.example.com/;accesskey=y",
        to_addr="you@example.com", from_addr="beehive@example.com")
    fake_client = MagicMock()
    with patch("azure.communication.email.EmailClient.from_connection_string",
               return_value=fake_client) as factory:
        notifier.send("subject", "body")
    kwargs = factory.call_args.kwargs
    assert kwargs["retry_policy"].total_retries == 3
    assert kwargs["read_timeout"] > 0 and kwargs["connection_timeout"] > 0
    polling = fake_client.begin_send.call_args.kwargs["polling"]
    assert isinstance(polling, LROBasePolling)
    assert polling._path_format_arguments == {"endpoint": "https://x.example.com"}
    timeout = fake_client.begin_send.return_value.result.call_args.kwargs["timeout"]
    assert 0 < timeout < 30 * 60  # always well inside the jobs worker's digest limit


def test_bounded_polling_caps_retry_after_and_stops_at_its_deadline():
    from beehive.notify import EmailStatusWaitExpired, _bounded_status_polling
    polling = _bounded_status_polling("https://x.example.com", deadline_seconds=60)
    response = MagicMock()
    response.http_response.headers = {"Retry-After": "3600"}
    polling._pipeline_response = response
    assert polling._extract_delay() == 30.0

    slept = []
    polling._sleep = slept.append
    polling._delay()
    assert slept == [30.0]
    polling._deadline -= 60
    with pytest.raises(EmailStatusWaitExpired):
        polling._delay()


def test_bounded_retry_policy_caps_retry_after():
    from beehive.notify import _bounded_retry_policy
    policy = _bounded_retry_policy()
    response = MagicMock()
    response.http_response.headers = {"Retry-After": "3600"}
    assert policy.get_retry_after(response) == 30.0
    response.http_response.headers = {}
    assert policy.get_retry_after(response) is None


def test_acs_send_treats_an_expired_status_wait_as_sent():
    from beehive.notify import EmailStatusWaitExpired
    _acs_send_with_poll_error(EmailStatusWaitExpired("delivery status still pending"))
