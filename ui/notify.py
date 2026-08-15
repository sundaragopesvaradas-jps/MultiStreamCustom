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


def _settings(get_secret: GetOptional) -> tuple[str, str, str, int, str]:
    user = (get_secret("smtp-user") or "").strip()
    # Google displays App Passwords in groups of four; the spaces are cosmetic
    # and a pasted copy that keeps them is rejected.
    password = (get_secret("smtp-password") or "").replace(" ", "").strip()
    host = (get_secret("smtp-host") or "").strip() or "smtp.gmail.com"
    try:
        port = int((get_secret("smtp-port") or "587").strip())
    except ValueError:
        port = 587
    mail_from = (get_secret("smtp-from") or "").strip() or user
    return user, password, host, port, mail_from


def check_credentials(get_secret: GetOptional) -> tuple[bool, str]:
    """Log in to the mail server without sending. Returns (ok, human detail)."""
    user, password, host, port, _ = _settings(get_secret)
    if not user or not password:
        return False, "smtp-user or smtp-password is not set in Key Vault."
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, password)
    except smtplib.SMTPAuthenticationError as exc:
        if exc.smtp_code == 535:
            return False, (
                f"{host} rejected the credentials for {user}. Generate a fresh "
                "App Password at Google Account \u203a Security \u203a App passwords "
                "(2-Step Verification must stay on — turning it off deletes every "
                "App Password)."
            )
        return False, f"{host} refused the login ({exc.smtp_code})."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not reach {host}:{port} — {exc}"
    return True, f"{host} accepted {user}."


def send_alert(get_secret: GetOptional, *, subject: str, body: str) -> bool:
    """Send a plain-text alert. Returns True if sent, False if skipped/failed."""
    user, password, host, port, mail_from = _settings(get_secret)
    if not user or not password:
        log.warning("SMTP not configured (smtp-user / smtp-password) — alert not sent: %s", subject)
        return False

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
    except smtplib.SMTPAuthenticationError as exc:
        log.error(
            "Alert not sent (%s rejected %s): %s — set a new App Password in the "
            "owner console under Email alerts.",
            host,
            user,
            exc.smtp_code,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send alert email: %s", exc)
        return False
