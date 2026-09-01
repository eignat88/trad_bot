# Grafana Monitoring — trad_bot

## Архитектура

```
Scanner / Paper Engine
        │
        ▼
PostgreSQL (trad_bot)
  ┌─────────────┐
  │     dds     │   ← основной слой данных приложения
  ├─────────────┤
  │     mart    │   ← аналитические представления для Grafana
  └──────▲──────┘
         │
    INSERT / UPDATE
         │
    Scanner + Paper Engine

  ┌──────▼──────┐
  │   Grafana   │   ← READ ONLY (grafana_reader)
  └─────────────┘
         │
         ▼
      Browser (HTTPS)
```

Grafana и PostgreSQL располагаются на одном VPS.

Внешний доступ — только к Grafana через Nginx reverse proxy (HTTPS).
PostgreSQL (порт 5432) не открывается наружу.

---

## Структура файлов

```
trad_bot/
├── sql/mart/
│   ├── 001_create_mart_schema.sql   — схема + grafana_reader
│   ├── 002_scanner_overview.sql     — KPI overview
│   ├── 003_scanner_performance.sql  — per-scanner stats
│   ├── 004_scanner_daily_stats.sql  — daily aggregated
│   ├── 005_paper_equity_curve.sql   — equity over time
│   ├── 006_paper_trade_stats.sql    — aggregate trade stats
│   ├── 007_signal_funnel.sql        — conversion funnel
│   └── 008_scanner_health.sql       — operational health
│
├── monitoring/grafana/
│   ├── dashboards/
│   │   ├── scanner-overview.json
│   │   ├── scanner-performance.json
│   │   ├── paper-trading.json
│   │   ├── signal-funnel.json
│   │   └── system-health.json
│   └── provisioning/
│       ├── dashboards/dashboards.yaml
│       └── datasources/postgres.yaml
│
├── deploy/
│   ├── install_grafana.sh          — установка Grafana
│   ├── configure_grafana.sh        — provisioning + SQL + restart
│   └── nginx/grafana.conf          — reverse proxy config
```

---

## Локальная разработка

### Проверка SQL views

Все SQL-файлы в `sql/mart/` используют `CREATE OR REPLACE VIEW` — безопасно для повторного запуска.

```bash
# Подключиться к локальной БД
psql -U postgres -d trad_bot

# Запустить все миграции
for f in sql/mart/*.sql; do psql -U postgres -d trad_bot -f "$f"; done
```

### Проверка данных

```sql
SELECT * FROM mart.scanner_overview;
SELECT * FROM mart.scanner_performance;
SELECT * FROM mart.scanner_daily_stats ORDER BY trade_date DESC LIMIT 10;
SELECT * FROM mart.paper_equity_curve ORDER BY snapshot_time DESC LIMIT 5;
SELECT * FROM mart.paper_trade_stats;
SELECT * FROM mart.signal_funnel;
SELECT * FROM mart.scanner_health;
```

### Сравнение с dds

```sql
-- Проверка: количество сделок должно совпадать
SELECT COUNT(*) FROM dds.paper_trade WHERE status = 'CLOSED';
SELECT closed_trades FROM mart.paper_trade_stats;
```

---

## Deployment на VPS

### Предварительные требования

- Ubuntu 20.04+ / Debian 11+
- PostgreSQL установлен и работает
- База `trad_bot` существует с данными в схеме `dds`
- Порт 3000 доступен (временно) или настроен Nginx

### Пошаговая инструкция

#### 1. Клонировать / обновить проект

```bash
cd /opt
git clone https://github.com/eignat88/trad_bot.git
cd trad_bot
git fetch origin
git switch feat/grafana-monitoring
```

#### 2. Создать схему mart и grafana_reader

```bash
export GRAFANA_READER_PASSWORD='your_secure_password_here'

# Запустить SQL-миграции
for f in sql/mart/*.sql; do
    sudo -u postgres psql -d trad_bot -f "$f"
done
```

Или автоматически через скрипт:

```bash
sudo GRAFANA_READER_PASSWORD='your_password' bash deploy/configure_grafana.sh
```

#### 3. Установить Grafana

```bash
sudo bash deploy/install_grafana.sh
```

#### 4. Настроить provisioning и dashboards

```bash
sudo GRAFANA_READER_PASSWORD='your_password' bash deploy/configure_grafana.sh
```

Этот скрипт:
- создаст/обновит пользователя `grafana_reader`
- применит SQL-миграции
- скопирует provisioning файлы
- скопирует dashboard JSON
- перезапустит Grafana

#### 5. Проверить статус

```bash
sudo systemctl status grafana-server
ss -lntp | grep 3000
```

#### 6. Открыть Grafana

```
http://<VPS_IP>:3000
```

Логин: `admin` / `admin` (сменить при первом входе).

#### 7. (Production) Настроить Nginx reverse proxy

```bash
sudo apt install nginx
sudo cp deploy/nginx/grafana.conf /etc/nginx/sites-available/grafana
sudo ln -s /etc/nginx/sites-available/grafana /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Для HTTPS:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d grafana.yourdomain.com
```

---

## Пользователь grafana_reader

Grafana подключается к PostgreSQL через пользователя `grafana_reader`.

Права:
- READ-only доступ к схемам `dds` и `mart`
- Нет прав на запись
- Нет прав на DDL

Пароль задается через переменную окружения `GRAFANA_READER_PASSWORD` и не хранится в Git.

---

## Dashboards

### Scanner Overview
- KPI: scanner runs, setups, signals, trades, errors
- Last events timestamps
- Trades over time
- Trades by scanner / direction
- PnL by scanner
- Signal funnel stages

### Scanner Performance
- Таблица: per-scanner performance metrics
- PnL by scanner (time series)
- Avg R, Profit Factor, Win Rate by scanner
- Trades by scanner
- PnL by direction over time

### Paper Trading
- KPI: equity, PnL, trades, win rate, profit factor, avg R, max drawdown
- Equity curve (time series)
- Cumulative PnL
- Daily PnL
- PnL by scanner / direction
- Recent trades table

### Signal Funnel
- Funnel stages: scanned → setups → ready → signals → trades → wins
- Conversion gauges (%)
- Visual funnel bar chart
- Trades by scanner table

### System Health
- Last run / signal / error timestamps
- Counters: runs, signals, errors (1h / 24h)
- Avg run duration, failed runs
- Scanner runs over time
- Run duration over time
- Errors over time
- Run stats by scanner table

---

## Alerts (будущее)

Базовые alert rules:

| Alert | Condition | Severity |
|-------|-----------|----------|
| Scanner Stale | `now() - last_scanner_run > 10min` | Warning |
| No Runs | `runs_last_1h = 0` | Warning |
| Error Spike | `errors_last_1h >= 5` | Critical |

Подключение notification channels (Telegram, email) — отдельная настройка.

---

## Безопасность

- PostgreSQL не открыт наружу (порт 5432 internal only)
- grafana_reader — read-only
- Пароли не хранятся в Git
- `.env` файлы в `.gitignore`
- Рекомендуется HTTPS через Nginx

---

## Производительность

Все SQL views проверены на текущем объеме данных (383 scanner runs, 1032 setups, 242 signals, 35 trades).

Рекомендуемые индексы для production:

```sql
-- Уже существуют в dds (создаются автоматически при заполнении):
-- dds.scanner_run(created_at)
-- dds.scanner_error(created_at)
-- dds.market_signal(created_at)
-- dds.paper_trade(created_at)
```

Для больших объемов данных рекомендуется Materialized View вместо VIEW для `scanner_overview` и `signal_funnel`.
