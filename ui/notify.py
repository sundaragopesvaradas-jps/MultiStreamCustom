"""Email alerts for MultiStream (login lockouts, etc.).

Uses Gmail SMTP by default. Store these Key Vault secrets:
  smtp-user      — full Gmail address used to send
  smtp-password  — Gmail App Password (not the account password)
Optional:
  smtp-host      — default smtp.gmail.com
  smtp-port      — default 587
  smtp-from      — default = smtp-user
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

log = logging.getLogger("multistream.notify")

ALERT_TO = "sundaragopesvaradas.jps@gmail.com"
GetOptional = Callable[[str], str | None]


def send_alert(get_secret: GetOptional, *, subject: str, body: str) -> bool:
    """Send a plain-text alert. Returns True if sent, False if skipped/failed."""
    user = get_secret("smtp-user")
    password = get_secret("smtp-password")
    if not user or not password:
        log.warning("SMTP not configured (smtp-user / smtp-password) — alert not sent: %s", subject)
        return False

    host = get_secret("smtp-host") or "smtp.gmail.com"
    port_raw = get_secret("smtp-port") or "587"
    try:
        port = int(port_raw)
    except ValueError:
        port = 587
    mail_from = get_secret("smtp-from") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ALERT_TO
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
        log.info("Alert emailed to %s: %s", ALERT_TO, subject)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send alert email: %s", exc)
        return False
