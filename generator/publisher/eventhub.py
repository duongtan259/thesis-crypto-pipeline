"""
Azure Event Hub publisher.
Batches events and sends them via AMQP using the azure-eventhub SDK.

Supports two auth modes:
  - connection_string: for local dev only (stored in Key Vault, fetched at startup)
  - Managed Identity / OIDC: set connection_string="" and provide fully_qualified_namespace
    Uses DefaultAzureCredential — works with Managed Identity in ACI, or
    Azure CLI credentials locally. No secrets required.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from azure.eventhub.aio import EventHubProducerClient
from azure.eventhub import EventData
from azure.eventhub.exceptions import EventHubError

if TYPE_CHECKING:
    from models.price_event import PriceEvent

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [1, 2, 4]  # seconds between retries (3 attempts total)


class EventHubPublisher:
    def __init__(
        self,
        eventhub_name: str,
        connection_string: str = "",
        fully_qualified_namespace: str = "",
    ):
        self._eventhub_name = eventhub_name
        self._connection_string = connection_string
        self._namespace = fully_qualified_namespace
        self._client: EventHubProducerClient | None = None
        self._sent_total = 0

    async def __aenter__(self):
        if self._connection_string:
            # Key Vault connection string (dev/thesis)
            self._client = EventHubProducerClient.from_connection_string(
                self._connection_string,
                eventhub_name=self._eventhub_name,
            )
            logger.info("EventHub publisher connected via connection string | hub=%s", self._eventhub_name)
        else:
            # Managed Identity / OIDC — no secrets (production)
            from azure.identity.aio import DefaultAzureCredential
            credential = DefaultAzureCredential()
            self._client = EventHubProducerClient(
                fully_qualified_namespace=self._namespace,
                eventhub_name=self._eventhub_name,
                credential=credential,
            )
            logger.info("EventHub publisher connected via Managed Identity | hub=%s", self._eventhub_name)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.close()
            logger.info("EventHub publisher closed | total_sent=%d", self._sent_total)

    async def send_batch(self, events: list["PriceEvent"]) -> int:
        """Send events as one or more batches. Retries up to 3x on transient errors."""
        if not self._client:
            raise RuntimeError("Publisher not started — use async context manager")

        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            try:
                sent = await self._send_once(events)
                self._sent_total += sent
                return sent
            except EventHubError as e:
                if attempt == len(_RETRY_DELAYS):
                    logger.error("EventHub send failed after %d attempts: %s", attempt, e)
                    raise
                logger.warning("EventHub send failed (attempt %d/%d): %s — retrying in %ds",
                               attempt, len(_RETRY_DELAYS), e, delay)
                await asyncio.sleep(delay)
        return 0

    async def _send_once(self, events: list["PriceEvent"]) -> int:
        sent = 0
        batch = await self._client.create_batch()
        for event in events:
            data = EventData(event.to_json_bytes())
            try:
                batch.add(data)
            except ValueError:
                # Batch full — flush and start a new one
                await self._client.send_batch(batch)
                sent += len(batch)
                batch = await self._client.create_batch()
                try:
                    batch.add(data)
                except ValueError:
                    logger.error("Event too large to send — skipping | symbol=%s", event.symbol)
        if len(batch) > 0:
            await self._client.send_batch(batch)
            sent += len(batch)
        return sent
