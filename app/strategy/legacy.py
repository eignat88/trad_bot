from app.models import MarketSnapshot


def legacy_rsi_ma_entry(snapshot: MarketSnapshot) -> bool:
    """Original prototype strategy, retained solely as a comparison baseline."""
    return snapshot.close > snapshot.ma20 and snapshot.rsi < 30
