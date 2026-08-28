# План доработки: fix/paper-live-safety

## Контекст

Анализ данных paper trading за 25–28 августа 2026 выявил критические проблемы:
- Конверсия сетапов в сделки: **3.8%** (1,030 сетапов → 39 сделок)
- Все 242 рыночных сигнала истекли без исполнения (`NO_ACTIVE_SETUPS`)
- Бот торгует LONG в TREND_DOWN → серия стоп-лоссов (27 авг)
- 61 ошибка API из-за частоты сканов каждую минуту

## Цель

Повысить конверсию сетапов в сделки с 3.8% до 10–15%, убрать ложные входы против тренда, снизить нагрузку на API.

---

## Доработка 1: Market Regime Filter (Entry Gate)

**Проблема:** Бот открывает LONG в TREND_DOWN и SHORT в TREND_UP, что приводит к сериям стопов.

**Решение:** Добавить фильтр направления в зависимости от рыночного режима:

| Рейжим | LONG | SHORT |
|---|---|---|
| TREND_UP | ✅ | ❌ |
| TREND_DOWN | ❌ | ✅ |
| RANGE | ✅ | ✅ |
| HIGH_VOLATILITY | ✅ | ✅ |

**Файлы:**
- `app/paper/engine.py` → метод `check_entries()`: добавить проверку `market_regime` перед входом
- `app/scanners/orchestrator.py` → метод `scan_all_with_stats()`: добавить фильтр на уровне сканера

---

## Доработка 2: Увеличение TTL сетапов

**Проблема:** TTL 2 часа (12 баров × 5m = 60 минут, 8 баров × 15m = 120 минут). Сетапы истекают до касания entry zone.

**Решение:** Увеличить TTL до 4 часов ( double текущего) + сделать настраиваемым:

```python
_ENTRY_TIMEOUT_MAP = {
    "5m": 24,   # 2 часа (было 12)
    "15m": 16,  # 4 часа (было 8)
    "1h": 12,   # 12 часов (было 6)
    "4h": 8,    # 32 часа (было 4)
}
```

**Файлы:**
- `app/paper/engine.py` → `_ENTRY_TIMEOUT_MAP` + новый параметр `setup_ttl_multiplier` в Settings
- `app/scanners/orchestrator.py` → `EXPIRATION_MAP` аналогично
- `app/config/settings.py` → добавить `setup_ttl_multiplier: float = 2.0`

---

## Доработка 3: Снижение частоты сканов

**Проблема:** Paper runner проверяет каждые 60 секунд → 61 ошибка API за 2 дня.

**Решение:** Увеличить интервал paper runner до 300 секунд (5 минут), как scanner runner.

**Файлы:**
- `paper_runner.py` → `interval = 60` → `interval = 300`

---

## Доработка 4: Расширение pullback_tolerance

**Проблема:** TREND_PULLBACK генерирует 40% сетапов, но конвертирует лишь 1 сделку. Входная зона слишком узкая.

**Решение:** Увеличить `pullback_tolerance` с 0.5% до 1.0% и `entry_zone` spread:

**Файлы:**
- `app/scanners/trend_pullback.py` → `pullback_tolerance: float = 0.01` (было 0.005)

---

## Доработка 5: Trailing Stop для Expired сделок

**Проблема:** Сделки с `EXPIRED` часто уходят в прибыль перед тайм-аутом, но фиксируют loss при EXPIRE.

**Решение:** Если MFE > 0.5R к моменту expiry, закрывать по текущей цене вместо TP/SL.

Уже частично реализовано в `_close_trade()` — expired trades используют recovery price. Доработка: добавить `EXPIRED_PROFITABLE` reason когда MFE > 0 и текущая цена > entry.

**Файлы:**
- `app/paper/engine.py` → метод `check_exits()`: в блоке expired добавить проверку MFE

---

## Доработка 6: Настройки в Settings

**Файлы:**
- `app/config/settings.py`:
  - `setup_ttl_multiplier: float = 2.0`
  - `regime_filter_enabled: bool = True`
  - `paper_scan_interval: int = 300`

---

## Порядок реализации

1. Settings (размерка конфига)
2. Regime filter в paper engine
3. TTL увеличение
4. Scan interval снижение
5. Pullback tolerance расширение
6. Expired profitable exit
7. Тесты

## Тесты

- `tests/test_paper_engine.py` — regime filter, TTL, expired profitable
- `tests/test_scanner_runtime.py` — TTL multiplier
- Существующие тесты должны продолжать работать
