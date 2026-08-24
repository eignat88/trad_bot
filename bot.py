import tkinter as tk
from tkinter import ttk
import requests
import time
import hashlib
import hmac
import json
from datetime import datetime
import threading

# ========== КОНФИГУРАЦИЯ ========== #
API_KEY = "Ключ от Bybit"
API_SECRET = "Второй ключ от Bybit"
TELEGRAM_TOKEN = "@Имя_в_ТГ"  # Замените на реальный токен
TELEGRAM_CHAT_ID = "чат_ID_в_ТГ"

DEFAULT_SETTINGS = {
    "pairs": ["BTCUSDT", "ETHUSDT"],
    "timeframe": "15",
    "risk_per_trade": 1,
    "stop_loss": 2,
    "take_profit": 3,
    "rsi_period": 14,
    "ma_period": 20
}

# ========== ЯДРО БОТА ========== #
class TradingBot:
    def __init__(self, settings):
        self.settings = settings
        self.active_orders = {}
        self.is_running = False
        self.gui = None

    def generate_signature(self, secret, data):
        return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()

    def get_price(self, symbol):
        try:
            url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            return float(data['result']['list'][0]['lastPrice'])
        except Exception as e:
            self.send_telegram(f"Ошибка цены {symbol}: {str(e)}")
            return 0.0

    def get_rsi(self, symbol):
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval={self.settings['timeframe']}&limit={self.settings['rsi_period'] + 1}"
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            closes = [float(item[4]) for item in data['result']['list']]
            
            gains, losses = [], []
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i-1]
                if diff > 0:
                    gains.append(diff)
                else:
                    losses.append(abs(diff))
            
            avg_gain = sum(gains)/self.settings['rsi_period'] if gains else 0
            avg_loss = sum(losses)/self.settings['rsi_period'] if losses else 1e-9
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))
        except Exception as e:
            self.send_telegram(f"Ошибка RSI {symbol}: {str(e)}")
            return 50.0

    def get_ma(self, symbol):
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval={self.settings['timeframe']}&limit={self.settings['ma_period']}"
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            closes = [float(item[4]) for item in data['result']['list']]
            return sum(closes)/len(closes)
        except Exception as e:
            self.send_telegram(f"Ошибка MA {symbol}: {str(e)}")
            return 0.0

    def send_telegram(self, message):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            }, timeout=5)
        except Exception as e:
            print(f"Ошибка Telegram: {str(e)}")

    def start_trading(self):
        self.is_running = True
        while self.is_running:
            for symbol in self.settings['pairs']:
                try:
                    price = self.get_price(symbol)
                    rsi = self.get_rsi(symbol)
                    ma = self.get_ma(symbol)
                    
                    if symbol not in self.active_orders:
                        if price > ma and rsi < 30:
                            self.place_order(symbol, "Buy")
                    else:
                        order = self.active_orders[symbol]
                        pl_pct = (price / float(order['price'])) - 1
                        
                        if pl_pct >= self.settings['take_profit']/100 or \
                           pl_pct <= -self.settings['stop_loss']/100 or \
                           (price < ma and rsi > 70):
                            self.place_order(symbol, "Sell")
                            del self.active_orders[symbol]
                            
                except Exception as e:
                    self.send_telegram(f"Ошибка: {symbol} - {str(e)}")
            
            time.sleep(60)

# ========== ГРАФИЧЕСКИЙ ИНТЕРФЕЙС ========== #
class TradingGUI(tk.Tk):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        bot.gui = self
        
        self.title("Crypto Trading Bot")
        self.geometry("800x600")
        
        self.create_widgets()
        self.update_data()
        
    def create_widgets(self):
        self.control_frame = ttk.LabelFrame(self, text="Управление")
        self.control_frame.pack(pady=10, fill=tk.X)
        
        self.start_btn = ttk.Button(self.control_frame, text="Старт", command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(self.control_frame, text="Стоп", command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.data_frame = ttk.LabelFrame(self, text="Данные")
        self.data_frame.pack(fill=tk.BOTH, expand=True, padx=10)
        
        self.tree = ttk.Treeview(self.data_frame, columns=("Pair", "Price", "RSI", "MA", "Status"), show="headings")
        columns = ("Pair", "Price", "RSI", "MA", "Status")
        for col in columns:
        self.tree.heading(col, text=col)
        self.tree.column(col, width=100, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        self.log_frame = ttk.LabelFrame(self, text="Логи")
        self.log = tk.Text(self.log_frame, height=8, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
    def start_bot(self):
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        threading.Thread(target=self.bot.start_trading, daemon=True).start()
        
    def stop_bot(self):
        self.bot.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        
    def update_data(self):
        for symbol in self.bot.settings['pairs']:
            price = self.bot.get_price(symbol)
            rsi = self.bot.get_rsi(symbol)
            ma = self.bot.get_ma(symbol)
            status = "Active" if symbol in self.bot.active_orders else "Waiting"
            
            values = (symbol, f"{price:.2f}", f"{rsi:.1f}", f"{ma:.2f}", status)
            
            if not self.tree.exists(symbol):
                self.tree.insert("", tk.END, iid=symbol, values=values)
            else:
                self.tree.item(symbol, values=values)
        
        self.after(5000, self.update_data)
        
    def log_message(self, message):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"{datetime.now().strftime('%H:%M:%S')} {message}\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

# ========== ЗАПУСК ========== #
if __name__ == "__main__":
    bot = TradingBot(DEFAULT_SETTINGS)
    app = TradingGUI(bot)
    app.mainloop()