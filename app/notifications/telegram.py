from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import Settings
from app.models import MarketSnapshot, Trade, TradeSignal


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.token, self.chat_id = settings.telegram_token, settings.telegram_chat_id

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        body = urlencode({"chat_id": self.chat_id, "text": message}).encode()
        response = urlopen(Request(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                   data=body, method="POST"), timeout=10)
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Telegram HTTP {response.status}")
        return True

    def setup(self, signal: TradeSignal, snapshot: MarketSnapshot, risk: float) -> bool:
        return self.send(f"🟢 {signal.symbol}\n\n{signal.setup.value} {signal.side.value}\n\n"
            f"Price 15m: {snapshot.price_change_15m:+.2f}%\nOI 15m: {snapshot.oi_change_15m:+.2f}%\n"
            f"Volume: {snapshot.volume_ratio:.2f}x\nFunding: {snapshot.funding_rate:+.4f}%\n\n"
            f"Entry: {signal.entry:.2f}\nSL: {signal.stop:.2f}\nRisk: {risk:.2f}%")

    def closed(self, trade: Trade) -> bool:
        return self.send(f"POSITION CLOSED\n\nPnL: {trade.pnl_r:+.2f}R\nPnL: {trade.pnl_usdt:+.2f} USDT")
