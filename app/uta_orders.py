"""Strict UTA execution-to-classic fill mapping for the existing Bark observer."""
from decimal import Decimal, InvalidOperation

FILL_PATH = '/api/v3/trade/fills'
PENDING_PATH = '/api/v3/trade/unfilled-orders'
FUTURES = {'USDT-FUTURES', 'USDC-FUTURES', 'COIN-FUTURES'}


def normalize_uta_fill(raw):
    """Return one explicit opening execution; never infer an unknown trade side."""
    if not isinstance(raw, dict) or str(raw.get('tradeSide') or '').lower() != 'open':
        return None
    category = str(raw.get('category') or '').upper()
    if category not in FUTURES:
        return None
    side = str(raw.get('side') or '').lower()
    if side not in {'buy', 'sell'}:
        return None
    direction = str(raw.get('holdSide') or raw.get('posSide') or '').lower()
    expected = 'long' if side == 'buy' else 'short'
    if direction and direction != expected:
        return None
    if str(raw.get('holdMode') or raw.get('posMode') or '').lower() in {'one_way_mode', 'one-way-mode'}:
        return None
    if str(raw.get('reduceOnly') or '').lower() in {'yes', 'true', '1'}:
        return None
    if not all(raw.get(k) for k in ('execId', 'orderId', 'symbol')):
        return None
    stamp = raw.get('execTime') or raw.get('createdTime')
    try:
        price, quantity = Decimal(str(raw.get('execPrice'))), Decimal(str(raw.get('execQty')))
        timestamp = int(stamp)
        if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0 or timestamp <= 0:
            return None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    item = dict(raw)
    item.update(
        productType=category, category=category, tradeId=str(raw['execId']),
        price=str(raw['execPrice']), baseVolume=str(raw['execQty']), cTime=str(timestamp),
        posSide=direction or expected, posMode=raw.get('holdMode') or raw.get('posMode') or '',
        marginCoin=raw.get('marginCoin') or {'USDT-FUTURES':'USDT','USDC-FUTURES':'USDC','COIN-FUTURES':'USD'}[category],
    )
    return item
