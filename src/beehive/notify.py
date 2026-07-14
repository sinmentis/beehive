"""Notifier/AcsEmailNotifier/LogNotifier/build_notifier seam for outbound email.
azure.communication.email is imported lazily inside AcsEmailNotifier.send so this module stays
importable without the `email` extra installed."""
from __future__ import annotations

from abc import ABC, abstractmethod

from beehive.email_routing import EmailConfigurationError


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
        client = EmailClient.from_connection_string(self._connection_string)
        content = {"subject": subject, "plainText": plain_text}
        if html is not None:
            content["html"] = html
        message = {
            "senderAddress": self._from_addr,
            "recipients": {"to": [{"address": recipient}]},
            "content": content,
        }
        poller = client.begin_send(message)
        poller.result()


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


def format_llm_failure(channel_name: str, error: str) -> tuple[str, str]:
    subject = f"蜂巢：{channel_name} AI 排序失败"
    body = (f"Channel「{channel_name}」这一轮 AI 排序/摘要调用失败，本轮跳过，"
             f"其余 Channel 正常。\n\n错误信息：{error}")
    return subject, body
