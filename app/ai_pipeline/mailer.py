"""SMTP sender with honest outcomes.

``accepted`` means the SMTP server accepted the message for delivery (a 250
reply). It does NOT mean the recipient's inbox received it. Outcomes are
classified so the caller knows whether a retry could create a duplicate:

  * NOT_SUBMITTED - failed before/at the server's refusal; safe to retry
  * UNKNOWN       - connection died mid-transfer; the mail MAY have gone out
"""
from __future__ import annotations

import mimetypes
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .util import get_logger

log = get_logger("mailer")
ACCEPTED, NOT_SUBMITTED, UNKNOWN = "accepted", "not_submitted", "unknown"


@dataclass
class SendOutcome:
    status: str
    detail: str = ""
    message_id: str = ""


class SmtpMailer:
    def __init__(self, settings, timeout: float = 30.0):
        self.s, self.timeout = settings, timeout

    def build(self, to_email: str, to_name: str, subject: str, body: str, attachment: str | Path) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.s.smtp_from_name, self.s.smtp_from)) if self.s.smtp_from_name else self.s.smtp_from
        msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=(self.s.smtp_from.split("@")[-1] or None))
        msg.set_content(body)
        path = Path(attachment)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        return msg

    def send(self, to_email: str, to_name: str, subject: str, body: str, attachment: str | Path) -> SendOutcome:
        problems = self.s.smtp_problems()
        if problems:
            return SendOutcome(NOT_SUBMITTED, "SMTP not configured: " + "; ".join(problems))
        try:
            msg = self.build(to_email, to_name, subject, body, attachment)
        except OSError as exc:
            return SendOutcome(NOT_SUBMITTED, f"could not read attachment: {type(exc).__name__}")
        ctx = ssl.create_default_context()                # certificate + hostname verification stays ON
        try:
            if self.s.smtp_security == "ssl":
                server = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=self.timeout, context=ctx)
            else:
                server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=self.timeout)
                server.starttls(context=ctx)
            server.login(self.s.smtp_user, self.s.smtp_password)
        except smtplib.SMTPAuthenticationError:
            return SendOutcome(NOT_SUBMITTED, "SMTP login rejected (check SMTP_USER / app password)")
        except (smtplib.SMTPException, ssl.SSLError, socket.timeout, OSError) as exc:
            return SendOutcome(NOT_SUBMITTED, f"could not connect/login to SMTP server ({type(exc).__name__})")
        try:
            refused = server.send_message(msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPHeloError) as exc:
            return SendOutcome(NOT_SUBMITTED, f"server refused the message ({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001 - disconnect after DATA: outcome genuinely unknown
            return SendOutcome(UNKNOWN, f"connection failed during transfer ({type(exc).__name__}); the mail may or may not have been sent")
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        if refused:
            return SendOutcome(NOT_SUBMITTED, f"recipient refused: {list(refused)}")
        return SendOutcome(ACCEPTED, "accepted by the SMTP server for delivery", msg["Message-ID"])
