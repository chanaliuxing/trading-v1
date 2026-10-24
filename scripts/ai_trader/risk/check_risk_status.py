#!/usr/bin/env python3
"""查看Pretrade Risk状态和决策逻辑"""
import json
import sys
from pathlib import Path
from datetime import date

def main():
    runs_dir = Path("./runs/ai_trader")
    today = date.today().isoformat()
    risk_file = runs_dir / today / "risk_limits.json"
    cache_file = runs_dir / today / "risk_pretrade.cache.json"
    
    print("=" * 80)
    print("📊 Pretrade Risk 状态检查")
    print("=" * 80)
    print()
    
    # 1. 检查risk_limits.json
    if risk_file.exists():
        print("✅ risk_limits.json 存在")
        with risk_file.open() as f:
            limits = json.load(f)
        
        print(f"\n📋 当前决策 ({len(limits)} symbols):\n")
        
        # 统计决策分布
        from collections import Counter
        decisions = Counter(d["decision"] for d in limits.values())
        
        for dec, count in decisions.most_common():
            print(f"  {dec}: {count} symbols")
        
        print(f"\n📝 详细信息:\n")
        for symbol, data in sorted(limits.items()):
            dec = data["decision"]
            flags = data.get("risk_flags", [])
            
            # 决策emoji
            emoji = {
                "ALLOW": "✅",
                "SIZE_DOWN": "⚠️",
                "REJECT": "🚫",
                "PENDING": "⏳",
                "EXIT": "❌"
            }.get(dec, "❓")
            
            print(f"  {emoji} {symbol:8s} → {dec:12s} | flags: {flags}")
    else:
        print("❌ risk_limits.json 不存在")
        print(f"   期望路径: {risk_file}")
    
    # 2. 检查缓存统计
    print(f"\n{'=' * 80}")
    print("💾 缓存状态")
    print("=" * 80)
    
    if cache_file.exists():
        with cache_file.open() as f:
            cache = json.load(f)
        
        stats = cache.get("stats", {})
        print(f"\n  缓存命中: {stats.get('hits', 0)}")
        print(f"  缓存未命中: {stats.get('misses', 0)}")
        print(f"  全局失效次数: {stats.get('global_invalidations', 0)}")
        
        per_symbol = cache.get("per_symbol", {})
        print(f"  缓存的symbols: {len(per_symbol)}")
    else:
        print("\n  ❌ 缓存文件不存在(首次运行正常)")
    
    # 3. 决策逻辑说明
    print(f"\n{'=' * 80}")
    print("📖 决策含义和影响")
    print("=" * 80)
    print("""
  ✅ ALLOW        → 允许正常交易
  ⚠️  SIZE_DOWN   → 允许交易但降低仓位(50%缩减)
  🚫 REJECT       → 暂停该symbol的所有交易
  ⏳ PENDING      → 等待数据补充,暂不交易
  ❌ EXIT         → 立即平仓该symbol
  
📍 决策依据:
  - ev_le_0: 期望值 ≤ 0 (交易无利润预期)
  - pf_le_1: 盈亏比 ≤ 1 (亏损≥盈利)
  - var_backtest_fail: VaR回测失败(风险模型不准)
  - spread_gt_50bps: 买卖价差 > 50bps (流动性差)
  - adv_lt_10m: 日均成交额 < $10M (流动性不足)
  - beta_spy_gt_1.5: 相对SPY的beta > 1.5 (波动过大)
  - data_missing: 缺少关键指标(atr14/rvol20)
  
🔧 控制位置:
  - orchestrator.py:1074-1078 - 应用到交易计划
  - REJECT → 设置 per_symbol_override.pause
  - SIZE_DOWN → 添加 restrictions.reduce_sizing
    """)
    
    print("=" * 80)

if __name__ == "__main__":
    main()
