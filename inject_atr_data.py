
import redis
import time
import json
from datetime import datetime, timedelta

def inject_mock_bars():
    r = redis.Redis(host='127.0.0.1', port=6379, db=0)
    
    # 模拟 14 个 5 分钟 Bar (上周五最后 70 分钟)
    # 策略计算 ATR 需要 OHLC 数据
    
    targets = {
        "TQQQ": {"price": 70.0, "atr": 0.85},
        "SQQQ": {"price": 10.0, "atr": 0.22}
    }
    
    now = datetime.now()
    
    for symbol, cfg in targets.items():
        key = f"sfs:bars:5m:{symbol}"
        r.delete(key) # 清除旧数据重新注入
        
        base_px = cfg["price"]
        atr = cfg["atr"]
        
        bars = []
        for i in range(20): # 多注入几根确保足够
            timestamp = int((now - timedelta(minutes=5 * (20 - i))).timestamp())
            # 构造带波动的 Bar
            bar = {
                "symbol": symbol,
                "ts": timestamp,
                "open": round(base_px, 4),
                "high": round(base_px + atr * 0.6, 4),
                "low": round(base_px - atr * 0.4, 4),
                "close": round(base_px + atr * 0.1, 4),
                "volume": 100000
            }
            # 更新基准价模拟小幅走势
            base_px = bar["close"]
            r.rpush(key, json.dumps(bar))
            
        print(f"✅ Injected 20 bars for {symbol} into {key}")

if __name__ == "__main__":
    inject_mock_bars()
