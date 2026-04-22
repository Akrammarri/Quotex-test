"""
HeavySignalBot — Automated trading bot for the Quotex platform.
Uses pyquotex (stable_api) with full 5-indicator confluence validation:
  RSI · MACD · Bollinger Bands · Market Sentiment · Quotex Internal Signal

Features from library docs integrated here:
  - Robust reconnection loop             (7__WebSocket.md)
  - get_available_asset() open-check     (9__Basic_Examples.md)
  - start_realtime_price() live price    (7__WebSocket.md)
  - edit_practice_balance() auto-refill  (3__Trading_Operations.md)
  - get_profile() startup display        (9__Basic_Examples.md)
  - get_payout_by_asset() quality filter (3__Trading_Operations.md)
  - client.close() graceful shutdown     (9__Basic_Examples.md)
  - Credentials from env vars            (9__Basic_Examples.md security note)
"""

import asyncio
import logging
import os

from pyquotex.stable_api import Quotex

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
MIN_PAYOUT_PCT   = 75     # Skip asset if 1-min payout is below this %
MIN_DEMO_BALANCE = 50.0   # Auto-refill demo balance when it drops below this
REFILL_AMOUNT    = 5000   # Amount to top-up demo balance with
TRADE_AMOUNT     = 10     # USD per trade
LOOP_SLEEP       = 5      # Seconds to wait between full scan cycles
MAX_RETRIES      = 5      # Max connection attempts before giving up


class HeavySignalBot:

    def __init__(self, email: str, password: str):
        self.client = Quotex(email=email, password=password, lang="en")
        self.running = False
        self.assets  = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc"]
        self.trade_amount = TRADE_AMOUNT

    # ── Connection helpers ─────────────────────────────────────────────────────

    async def connect_with_retries(self) -> bool:
        """
        Retry connection up to MAX_RETRIES times.
        Pattern from 7__WebSocket.md — Automatic Reconnection.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Connection attempt {attempt}/{MAX_RETRIES}...")
                check, reason = await self.client.connect()
                if check:
                    logger.info("✅ Connected to Quotex.")
                    return True
                logger.warning(f"Connection refused: {reason}")
            except Exception as exc:
                logger.error(f"Connection exception: {exc}")
            await asyncio.sleep(5)
        return False

    # ── Bot entry point ────────────────────────────────────────────────────────

    async def start(self):
        connected = await self.connect_with_retries()
        if not connected:
            logger.error("Could not connect after maximum retries. Exiting.")
            return

        self.running = True

        # Always start on PRACTICE — switch to REAL only when strategy is proven
        self.client.set_account_mode("PRACTICE")

        # Show account profile on startup (9__Basic_Examples.md — get_profile)
        try:
            profile = await self.client.get_profile()
            logger.info(
                f"👤 User: {profile.nick_name} | "
                f"Demo: ${profile.demo_balance:.2f} | "
                f"Live: ${profile.live_balance:.2f} | "
                f"Country: {profile.country_name}"
            )
        except Exception as exc:
            logger.warning(f"Profile fetch failed (non-critical): {exc}")

        balance = await self.client.get_balance()
        logger.info(f"💰 Starting PRACTICE balance: ${balance:.2f}")

        try:
            await self.trading_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — shutting down.")
        finally:
            self.running = False
            await self.client.close()          # 9__Basic_Examples.md — client.close()
            logger.info("Connection closed. Bot stopped cleanly.")

    # ── Main trading loop ──────────────────────────────────────────────────────

    async def trading_loop(self):
        self.client.start_signals_data()
        logger.info("📡 Subscribed to Quotex internal signals.")

        while self.running:

            # ── Connection health check (7__WebSocket.md) ────────────────────
            if not await self.client.check_connect():
                logger.warning("WebSocket disconnected — attempting reconnect...")
                if not await self.connect_with_retries():
                    logger.error("Reconnection failed. Stopping bot.")
                    break
                self.client.start_signals_data()
                logger.info("Re-subscribed to signals after reconnect.")

            # ── Auto-refill demo balance (3__Trading_Operations.md §7.2) ─────
            try:
                balance = await self.client.get_balance()
                if balance < MIN_DEMO_BALANCE:
                    logger.info(
                        f"Balance ${balance:.2f} below threshold ${MIN_DEMO_BALANCE}. "
                        f"Refilling with ${REFILL_AMOUNT}..."
                    )
                    await self.client.edit_practice_balance(REFILL_AMOUNT)
                    balance = await self.client.get_balance()
                    logger.info(f"✅ Balance refilled → ${balance:.2f}")
            except Exception as exc:
                logger.warning(f"Balance check/refill failed: {exc}")

            # ── Fetch current signals once per cycle ─────────────────────────
            signals = self.client.get_signal_data()

            for asset in self.assets:
                try:
                    await self._process_asset(asset, signals)
                except Exception as exc:
                    logger.error(f"[{asset}] Unhandled error in _process_asset: {exc}")

            await asyncio.sleep(LOOP_SLEEP)

    # ── Per-asset pipeline ─────────────────────────────────────────────────────

    async def _process_asset(self, asset: str, signals: dict):

        # ── 1. Market-open check (9__Basic_Examples.md — get_available_asset) ─
        #    force_open=True auto-flips between _otc and non-otc versions
        asset_name, asset_data = await self.client.get_available_asset(
            asset, force_open=True
        )
        if not asset_data or not asset_data[2]:
            logger.debug(f"[{asset}] Market is closed, skipping.")
            return
        asset = asset_name  # Use the resolved name (may have toggled _otc)

        # ── 2. Payout quality filter (3__Trading_Operations.md — get_payment) ─
        try:
            payout = self.client.get_payout_by_asset(asset, timeframe="1")
            if payout is not None and payout < MIN_PAYOUT_PCT:
                logger.debug(
                    f"[{asset}] Payout {payout}% < minimum {MIN_PAYOUT_PCT}%, skipping."
                )
                return
        except Exception:
            pass  # If payout unavailable, proceed rather than block

        # ── 3. Start candle stream ────────────────────────────────────────────
        self.client.start_candles_stream(asset, period=60)

        # ── 4. Live price (7__WebSocket.md — start_realtime_price) ───────────
        current_price = None
        try:
            await self.client.start_realtime_price(asset, period=60)
            price_feed = await self.client.get_realtime_price(asset)
            # price_feed is a list of {time, price} dicts — take the latest
            if price_feed:
                current_price = float(price_feed[-1]["price"])
        except Exception as exc:
            logger.debug(f"[{asset}] Live price unavailable: {exc}")

        # ── 5. Market Sentiment (7__WebSocket.md — start_realtime_sentiment) ──
        try:
            await self.client.start_realtime_sentiment(asset, period=60)
        except Exception:
            pass
        sentiment   = await self.client.get_realtime_sentiment(asset)
        buy_percent = sentiment.get("sentiment", {}).get("buy", 50)

        # ── 6. RSI (6__Technical_Indicators.md — RSI section) ─────────────────
        rsi_data    = await self.client.calculate_indicator(
            asset=asset,
            indicator="RSI",
            params={"period": 14},
            timeframe=60
        )
        current_rsi = rsi_data.get("current") or 50.0

        # ── 7. MACD (6__Technical_Indicators.md — MACD section) ───────────────
        #    Response: {macd, signal, histogram, current:{macd, signal, histogram}}
        macd_data      = await self.client.calculate_indicator(
            asset=asset,
            indicator="MACD",
            params={"fast_period": 12, "slow_period": 26, "signal_period": 9},
            timeframe=60
        )
        macd_current   = macd_data.get("current") or {}
        macd_line      = float(macd_current.get("macd")      or 0)
        macd_signal_v  = float(macd_current.get("signal")    or 0)
        macd_histogram = float(macd_current.get("histogram") or 0)

        # ── 8. Bollinger Bands (6__Technical_Indicators.md — Bollinger section) ─
        #    Response: {upper, middle, lower, current:{upper, middle, lower}}
        bb_data    = await self.client.calculate_indicator(
            asset=asset,
            indicator="BOLLINGER",
            params={"period": 20, "std": 2},
            timeframe=60
        )
        bb_current = bb_data.get("current") or {}
        bb_upper   = float(bb_current.get("upper")  or 0)
        bb_middle  = float(bb_current.get("middle") or 0)
        bb_lower   = float(bb_current.get("lower")  or 0)

        # Fall back to BB middle (20-period SMA of closes) if live price failed
        price = current_price if current_price else bb_middle

        # ── 9. Quotex internal signal ─────────────────────────────────────────
        asset_signals = signals.get(asset, {})
        if not asset_signals:
            return

        latest_time = max(asset_signals.keys())
        signal_info = asset_signals[latest_time]
        direction   = str(signal_info.get("dir", "")).lower()
        duration    = int(signal_info.get("duration", 60))

        if direction not in ("call", "put"):
            return

        # ── 10. Log full indicator snapshot ───────────────────────────────────
        logger.info(
            f"[{asset}] Signal={direction.upper()} dur={duration}s | "
            f"Price={price:.5f} | "
            f"RSI={current_rsi:.2f} | "
            f"MACD={macd_line:.5f} Sig={macd_signal_v:.5f} Hist={macd_histogram:.5f} | "
            f"BB upper={bb_upper:.5f} mid={bb_middle:.5f} lower={bb_lower:.5f} | "
            f"Buyers={buy_percent}%"
        )

        # ── 11. Heavy Validation Logic ─────────────────────────────────────────
        #
        # ✅ CALL — execute only when ALL five filters pass:
        #   ① Quotex signal direction  = "call"
        #   ② RSI < 45                 → not overbought; momentum has room to rise
        #   ③ buy_percent > 60         → crowd sentiment strongly favours buyers
        #   ④ MACD line > signal line  AND histogram > 0
        #                              → active bullish crossover with growing strength
        #   ⑤ price < BB upper band    → price below resistance; room to move up
        #
        if (
            direction     == "call"
            and current_rsi    <  45
            and buy_percent    >  60
            and macd_line      >  macd_signal_v
            and macd_histogram >  0
            and bb_upper       >  0
            and price          <  bb_upper
        ):
            logger.info(f"✅ ALL 5 INDICATORS ALIGNED → BUY (Call) on {asset}")
            await self.execute_trade(asset, "call", duration)

        # ✅ PUT — execute only when ALL five filters pass:
        #   ① Quotex signal direction  = "put"
        #   ② RSI > 55                 → not oversold; momentum has room to fall
        #   ③ buy_percent < 40         → crowd sentiment strongly favours sellers
        #   ④ MACD line < signal line  AND histogram < 0
        #                              → active bearish crossover with growing strength
        #   ⑤ price > BB lower band    → price above support; room to move down
        #
        elif (
            direction     == "put"
            and current_rsi    >  55
            and buy_percent    <  40
            and macd_line      <  macd_signal_v
            and macd_histogram <  0
            and bb_lower       >  0
            and price          >  bb_lower
        ):
            logger.info(f"✅ ALL 5 INDICATORS ALIGNED → SELL (Put) on {asset}")
            await self.execute_trade(asset, "put", duration)

        else:
            logger.info(
                f"⏭️  [{asset}] Skipped — confluence not met "
                f"(RSI={current_rsi:.1f} | Hist={macd_histogram:.5f} | "
                f"Buyers={buy_percent}% | "
                f"PriceInBands={'✓' if bb_lower < price < bb_upper else '✗'})"
            )

    # ── Trade execution ────────────────────────────────────────────────────────

    async def execute_trade(self, asset: str, direction: str, duration: int):
        """
        Place a trade then await the result.
        Pattern from 3__Trading_Operations.md §2 — Buy with Result Verification.
        """
        logger.info(
            f"🚀 Placing {direction.upper()} | Asset: {asset} | "
            f"${self.trade_amount} | Duration: {duration}s"
        )

        status, buy_info = await self.client.buy(
            self.trade_amount, asset, direction, duration
        )

        if not status:
            logger.warning(f"❌ Trade placement failed. Reason: {buy_info}")
            return

        trade_id = buy_info.get("id", "?")
        logger.info(f"📋 Trade confirmed (ID: {trade_id}). Waiting for expiry...")

        try:
            win    = await self.client.check_win(trade_id)
            profit = self.client.get_profit()
            icon   = "🏆 WIN" if win else "❌ LOSS"
            logger.info(
                f"{icon} | {asset} {direction.upper()} | "
                f"P&L: ${profit:.2f}"
            )
        except Exception as exc:
            logger.error(f"Error fetching trade result for {asset}: {exc}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Credentials via environment variables — never hardcode (9__Basic_Examples.md)
    # Before running:
    #   export QUOTEX_EMAIL="your@email.com"
    #   export QUOTEX_PASSWORD="yourpassword"
    email    = os.environ.get("QUOTEX_EMAIL")
    password = os.environ.get("QUOTEX_PASSWORD")

    if not email or not password:
        raise EnvironmentError(
            "Credentials missing.\n"
            "Please set environment variables before running:\n"
            "  export QUOTEX_EMAIL='your@email.com'\n"
            "  export QUOTEX_PASSWORD='yourpassword'"
        )

    bot = HeavySignalBot(email=email, password=password)
    asyncio.run(bot.start())
