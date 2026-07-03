from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BacktestConfig:
    period_years: int
    evaluation_horizons: tuple[int, ...]
    reentry_cooldown_days: int


@dataclass(frozen=True)
class DataConfig:
    database_path: Path
    yfinance_cache_dir: Path
    jpx_listed_issues_url: str
    benchmark_tickers: dict[str, str]
    price_period: str
    price_interval: str


@dataclass(frozen=True)
class UniverseConfig:
    enable_current_market_cap_filter: bool
    min_current_market_cap: int | None


@dataclass(frozen=True)
class SignalConfig:
    short_trading_value_window: int
    long_trading_value_window: int
    inflow_ratio_threshold: float
    min_consecutive_days: int
    min_long_trading_value: int
    moving_average_window: int
    require_close_above_previous: bool
    close_lookback_days: int


@dataclass(frozen=True)
class ReportConfig:
    output_dir: Path
    language: str


@dataclass(frozen=True)
class DailyConfig:
    enabled: bool
    output_dir: Path
    monitoring_lookback_days: int
    early_monitoring_days: int
    send_empty_email: bool
    price_refresh_lookback_days: int
    notification_history_mode: str


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    use_starttls: bool
    use_ssl: bool
    username_env: str
    password_env: str
    from_env: str
    to_env: str
    cc_env: str | None
    bcc_env: str | None


@dataclass(frozen=True)
class AppConfig:
    backtest: BacktestConfig
    data: DataConfig
    universe: UniverseConfig
    signal: SignalConfig
    report: ReportConfig
    daily: DailyConfig
    email: EmailConfig


class ConfigError(ValueError):
    """Raised when the configuration file is missing required or valid values."""


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration file is not valid JSON: {config_path}") from exc

    config = _parse_config(raw)
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.backtest.period_years <= 0:
        raise ConfigError("backtest.period_years must be greater than 0")
    if not config.backtest.evaluation_horizons:
        raise ConfigError("backtest.evaluation_horizons must not be empty")
    if any(days <= 0 for days in config.backtest.evaluation_horizons):
        raise ConfigError("backtest.evaluation_horizons values must be greater than 0")
    if config.backtest.reentry_cooldown_days < 0:
        raise ConfigError("backtest.reentry_cooldown_days must be greater than or equal to 0")

    if config.signal.short_trading_value_window <= 0:
        raise ConfigError("signal.short_trading_value_window must be greater than 0")
    if config.signal.long_trading_value_window <= 0:
        raise ConfigError("signal.long_trading_value_window must be greater than 0")
    if config.signal.short_trading_value_window >= config.signal.long_trading_value_window:
        raise ConfigError("signal.short_trading_value_window must be less than signal.long_trading_value_window")
    if config.signal.inflow_ratio_threshold <= 0:
        raise ConfigError("signal.inflow_ratio_threshold must be greater than 0")
    if config.signal.min_consecutive_days <= 0:
        raise ConfigError("signal.min_consecutive_days must be greater than 0")
    if config.signal.min_long_trading_value < 0:
        raise ConfigError("signal.min_long_trading_value must be greater than or equal to 0")
    if config.signal.moving_average_window <= 0:
        raise ConfigError("signal.moving_average_window must be greater than 0")
    if config.signal.close_lookback_days <= 0:
        raise ConfigError("signal.close_lookback_days must be greater than 0")

    if config.universe.enable_current_market_cap_filter and config.universe.min_current_market_cap is None:
        raise ConfigError("universe.min_current_market_cap is required when current market cap filter is enabled")
    if config.universe.min_current_market_cap is not None and config.universe.min_current_market_cap < 0:
        raise ConfigError("universe.min_current_market_cap must be greater than or equal to 0")
    if config.report.language != "ja":
        raise ConfigError("report.language must be 'ja' because final Markdown reports are required in Japanese")
    if config.daily.monitoring_lookback_days <= 0:
        raise ConfigError("daily.monitoring_lookback_days must be greater than 0")
    if config.daily.early_monitoring_days <= 0:
        raise ConfigError("daily.early_monitoring_days must be greater than 0")
    if config.daily.early_monitoring_days >= config.daily.monitoring_lookback_days:
        raise ConfigError("daily.early_monitoring_days must be less than daily.monitoring_lookback_days")
    if config.daily.price_refresh_lookback_days <= 0:
        raise ConfigError("daily.price_refresh_lookback_days must be greater than 0")
    if config.daily.notification_history_mode not in {"ticker_event_date", "ticker_event_date_signal"}:
        raise ConfigError("daily.notification_history_mode must be 'ticker_event_date' or 'ticker_event_date_signal'")
    if config.email.smtp_port <= 0:
        raise ConfigError("email.smtp_port must be greater than 0")
    if config.email.use_starttls and config.email.use_ssl:
        raise ConfigError("email.use_starttls and email.use_ssl cannot both be true")


def _parse_config(raw: dict[str, Any]) -> AppConfig:
    backtest = _required_mapping(raw, "backtest")
    data = _required_mapping(raw, "data")
    universe = _required_mapping(raw, "universe")
    signal = _required_mapping(raw, "signal")
    report = _required_mapping(raw, "report")
    daily = raw.get("daily", {})
    email = raw.get("email", {})
    if not isinstance(daily, dict):
        raise ConfigError("daily must be an object")
    if not isinstance(email, dict):
        raise ConfigError("email must be an object")

    return AppConfig(
        backtest=BacktestConfig(
            period_years=_required_int(backtest, "period_years"),
            evaluation_horizons=tuple(_required_int_list(backtest, "evaluation_horizons")),
            reentry_cooldown_days=_required_int(backtest, "reentry_cooldown_days"),
        ),
        data=DataConfig(
            database_path=Path(_required_str(data, "database_path")),
            yfinance_cache_dir=Path(_required_str(data, "yfinance_cache_dir")),
            jpx_listed_issues_url=_required_str(data, "jpx_listed_issues_url"),
            benchmark_tickers=dict(_required_mapping(data, "benchmark_tickers")),
            price_period=_required_str(data, "price_period"),
            price_interval=_required_str(data, "price_interval"),
        ),
        universe=UniverseConfig(
            enable_current_market_cap_filter=_required_bool(universe, "enable_current_market_cap_filter"),
            min_current_market_cap=_optional_int(universe, "min_current_market_cap"),
        ),
        signal=SignalConfig(
            short_trading_value_window=_required_int(signal, "short_trading_value_window"),
            long_trading_value_window=_required_int(signal, "long_trading_value_window"),
            inflow_ratio_threshold=_required_float(signal, "inflow_ratio_threshold"),
            min_consecutive_days=_required_int(signal, "min_consecutive_days"),
            min_long_trading_value=_required_int(signal, "min_long_trading_value"),
            moving_average_window=_required_int(signal, "moving_average_window"),
            require_close_above_previous=_required_bool(signal, "require_close_above_previous"),
            close_lookback_days=_required_int(signal, "close_lookback_days"),
        ),
        report=ReportConfig(
            output_dir=Path(_required_str(report, "output_dir")),
            language=_required_str(report, "language"),
        ),
        daily=DailyConfig(
            enabled=_optional_bool(daily, "enabled", True),
            output_dir=Path(_optional_str(daily, "output_dir", "reports/daily")),
            monitoring_lookback_days=_optional_int_with_default(daily, "monitoring_lookback_days", 63),
            early_monitoring_days=_optional_int_with_default(daily, "early_monitoring_days", 21),
            send_empty_email=_optional_bool(daily, "send_empty_email", False),
            price_refresh_lookback_days=_optional_int_with_default(daily, "price_refresh_lookback_days", 10),
            notification_history_mode=_optional_str(daily, "notification_history_mode", "ticker_event_date"),
        ),
        email=EmailConfig(
            smtp_host=_optional_str(email, "smtp_host", "smtp.gmail.com"),
            smtp_port=_optional_int_with_default(email, "smtp_port", 587),
            use_starttls=_optional_bool(email, "use_starttls", True),
            use_ssl=_optional_bool(email, "use_ssl", False),
            username_env=_optional_str(email, "username_env", "GMAIL_SMTP_USER"),
            password_env=_optional_str(email, "password_env", "GMAIL_APP_PASSWORD"),
            from_env=_optional_str(email, "from_env", "MAIL_FROM"),
            to_env=_optional_str(email, "to_env", "MAIL_TO"),
            cc_env=_optional_str_or_none(email, "cc_env", "MAIL_CC"),
            bcc_env=_optional_str_or_none(email, "bcc_env", "MAIL_BCC"),
        ),
    )


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _optional_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer or null")
    return value


def _optional_int_with_default(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    return value


def _required_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    return float(value)


def _optional_str(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _optional_str_or_none(data: dict[str, Any], key: str, default: str | None) -> str | None:
    value = data.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string or null")
    return value


def _optional_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number or null")
    return float(value)


def _required_int_list(data: dict[str, Any], key: str) -> list[int]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise ConfigError(f"{key} must contain only integers")
    return value
