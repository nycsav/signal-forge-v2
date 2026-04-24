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
    min_signal_score: float = 62.0  # aligned with RiskAgent MIN_SIGNAL_SCORE_FLOOR
    scan_interval_seconds: int = 600   # 10 min — gives 70s LLM pipeline room, reduces overtrading
    monitor_interval_seconds: int = 120  # 2 min exit checks
    max_open_positions: int = 5

    # Watchlist — restricted to high-liquidity, low-spread majors
    watchlist: list[str] = ["BTC-USD", "ETH-USD", "SOL-USD"]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
