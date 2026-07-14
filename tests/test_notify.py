from unittest.mock import MagicMock, patch

import pytest

from beehive.email_routing import EmailConfigurationError
from beehive.notify import (AcsEmailNotifier, LogNotifier, build_notifier,
                                format_llm_failure)


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


def test_format_llm_failure():
    subject, body = format_llm_failure("NZ Finance", "timeout after 120s")
    assert "NZ Finance" in subject
    assert "timeout after 120s" in body


def test_format_llm_failure_subject_includes_product_name():
    subject, _ = format_llm_failure("NZ Finance", "timeout after 120s")
    assert "蜂巢" in subject


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
