"""Operational alerts by email.

Two transports behind one interface:

  smtp  -- the client's relay. No Graph permission, nothing to consent to.
  graph -- Microsoft Graph sendMail. Requires the Mail.Send APPLICATION
           permission, which lets the app send as ANY mailbox in the tenant.
           Microsoft's mitigation is an ApplicationAccessPolicy limiting it
           to one mailbox; that is extra configuration for their admin.

SMTP is the default: one alert a day does not justify tenant-wide send-as
rights on a healthcare tenant.
"""
import re
import smtplib
from email.message import EmailMessage

import config


class AlertError(Exception):
    pass


def recipients(value=None):
    """ALERT_EMAIL as a list. Accepts one address or several, comma or
    semicolon separated, so alerts can reach a person as well as a mailbox."""
    raw = value if value is not None else config.ALERT_EMAIL
    parts = re.split(r"[,;]", raw or "")
    return [p.strip() for p in parts if p.strip()]


def send(subject, body, to=None, transport=None, graph_client=None):
    addresses = recipients(to)
    if not addresses:
        raise AlertError("ALERT_EMAIL is not set -- nobody would be told")
    transport = transport or config.ALERT_TRANSPORT
    if transport == "graph":
        return _send_graph(subject, body, addresses, graph_client)
    return _send_smtp(subject, body, addresses)


def _send_smtp(subject, body, addresses):
    if not config.SMTP_HOST:
        raise AlertError("SMTP_HOST is not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.ALERT_FROM or config.SMTP_USER
    msg["To"] = ", ".join(addresses)
    msg.set_content(body)

    server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    try:
        server.starttls()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        server.quit()
    return True


def _send_graph(subject, body, addresses, graph_client):
    if graph_client is None:
        # Callers that only want to send an email should not have to know how
        # Graph authentication works. reconcile.py calls send() with no client.
        from graph_client import GraphClient, make_auth
        graph_client = GraphClient(make_auth())
    sender = config.ALERT_FROM
    if not sender:
        raise AlertError("ALERT_FROM must name a mailbox in the tenant")
    resp = graph_client.request(
        "POST", "/users/{}/sendMail".format(sender),
        json={"message": {"subject": subject,
                          "body": {"contentType": "Text", "content": body},
                          "toRecipients": [{"emailAddress": {"address": a}}
                                           for a in addresses]},
              "saveToSentItems": False})
    if resp.status_code not in (200, 202):
        raise AlertError("sendMail failed: {} {}".format(
            resp.status_code, resp.text[:200]))
    return True


def gap_report(day, missing, checked, no_transcript):
    """The body of a reconciliation alert."""
    lines = [
        "Zoom Phone transcript pipeline -- reconciliation gap",
        "",
        "Date checked        : {}".format(day),
        "Recordings with a transcript in Zoom : {}".format(checked),
        "Delivered to SharePoint              : {}".format(checked - len(missing)),
        "MISSING                              : {}".format(len(missing)),
        "Recordings with no transcript (ignored): {}".format(no_transcript),
        "",
        "Missing recording IDs:",
    ]
    lines.extend("  {}".format(i) for i in sorted(missing)[:200])
    if len(missing) > 200:
        lines.append("  ... and {} more".format(len(missing) - 200))
    lines += ["", "Re-run:  python3 pipeline.py --from {0} --to {0}".format(day)]
    return "\n".join(lines)


def main():
    """Send one test email, so alerting can be proved before go-live.

        python3 alerts.py                      # to ALERT_EMAIL
        python3 alerts.py someone@example.com  # to a specific address
    """
    import sys
    to = sys.argv[1] if len(sys.argv) > 1 else config.ALERT_EMAIL

    print("\n  transport : {}".format(config.ALERT_TRANSPORT))
    print("  from      : {}".format(config.ALERT_FROM or "(not set)"))
    print("  to        : {}\n".format(", ".join(recipients(to))))

    if not config.ALERT_FROM:
        print("  ALERT_FROM is not set -- nothing can be sent.\n")
        return 1

    try:
        send("Zoom transcript pipeline -- test alert",
             "This is a test from the Zoom Phone transcript pipeline.\n"
             "If you are reading this, alerting works. No action needed.\n",
             to=to)
    except AlertError as exc:
        print("  FAILED: {}\n".format(exc))
        text = str(exc)
        if "ErrorAccessDenied" in text or "403" in text:
            print("  403 usually means the Mail.Send permission is missing, or")
            print("  an ApplicationAccessPolicy excludes this mailbox.\n")
        elif "ResourceNotFound" in text or "404" in text:
            print("  404 means {!r} is not a real mailbox in the"
                  "\n  tenant. A distribution list or alias will not work --"
                  "\n  sendMail needs a licensed mailbox.\n".format(config.ALERT_FROM))
        return 1

    print("  sent. Check the inbox.\n")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
