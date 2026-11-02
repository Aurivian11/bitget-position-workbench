"""Account trading fees and contract price increments for position estimates."""
import threading
import time
from decimal import Decimal, localcontext


class PositionCostCache:
    FEE_PATH = "/api/v2/common/trade-rate"
    CONTRACT_PATH = "/api/v2/mix/market/contracts"
    LINEAR_PRODUCTS = frozenset({"USDT-FUTURES", "USDC-FUTURES"})
    METADATA_KEYS = ("_closeTakerFeeRate", "_priceTick")

    def __init__(self, reader, *, ttl=300, retry_seconds=30, clock=time.monotonic):
        self.reader = reader
        self.ttl = max(0.0, float(ttl))
        self.retry_seconds = max(0.0, float(retry_seconds))
        self.clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def _read(self, product_type, symbol):
        fees = self.reader({"symbol": symbol, "businessType": "mix"}, self.FEE_PATH)
        if not isinstance(fees, dict):
            raise ValueError("Invalid account fee response")
        fee = Decimal(str(fees.get("takerFeeRate")))
        if not fee.is_finite() or not Decimal(0) <= fee < Decimal(1):
            raise ValueError("Invalid taker fee rate")
        contracts = self.reader(
            {"symbol": symbol, "productType": product_type}, self.CONTRACT_PATH
        )
        if not isinstance(contracts, list):
            raise ValueError("Invalid contract response")
        matches = [row for row in contracts if isinstance(row, dict) and row.get("symbol") == symbol]
        if len(matches) != 1:
            raise ValueError("Contract symbol was not uniquely matched")
        contract = matches[0]
        places = int(str(contract.get("pricePlace")))
        step = Decimal(str(contract.get("priceEndStep")))
        if not 0 <= places <= 18 or not step.is_finite() or step <= 0:
            raise ValueError("Invalid contract price increment")
        with localcontext() as context:
            context.prec = max(28, len(step.as_tuple().digits) + places + 5)
            tick = step.scaleb(-places)
        if not tick.is_finite() or tick <= 0:
            raise ValueError("Invalid contract price increment")
        return {"_closeTakerFeeRate": str(fee), "_priceTick": str(tick)}

    def annotate(self, item, product_type):
        """Copy a position and attach only complete, currently valid metadata.

        A lookup failure never blocks the positions result by raising an error.
        Old metadata is not reused after expiry; retries are rate-limited.
        """
        result = dict(item)
        for key in self.METADATA_KEYS:
            result.pop(key, None)
        product_type = str(product_type or "").strip().upper()
        symbol = str(item.get("symbol") or "").strip()
        if product_type not in self.LINEAR_PRODUCTS or not symbol:
            return result
        key = (product_type, symbol)
        with self._lock:
            now = self.clock()
            cached = self._entries.get(key)
            if cached is not None and now < cached[0]:
                if cached[1] is not None:
                    result.update(cached[1])
                return result
            try:
                values = self._read(product_type, symbol)
            except Exception:
                # Do not log exception text: transport errors can contain URLs.
                self._entries[key] = (self.clock() + self.retry_seconds, None)
                return result
            self._entries[key] = (self.clock() + self.ttl, values)
            result.update(values)
            return result
