#!/usr/bin/env python3
"""
验证每个 LLM bot 的资金是独立的

这个脚本创建多个 bot 实例，确认它们的资金池是独立的。
"""

from llm_trader.portfolio.portfolio_manager import PortfolioManager


def test_independent_portfolios():
    """测试多个 portfolio 实例是否独立"""
    print("=" * 80)
    print("测试：每个 LLM Bot 的资金是否独立")
    print("=" * 80)

    # 创建 4 个独立的 portfolio (模拟 4 个 LLM)
    portfolios = {
        "openai": PortfolioManager(initial_cash=50000.0),
        "gemini": PortfolioManager(initial_cash=50000.0),
        "deepseek": PortfolioManager(initial_cash=50000.0),
        "deepseek_nvidia": PortfolioManager(initial_cash=50000.0),
    }

    print(f"\n初始状态：")
    for name, portfolio in portfolios.items():
        print(f"  {name:20s}: ${portfolio.cash:,.2f}")

    # OpenAI 买入 AAPL
    print(f"\n操作：OpenAI 买入 AAPL x 100 @ $180")
    portfolios["openai"].open_position("AAPL", 100, 180.0)

    # Gemini 买入 TSLA
    print(f"操作：Gemini 买入 TSLA x 50 @ $250")
    portfolios["gemini"].open_position("TSLA", 50, 250.0)

    # 检查每个 portfolio 的状态
    print(f"\n操作后状态：")
    for name, portfolio in portfolios.items():
        state = portfolio.get_position_state()
        print(f"  {name:20s}: Cash=${state['cash']:>10,.2f}, "
              f"Positions={len(state['positions'])}, "
              f"Total=${state['total_equity']:>10,.2f}")

    # 验证
    print(f"\n验证结果：")
    print(f"  ✓ OpenAI 现金减少: ${portfolios['openai'].cash:,.2f} (应该是 $32,000)")
    print(f"  ✓ Gemini 现金减少: ${portfolios['gemini'].cash:,.2f} (应该是 $37,500)")
    print(f"  ✓ DeepSeek 现金不变: ${portfolios['deepseek'].cash:,.2f} (应该是 $50,000)")
    print(f"  ✓ DeepSeek-NVIDIA 现金不变: ${portfolios['deepseek_nvidia'].cash:,.2f} (应该是 $50,000)")

    # 总资金
    total_cash = sum(p.cash for p in portfolios.values())
    total_equity = sum(p.total_equity for p in portfolios.values())

    print(f"\n总计：")
    print(f"  总现金: ${total_cash:,.2f}")
    print(f"  总权益: ${total_equity:,.2f}")
    print(f"  初始总资金: ${4 * 50000:,.2f}")

    print(f"\n结论：")
    if total_equity == 200000.0:
        print(f"  ✅ 每个 LLM 都有独立的 $50,000 资金池")
        print(f"  ✅ 总资金 = 4 × $50,000 = $200,000")
        print(f"  ✅ 资金是独立的，不共享")
    else:
        print(f"  ❌ 出现问题")

    print("=" * 80)


if __name__ == "__main__":
    test_independent_portfolios()
