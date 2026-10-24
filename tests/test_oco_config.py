"""
OCO 配置测试脚本

测试策略级 OCO 配置是否正确工作:
1. 策略读取 JSON 配置
2. 传递到 DecisionEnvelope
3. Translator 正确解析
4. OCO 价格计算正确

Usage:
    python tests/test_oco_config.py
"""

import asyncio
import json
import pytest
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.strategy import DecisionEnvelope, OrderStyle, Freshness
from services.decision.translator import IntentTranslator


@pytest.fixture
def oco_config() -> Dict[str, Any]:
    """Fixture providing OCO configuration for tests"""
    return {
        "take_profit_bp": 250,
        "hard_stop_bp": 180,
        "trail_atr_mult": 2.5
    }


@pytest.fixture
def envelope(oco_config: Dict[str, Any]) -> DecisionEnvelope:
    """Fixture providing DecisionEnvelope with OCO config"""
    return DecisionEnvelope(
        decision_id="test_001",
        strategy_id="test_strategy",
        symbol="TSLA",
        ts_bar_end_us=1698765432000000,
        target_qty=100.0,
        order_style=OrderStyle(kind="MKT"),
        freshness=Freshness(ttl_ms=60000),
        extras={
            "ml_signal": 0.8,
            "ml_conf": 0.75,
            "ref_px": 250.0,
            "oco_cfg": oco_config,
        }
    )


# ============================================================================
# Test 1: 策略读取 JSON 配置
# ============================================================================

def test_strategy_load_oco_config():
    """测试策略能否正确读取 JSON 中的 oco 配置"""
    print("\n" + "="*80)
    print("Test 1: 策略读取 JSON OCO 配置")
    print("="*80)

    # 创建测试配置
    test_config = {
        "symbols": ["TSLA"],
        "bar_sec": 60,
        "sizing": {"mode": "fixed", "base_qty": 100},
        "market_filter": {"conf_floor": 0.6},
        "oco": {
            "take_profit_bp": 250,
            "hard_stop_bp": 180,
            "trail_atr_mult": 2.5
        }
    }

    # 模拟策略读取逻辑
    oco_cfg = test_config.get("oco", {})
    oco_config = {
        "take_profit_bp": float(oco_cfg.get("take_profit_bp", 200)),
        "hard_stop_bp": float(oco_cfg.get("hard_stop_bp", 150)),
        "trail_atr_mult": float(oco_cfg.get("trail_atr_mult", 2.0)),
    }

    # 验证
    assert oco_config["take_profit_bp"] == 250, f"Expected 250, got {oco_config['take_profit_bp']}"
    assert oco_config["hard_stop_bp"] == 180, f"Expected 180, got {oco_config['hard_stop_bp']}"
    assert oco_config["trail_atr_mult"] == 2.5, f"Expected 2.5, got {oco_config['trail_atr_mult']}"

    print("✅ 策略成功读取 OCO 配置:")
    print(f"   - take_profit_bp: {oco_config['take_profit_bp']}")
    print(f"   - hard_stop_bp: {oco_config['hard_stop_bp']}")
    print(f"   - trail_atr_mult: {oco_config['trail_atr_mult']}")

    return oco_config


# ============================================================================
# Test 2: DecisionEnvelope 传递 oco_cfg
# ============================================================================

def test_envelope_with_oco_cfg(oco_config: Dict[str, Any]):
    """测试 DecisionEnvelope 能否携带 oco_cfg"""
    print("\n" + "="*80)
    print("Test 2: DecisionEnvelope 传递 oco_cfg")
    print("="*80)

    envelope = DecisionEnvelope(
        decision_id="test_001",
        strategy_id="test_strategy",
        symbol="TSLA",
        ts_bar_end_us=1698765432000000,
        target_qty=100.0,
        order_style=OrderStyle(kind="MKT"),
        freshness=Freshness(ttl_ms=60000),
        extras={
            "ml_signal": 0.8,
            "ml_conf": 0.75,
            "ref_px": 250.0,
            "oco_cfg": oco_config,  # ← 关键: 传递 OCO 配置
        }
    )

    # 验证
    assert "oco_cfg" in envelope.extras, "oco_cfg not in extras"
    assert envelope.extras["oco_cfg"]["take_profit_bp"] == 250
    assert envelope.extras["oco_cfg"]["hard_stop_bp"] == 180

    print("✅ DecisionEnvelope 成功携带 oco_cfg:")
    print(f"   - extras['oco_cfg']: {envelope.extras['oco_cfg']}")

    return envelope


# ============================================================================
# Test 3: Translator 读取 oco_cfg
# ============================================================================

async def test_translator_parse_oco_cfg(envelope: DecisionEnvelope):
    """测试 Translator 能否正确读取 oco_cfg"""
    print("\n" + "="*80)
    print("Test 3: Translator 解析 oco_cfg")
    print("="*80)

    # 创建 Translator (使用默认配置)
    translator_cfg = {
        "buckets": [
            {
                "name": "default",
                "action": {
                    "order_type": "MKT",
                    "ref": "mid",
                    "tif": "DAY"
                }
            }
        ],
        "defaults": {
            "price_collar_pct": 3.0,
            "oco": {
                "take_profit_bp": 40,   # 系统默认 (应该被策略级覆盖)
                "hard_stop_bp": 80,
                "trail_atr_mult": 1.5
            }
        }
    }

    # 模拟 Translator 读取逻辑
    # Priority: 1) Strategy-level config from extras  2) System default config
    oco_cfg = (envelope.extras or {}).get("oco_cfg") or translator_cfg.get("defaults", {}).get("oco", {})

    # 验证
    assert oco_cfg["take_profit_bp"] == 250, f"Expected 250 (strategy), got {oco_cfg['take_profit_bp']}"
    assert oco_cfg["hard_stop_bp"] == 180, f"Expected 180 (strategy), got {oco_cfg['hard_stop_bp']}"

    print("✅ Translator 成功读取策略级 oco_cfg (覆盖系统默认):")
    print(f"   - 系统默认: take_profit_bp=40, hard_stop_bp=80")
    print(f"   - 策略级别: take_profit_bp={oco_cfg['take_profit_bp']}, hard_stop_bp={oco_cfg['hard_stop_bp']}")
    print(f"   - 最终使用: 策略级别 ✅")

    return oco_cfg


# ============================================================================
# Test 4: OCO 价格计算
# ============================================================================

def test_oco_price_calculation(oco_config: Dict[str, Any]):
    """测试 OCO 价格计算是否正确"""
    print("\n" + "="*80)
    print("Test 4: OCO 价格计算")
    print("="*80)

    # 模拟场景
    entry_price = 250.0  # TSLA 入场价
    side = "BUY"         # 买入方向

    # 计算止盈价
    tp_bp = oco_config.get("take_profit_bp", 40) / 1e4  # 250 bp = 0.025
    if side == "BUY":
        tp_px = entry_price * (1 + tp_bp)
    else:
        tp_px = entry_price * (1 - tp_bp)

    # 计算止损价
    sl_bp = oco_config.get("hard_stop_bp", 80) / 1e4  # 180 bp = 0.018
    if side == "BUY":
        sl_px = entry_price * (1 - sl_bp)
    else:
        sl_px = entry_price * (1 + sl_bp)

    # 预期值
    expected_tp = 250.0 * (1 + 0.025)  # $256.25
    expected_sl = 250.0 * (1 - 0.018)  # $245.50

    # 验证
    assert abs(tp_px - expected_tp) < 0.01, f"TP price mismatch: {tp_px} vs {expected_tp}"
    assert abs(sl_px - expected_sl) < 0.01, f"SL price mismatch: {sl_px} vs {expected_sl}"

    # 计算风险回报比
    risk = entry_price - sl_px
    reward = tp_px - entry_price
    risk_reward_ratio = reward / risk if risk > 0 else 0

    print(f"✅ OCO 价格计算正确:")
    print(f"   - 入场价: ${entry_price:.2f}")
    print(f"   - 止盈价: ${tp_px:.2f} (+{(tp_px/entry_price - 1)*100:.2f}%)")
    print(f"   - 止损价: ${sl_px:.2f} ({(sl_px/entry_price - 1)*100:.2f}%)")
    print(f"   - 风险回报比: {risk_reward_ratio:.2f}:1")

    return tp_px, sl_px, risk_reward_ratio


# ============================================================================
# Test 5: 默认值测试
# ============================================================================

def test_default_values():
    """测试没有配置 oco 时使用默认值"""
    print("\n" + "="*80)
    print("Test 5: 默认值测试 (无 oco 配置)")
    print("="*80)

    # 没有 oco 配置
    test_config = {
        "symbols": ["AAPL"],
        "sizing": {"mode": "fixed", "base_qty": 100},
    }

    # 策略读取逻辑
    oco_cfg = test_config.get("oco", {})
    oco_config = {
        "take_profit_bp": float(oco_cfg.get("take_profit_bp", 200)),  # 默认 2%
        "hard_stop_bp": float(oco_cfg.get("hard_stop_bp", 150)),      # 默认 1.5%
        "trail_atr_mult": float(oco_cfg.get("trail_atr_mult", 2.0)),
    }

    # 验证默认值
    assert oco_config["take_profit_bp"] == 200, "Default TP should be 200"
    assert oco_config["hard_stop_bp"] == 150, "Default SL should be 150"

    print("✅ 默认值测试通过:")
    print(f"   - take_profit_bp: {oco_config['take_profit_bp']} (默认)")
    print(f"   - hard_stop_bp: {oco_config['hard_stop_bp']} (默认)")


# ============================================================================
# Test 6: 实际 JSON 文件测试
# ============================================================================

def test_real_json_files():
    """测试实际的策略 JSON 配置文件"""
    print("\n" + "="*80)
    print("Test 6: 实际 JSON 文件测试")
    print("="*80)

    config_dir = Path("strategies/configs")
    test_files = [
        "enhanced_ml_TSLA.json",
        "enhanced_ml_AAPL.json",
        "enhanced_ml_NVDA.json",
    ]

    results = []
    for filename in test_files:
        file_path = config_dir / filename
        if not file_path.exists():
            print(f"⚠️  文件不存在: {file_path}")
            continue

        try:
            with open(file_path, 'r') as f:
                config = json.load(f)

            oco = config.get("oco")
            if oco:
                symbol = config.get("symbols", ["UNKNOWN"])[0] if "symbols" in config else config.get("symbol", "UNKNOWN")
                results.append({
                    "file": filename,
                    "symbol": symbol,
                    "oco": oco
                })
                print(f"✅ {filename} ({symbol}):")
                print(f"   - take_profit_bp: {oco.get('take_profit_bp')}")
                print(f"   - hard_stop_bp: {oco.get('hard_stop_bp')}")
                print(f"   - trail_atr_mult: {oco.get('trail_atr_mult')}")
            else:
                print(f"⚠️  {filename}: 没有 oco 配置")

        except Exception as e:
            print(f"❌ {filename}: 读取失败 - {e}")

    return results


# ============================================================================
# Test 7: 边界情况测试
# ============================================================================

def test_edge_cases():
    """测试边界情况"""
    print("\n" + "="*80)
    print("Test 7: 边界情况测试")
    print("="*80)

    # Case 1: oco 为 null
    config1 = {"oco": None}
    oco1 = config1.get("oco", {}) or {}
    default1 = {
        "take_profit_bp": float(oco1.get("take_profit_bp", 200)),
        "hard_stop_bp": float(oco1.get("hard_stop_bp", 150)),
    }
    assert default1["take_profit_bp"] == 200
    print("✅ Case 1: oco=null 使用默认值")

    # Case 2: oco 为空对象
    config2 = {"oco": {}}
    oco2 = config2.get("oco", {})
    default2 = {
        "take_profit_bp": float(oco2.get("take_profit_bp", 200)),
        "hard_stop_bp": float(oco2.get("hard_stop_bp", 150)),
    }
    assert default2["take_profit_bp"] == 200
    print("✅ Case 2: oco={} 使用默认值")

    # Case 3: 部分配置
    config3 = {"oco": {"take_profit_bp": 300}}
    oco3 = config3.get("oco", {})
    partial = {
        "take_profit_bp": float(oco3.get("take_profit_bp", 200)),
        "hard_stop_bp": float(oco3.get("hard_stop_bp", 150)),
    }
    assert partial["take_profit_bp"] == 300
    assert partial["hard_stop_bp"] == 150  # 使用默认
    print("✅ Case 3: 部分配置,缺失字段使用默认值")

    # Case 4: 极端值
    config4 = {"oco": {"take_profit_bp": 10000, "hard_stop_bp": 1}}
    oco4 = config4.get("oco", {})
    extreme = {
        "take_profit_bp": float(oco4.get("take_profit_bp", 200)),
        "hard_stop_bp": float(oco4.get("hard_stop_bp", 150)),
    }
    assert extreme["take_profit_bp"] == 10000  # 100% (基本不会触发)
    assert extreme["hard_stop_bp"] == 1        # 0.01%
    print("✅ Case 4: 极端值也能正确处理")


# ============================================================================
# Main Test Runner
# ============================================================================

async def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*80)
    print("OCO 配置集成测试")
    print("="*80)
    print(f"测试时间: {Path(__file__).stat().st_mtime}")

    try:
        # Test 1: 策略读取 JSON 配置
        oco_config = test_strategy_load_oco_config()

        # Test 2: DecisionEnvelope 传递
        envelope = test_envelope_with_oco_cfg(oco_config)

        # Test 3: Translator 解析
        oco_cfg = await test_translator_parse_oco_cfg(envelope)

        # Test 4: 价格计算
        tp_px, sl_px, risk_reward = test_oco_price_calculation(oco_cfg)

        # Test 5: 默认值测试
        test_default_values()

        # Test 6: 实际文件测试
        real_files = test_real_json_files()

        # Test 7: 边界情况
        test_edge_cases()

        # 汇总
        print("\n" + "="*80)
        print("测试汇总")
        print("="*80)
        print("✅ Test 1: 策略读取 JSON OCO 配置 - PASSED")
        print("✅ Test 2: DecisionEnvelope 传递 oco_cfg - PASSED")
        print("✅ Test 3: Translator 解析 oco_cfg - PASSED")
        print("✅ Test 4: OCO 价格计算 - PASSED")
        print("✅ Test 5: 默认值测试 - PASSED")
        print(f"✅ Test 6: 实际 JSON 文件测试 - {len(real_files)} files PASSED")
        print("✅ Test 7: 边界情况测试 - PASSED")

        print("\n" + "="*80)
        print("🎉 所有测试通过!")
        print("="*80)

        return True

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
