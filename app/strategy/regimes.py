from enum import Enum

from app.models import MarketSnapshot


class PriceOIRegime(str, Enum):
    PRICE_UP_OI_UP = "STATE_1_PRICE_UP_OI_UP"
    FOMO = "STATE_2_FOMO"
    PRICE_DOWN_OI_UP = "STATE_3_PRICE_DOWN_OI_UP"
    PRICE_DOWN_OI_DOWN = "STATE_4_PRICE_DOWN_OI_DOWN"
    PRICE_UP_OI_DOWN = "STATE_5_PRICE_UP_OI_DOWN"
    FLAT_OI_UP = "STATE_6_FLAT_OI_UP"
    FLAT_OI_DOWN = "STATE_7_FLAT_OI_DOWN"
    NEUTRAL = "NEUTRAL"


def classify_regime(snapshot: MarketSnapshot, flat_threshold: float = 0.15,
                    fomo_price: float = 8.0, fomo_oi: float = 15.0) -> PriceOIRegime:
    price, oi = snapshot.price_change_15m, snapshot.oi_change_15m
    if snapshot.price_change_1h >= fomo_price and snapshot.oi_change_1h >= fomo_oi:
        return PriceOIRegime.FOMO
    if abs(price) <= flat_threshold:
        if oi > 0:
            return PriceOIRegime.FLAT_OI_UP
        if oi < 0:
            return PriceOIRegime.FLAT_OI_DOWN
    if price > 0 and oi > 0:
        return PriceOIRegime.PRICE_UP_OI_UP
    if price < 0 and oi > 0:
        return PriceOIRegime.PRICE_DOWN_OI_UP
    if price < 0 and oi < 0:
        return PriceOIRegime.PRICE_DOWN_OI_DOWN
    if price > 0 and oi < 0:
        return PriceOIRegime.PRICE_UP_OI_DOWN
    return PriceOIRegime.NEUTRAL
