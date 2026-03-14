from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Azure Event Hub auth — use ONE of the following:
    # 1. Connection string from Key Vault (thesis/dev)
    eventhub_connection_string: str = ""
    # 2. Namespace + Managed Identity (production/OIDC — no secret needed)
    eventhub_namespace: str = ""   # e.g. thesis-crypto-eh-ns.servicebus.windows.net
    eventhub_name: str = "crypto-prices"

    # Local Kafka (for offline dev)
    use_local_kafka: bool = False
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic: str = "crypto-prices"

    # Data source
    data_source: str = "merged"               # "merged" | "coinbase_ws" | "coingecko_rest"
    symbols: str = "BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD"

    # Throughput control
    batch_size: int = 50                       # events per EventHub batch
    max_events_per_second: int = 0             # 0 = unlimited (as fast as source sends)
    poll_interval: float = 10.0                # seconds between CoinGecko polls

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip() for s in self.symbols.split(",") if s.strip()]
