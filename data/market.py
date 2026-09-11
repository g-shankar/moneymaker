"""Market data access: Alpaca Data API with a yfinance fallback.

Paper-trading context only. Credentials are read from environment
variables (ALPACA_API_KEY / ALPACA_API_SECRET) — never stored in files,
logs, or code. If credentials are absent, the client runs in
yfinance-only mode (headless-safe, stdlib logging only).
"""

import logging
import os
from datetime import date, timedelta

import pandas as pd

log = logging.getLogger(__name__)

_BARS_COLS = ["open", "high", "low", "close", "volume"]


class MarketData:
    """Fetch daily OHLCV bars and latest prices.

    Parameters
    ----------
    feed : str
        Alpaca data feed ("iex" default; "sip" if entitled). Ignored when
        Alpaca credentials are absent and yfinance is used instead.
    """

    def __init__(self, feed: str = "iex"):
        self.feed = feed
        self.key = os.environ.get("ALPACA_API_KEY")
        self.secret = os.environ.get("ALPACA_API_SECRET")
        if not (self.key and self.secret):
            log.warning(
                "ALPACA_API_KEY/ALPACA_API_SECRET not set — "
                "MarketData running in yfinance-only mode."
            )

    # ------------------------------------------------------------------ API
    def get_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Return daily OHLCV bars for *symbol* between *start* and *end*.

        Tries the Alpaca Data API first, then falls back to yfinance on any
        failure. Result has a DatetimeIndex and lowercase columns exactly:
        open, high, low, close, volume. Rows with NaNs are dropped.
        """
        df = None
        if self.key and self.secret:
            df = self._alpaca_bars(symbol, start, end)
        if df is None:
            df = self._yfinance_bars(symbol, start, end)
        return df

    def get_latest_price(self, symbol: str) -> float:
        """Last available close, from bars over the trailing 10 days."""
        today = date.today()
        start = (today - timedelta(days=10)).isoformat()
        df = self.get_bars(symbol, start, today.isoformat())
        if df.empty:
            raise ValueError(f"No bars returned for {symbol}")
        return float(df["close"].iloc[-1])

    # ------------------------------------------------------------- internals
    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        for c in _BARS_COLS:
            if c not in df.columns:
                raise ValueError(f"missing column {c!r} in bars response")
        df = df[_BARS_COLS]
        df.index = pd.to_datetime(df.index)
        df = df.sort_index().dropna()
        return df

    def _alpaca_bars(self, symbol: str, start: str, end: str):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            client = StockHistoricalDataClient(self.key, self.secret)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=self.feed,
            )
            bars = client.get_stock_bars(req)
            frame = bars.df
            if frame is None or frame.empty:
                log.warning("Alpaca returned no bars for %s; trying yfinance", symbol)
                return None
            frame = frame.reset_index()
            frame = frame.rename(columns={"timestamp": "index"}).set_index("index")
            return self._normalize(frame)
        except Exception as exc:  # network, auth, missing alpaca-py, bad payload
            log.warning("Alpaca bars failed for %s (%s); trying yfinance", symbol, exc)
            return None

    def _yfinance_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        try:
            import yfinance as yf

            df = yf.download(
                symbol, start=start, end=end, auto_adjust=False, progress=False
            )
            if df is None or df.empty:
                log.warning("yfinance returned no bars for %s", symbol)
                return pd.DataFrame(columns=_BARS_COLS)
            return self._normalize(df)
        except Exception as exc:
            log.error("yfinance bars failed for %s: %s", symbol, exc)
            return pd.DataFrame(columns=_BARS_COLS)
