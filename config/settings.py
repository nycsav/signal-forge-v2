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

    # Ollama — upgraded 2026-04-16
    # Primary: Qwen3.5 (262K context, hybrid thinking) > Qwen3 14B > Gemma3 12B
    # Fast: DeepSeek R1 8B 0528 (reasoning at 8B) > Llama 3.2 3B
    ollama_host: str = "http://localhost:11434"
    # Models — stable config (reverted 2026-04-17 stabilization)
    ollama_host: str = "http://localhost:11434"
    deepseek_model: str = "qwen3:14b"   # Primary analyst
    fast_model: str = "llama3.2:3b"     # Pre-filter + sanity check

    # Optional
    perplexity_api_key: str = ""
    whale_alert_api_key: str = ""
    cryptoquant_api_key: str = ""
    nansen_api_key: str = ""
    arkham_api_key: str = ""
    cmc_api_key: str = ""  # Free — apply at intel.arkm.com/api

    # Infrastructure
    redis_url: str = "redis://localhost:6379"
    database_path: str = "/Users/sav/signal-forge-v2/data/trades.db"
    dashboard_port: int = 8000
    log_level: str = "INFO"

    # Trading parameters
    min_signal_score: float = 55.0
    scan_interval_seconds: int = 300   # 5 min scan (was 15 min)
    monitor_interval_seconds: int = 120  # 2 min exit checks (was 5 min)
    max_open_positions: int = 5

    # Watchlist — Top 50 by market cap
    watchlist: list[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD",
        "ADA-USD", "AVAX-USD", "DOGE-USD", "DOT-USD", "LINK-USD",
        "UNI-USD", "ATOM-USD", "LTC-USD", "NEAR-USD", "APT-USD",
        "ARB-USD", "OP-USD", "FIL-USD", "INJ-USD", "SUI-USD",
        "MATIC-USD", "AAVE-USD", "RENDER-USD", "FET-USD", "TIA-USD",
        "SEI-USD", "STX-USD", "IMX-USD", "PEPE-USD", "WIF-USD",
        "BONK-USD", "FLOKI-USD", "SHIB-USD", "TRX-USD", "XLM-USD",
        "HBAR-USD", "VET-USD", "ALGO-USD", "ICP-USD", "FTM-USD",
        "EOS-USD", "SAND-USD", "MANA-USD", "GRT-USD", "CRV-USD",
        "MKR-USD", "COMP-USD", "SNX-USD", "RUNE-USD", "ONDO-USD",
    ]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
