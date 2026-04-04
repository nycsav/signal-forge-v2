"""Signal Forge v2 — Central Configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Portfolio
    portfolio_value: float = 100000.0
    mode: str = "paper"  # "paper" or "live"

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_secret_key: str = ""  # alias used in v1 .env
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Coinbase
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""

    # altFINS
    altfins_api_key: str = ""

    # Ollama
    ollama_host: str = "http://localhost:11434"
    deepseek_model: str = "deepseek-r1:14b"
    fast_model: str = "llama3.2:3b"

    # Optional
    perplexity_api_key: str = ""
    whale_alert_api_key: str = ""
    cryptoquant_api_key: str = ""
    nansen_api_key: str = ""

    # Infrastructure
    redis_url: str = "redis://localhost:6379"
    database_path: str = "/Users/sav/signal-forge-v2/data/trades.db"
    dashboard_port: int = 8000
    log_level: str = "INFO"

    # Trading parameters
    min_signal_score: float = 55.0
    scan_interval_seconds: int = 900
    monitor_interval_seconds: int = 300
    max_open_positions: int = 5

    # Watchlist
    watchlist: list[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
        "AVAX-USD", "DOGE-USD", "DOT-USD", "LINK-USD", "UNI-USD",
        "ATOM-USD", "LTC-USD", "NEAR-USD", "APT-USD", "ARB-USD",
        "OP-USD", "FIL-USD", "INJ-USD", "SUI-USD",
    ]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
