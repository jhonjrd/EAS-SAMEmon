"""
integrations.py — Home Assistant & External Alert Integrations

Provides AlertDispatcher, which fans out decoded EAS/SASMEX alerts to:
  - WebhookProvider  : HTTP POST JSON to a Home Assistant webhook URL

Design notes
------------
- Provider runs in a background daemon thread so it never blocks the DSP
  pipeline.
- A WebhookProvider simply POSTs the JSON payload to the configured URL,
  making it compatible with any Home Assistant Webhook automation trigger.
"""

import logging
import threading
import requests

log = logging.getLogger('EAS-SAMEmon.Integrations')


# ---------------------------------------------------------------------------
# AlertDispatcher
# ---------------------------------------------------------------------------

class AlertDispatcher:
    """
    Main entry point for external notifications.
    Manages a collection of providers based on the configuration.
    """

    def __init__(self, config: dict):
        self._providers: list = []
        self.reconfig(config)

    def reconfig(self, config: dict):
        """Rebuild active providers from new configuration dict."""
        # Cleanly stop any existing providers before replacing them
        for p in self._providers:
            if hasattr(p, 'stop'):
                try:
                    p.stop()
                except Exception:
                    pass
        self._providers = []

        webhook_cfg = config.get('webhook', {})
        if webhook_cfg.get('enabled') and webhook_cfg.get('url'):
            log.info(f"Webhook integration enabled: {webhook_cfg['url']}")
            self._providers.append(WebhookProvider(webhook_cfg))

    def dispatch(self, alert_data: dict):
        """
        Send alert_data to all active providers in background threads.
        alert_data is a copy so mutations in one provider don't affect others.
        """
        for provider in self._providers:
            payload = dict(alert_data)
            t = threading.Thread(
                target=provider.send,
                args=(payload,),
                name=f'alert-{provider.__class__.__name__}',
                daemon=True,
            )
            t.start()

    def stop(self):
        """Gracefully stop all providers (call on pipeline shutdown)."""
        for p in self._providers:
            if hasattr(p, 'stop'):
                try:
                    p.stop()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# WebhookProvider
# ---------------------------------------------------------------------------

class WebhookProvider:
    """Sends a JSON POST request to a Home Assistant Webhook URL."""

    def __init__(self, config: dict):
        self.url     = config['url']
        self.timeout = config.get('timeout', 10)

    def send(self, data: dict):
        try:
            log.debug(f"Sending Webhook → {self.url}")
            resp = requests.post(self.url, json=data, timeout=self.timeout)
            if resp.status_code >= 400:
                log.warning(f"Webhook returned {resp.status_code}: {self.url}")
            else:
                log.info(f"Webhook delivered ({resp.status_code}): {self.url}")
        except requests.exceptions.Timeout:
            log.error(f"Webhook timed out after {self.timeout}s: {self.url}")
        except Exception as e:
            log.error(f"Webhook error: {e}")
