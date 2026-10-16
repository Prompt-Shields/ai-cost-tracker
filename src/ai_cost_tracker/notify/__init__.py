"""Alert notifications: email, Slack, Teams and signed webhooks (issue #30).

Other modules only create ``AlertEvent`` rows; ``run_notifications`` (per alert) and
``run_daily_digest`` (one summary a day) deliver them to the configured channels.
"""

from ai_cost_tracker.notify.jobs import run_daily_digest, run_notifications
from ai_cost_tracker.notify.senders import NotifyError, send_test

__all__ = ["NotifyError", "run_daily_digest", "run_notifications", "send_test"]
