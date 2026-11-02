"""Normalize read-only Bitget UTA responses without inventing financial data."""
from decimal import Decimal, InvalidOperation, localcontext
from position_costs import PositionCostCache

PRODUCTS = frozenset({'USDT-FUTURES', 'USDC-FUTURES', 'COIN-FUTURES'})


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _numeric(value):
    number = _number(value)
    return str(number) if number is not None else None


def _time(value, required=False):
    number = _number(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        if required:
            raise ValueError('UTA position creation time is invalid')
        return None
    return str(int(number))


def _base(raw, category):
    if not isinstance(raw, dict):
        raise ValueError('UTA position must be an object')
    product = str(category or '').upper()
    if product not in PRODUCTS or (raw.get('category') and str(raw['category']).upper() != product):
        raise ValueError('UTA position category mismatch')
    symbol = str(raw.get('symbol') or '').strip().upper()
    side = str(raw.get('posSide') or '').lower()
    if not symbol or side not in {'long', 'short'}:
        raise ValueError('UTA position identity is incomplete')
    return {
        'productType': product, 'symbol': symbol, 'holdSide': side,
        'marginCoin': raw.get('marginCoin'), 'marginMode': raw.get('marginMode'),
        'holdMode': raw.get('holdMode'), 'cTime': _time(raw.get('createdTime'), required=True),
        'uTime': _time(raw.get('updatedTime')), '_accountMode': 'uta',
    }


def unwrap_list(data):
    """A null list is an observed UTA empty result; malformed envelopes are errors."""
    if not isinstance(data, dict) or 'list' not in data:
        raise ValueError('UTA list response is malformed')
    rows = data['list']
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('UTA list response has invalid rows')
    return rows


def normalize_position(raw, category):
    result = _base(raw, category)
    quantity, entry = _number(raw.get('total')), _number(raw.get('avgPrice'))
    if quantity is None or quantity < 0 or entry is None or entry < 0 or (quantity > 0 and entry <= 0):
        raise ValueError('UTA position quantity or entry price is invalid')
    result.update({
        'total': str(quantity), 'openPriceAvg': str(entry),
        'markPrice': _numeric(raw.get('markPrice')),
        'unrealizedPL': _numeric(raw.get('unrealisedPnl')),
        'marginSize': _numeric(raw.get('positionBalance')),
        'leverage': _numeric(raw.get('leverage')),
        'available': _numeric(raw.get('available')), 'frozen': _numeric(raw.get('frozen')),
        'totalFee': _numeric(raw.get('totalFunding')),
        'breakEvenPrice': _numeric(raw.get('breakEvenPrice')),
        'liquidationPrice': _numeric(raw.get('liquidationPrice')),
        'takeProfit': None, 'stopLoss': None,
        '_utaCurRealisedPnl': _numeric(raw.get('curRealisedPnl')),
        '_utaCashDividend': _numeric(raw.get('cashDividend')),
        '_utaOpenFeeTotal': _numeric(raw.get('openFeeTotal')),
        '_utaCloseFeeTotal': _numeric(raw.get('closeFeeTotal')),
    })
    realised, dividend = _number(raw.get('curRealisedPnl')), _number(raw.get('cashDividend'))
    opened_fee, closed_fee = _number(raw.get('openFeeTotal')), _number(raw.get('closeFeeTotal'))
    with localcontext() as context:
        context.prec = 50
        result['achievedProfits'] = str(realised) if realised is not None and dividend is not None else None
        # UTA docs do not define whether current realised PnL already includes dividends.
        result['_utaPnlAmbiguous'] = dividend is not None and dividend != 0
        result['deductedFee'] = str(abs(opened_fee + closed_fee)) if opened_fee is not None and closed_fee is not None else None
    result['_utaFinancialsComplete'] = all(result.get(key) is not None for key in
        ('unrealizedPL', 'achievedProfits', 'deductedFee', 'totalFee'))
    # UTA current-position does not promise a position ID. Do not synthesize one.
    if raw.get('positionId'):
        result['posId'] = str(raw['positionId'])
    return result


def normalize_history(raw, category):
    result = _base(raw, category)
    position_id = str(raw.get('positionId') or '').strip()
    if not position_id:
        raise ValueError('UTA historical position ID is missing')
    result.update({
        'positionId': position_id, 'ctime': result['cTime'], 'utime': result['uTime'],
        'openAvgPrice': _numeric(raw.get('openPriceAvg')),
        'closeAvgPrice': _numeric(raw.get('closePriceAvg')),
        'openTotalPos': _numeric(raw.get('openTotalPos')),
        'closeTotalPos': _numeric(raw.get('closeTotalPos')),
        'pnl': _numeric(raw.get('cumRealisedPnl')),
        'netProfit': _numeric(raw.get('netProfit')),
        'openFee': _numeric(raw.get('openFeeTotal')),
        'closeFee': _numeric(raw.get('closeFeeTotal')),
        'totalFunding': _numeric(raw.get('totalFunding')),
        '_utaCashDividend': _numeric(raw.get('cashDividend')),
    })
    return result


def apply_tpsl(positions, rows):
    """Attach only unambiguous, verified pending TPSL targets to copied positions."""
    if not isinstance(positions, dict) or not isinstance(rows, list):
        raise ValueError('UTA strategy response is malformed')
    validated = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('UTA strategy response has an invalid order')
        category = str(row.get('category') or '').upper()
        symbol = str(row.get('symbol') or '').strip().upper()
        side = str(row.get('posSide') or '').lower()
        if category not in PRODUCTS or not symbol or side not in {'long', 'short'}:
            raise ValueError('UTA strategy identity is incomplete')
        validated.append(((category, symbol, side), row))
    result = {key: dict(value) for key, value in positions.items()}
    for item in result.values():
        match = (str(item.get('productType') or '').upper(), str(item.get('symbol') or '').upper(), str(item.get('holdSide') or '').lower())
        matching = [row for key, row in validated if key == match]
        unknown_status = any(row.get('status') not in {'pending', 'submitting', 'success', 'failed', 'cancelled'} for row in matching)
        active = [row for row in matching if row.get('status') in {'pending', 'submitting'}]
        for field in ('takeProfit', 'stopLoss'):
            targets, unknown = set(), unknown_status
            for row in active:
                value = row.get(field)
                if value == '':
                    continue
                number = _number(value)
                if number is None or number < 0:
                    unknown = True
                elif number > 0:
                    targets.add(number)
            item[field] = None if unknown or len(targets) > 1 else (str(next(iter(targets))) if targets else '')
    return result


class UTAPositionCostCache(PositionCostCache):
    FEE_PATH = '/api/v3/account/fee-rate'
    CONTRACT_PATH = '/api/v3/market/instruments'

    def _read(self, product_type, symbol):
        params = {'category': product_type, 'symbol': symbol}
        fees = self.reader(params, self.FEE_PATH)
        if not isinstance(fees, dict):
            raise ValueError('Invalid UTA account fee response')
        fee = _number(fees.get('takerFeeRate'))
        if fee is None or not Decimal(0) <= fee < Decimal(1):
            raise ValueError('Invalid UTA taker fee rate')
        instruments = self.reader(params, self.CONTRACT_PATH)
        if not isinstance(instruments, list):
            raise ValueError('Invalid UTA instrument response')
        matches = [row for row in instruments if isinstance(row, dict)
                   and row.get('symbol') == symbol
                   and str(row.get('category') or '').upper() == product_type]
        if len(matches) != 1:
            raise ValueError('UTA instrument was not uniquely matched')
        contract = matches[0]
        precision = _number(contract.get('pricePrecision'))
        tick = _number(contract.get('priceMultiplier'))
        if precision is None or precision != precision.to_integral_value() or not 0 <= precision <= 18 or tick is None or tick <= 0:
            raise ValueError('Invalid UTA price increment')
        with localcontext() as context:
            context.prec = 50
            units = tick.scaleb(int(precision))
            if units != units.to_integral_value():
                raise ValueError('UTA price increment exceeds price precision')
        return {'_closeTakerFeeRate': str(fee), '_priceTick': str(tick)}
