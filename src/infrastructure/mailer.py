from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr
import smtplib
from typing import Any


def send_mail(mail_cfg: dict[str, Any], subject: str, body: str) -> None:
    """Send a plain-text alert through SMTP with STARTTLS."""
    host = str(mail_cfg.get("smtp_host") or "").strip()
    port = int(mail_cfg.get("smtp_port") or 0)
    user = str(mail_cfg.get("user") or "").strip()
    password = str(mail_cfg.get("password") or "")
    sender_name = str(mail_cfg.get("from_name") or "").strip()
    sender = str(mail_cfg.get("from") or "").strip()
    recipients = [
        address.strip()
        for address in str(mail_cfg.get("to") or "").split(",")
        if address.strip()
    ]
    missing = [
        name
        for name, value in (
            ("smtp_host", host),
            ("smtp_port", port),
            ("user", user),
            ("password", password),
            ("from", sender),
            ("to", recipients),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Missing availability mail setting(s): {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(message, from_addr=sender, to_addrs=recipients)
