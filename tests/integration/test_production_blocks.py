#!/usr/bin/env python3
"""
Test script to verify production safety blocks for ML inference services.

This script tests that:
1. RealMLInferenceService and MockMLInferenceService can be instantiated in dev mode
2. Both services are blocked in production mode with clear error messages
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


def test_dev_mode():
    """Test that services can be instantiated in dev mode."""
    print("\n" + "=" * 70)
    print("TEST 1: Dev Mode (ENV_MODE=dev) - Services should initialize")
    print("=" * 70)

    os.environ["ENV_MODE"] = "dev"

    # Test MockMLInferenceService
    try:
        from services.ml.mock_inference_service import MockMLInferenceService
        service = MockMLInferenceService(seed=42)
        print("✅ MockMLInferenceService: Initialized successfully in dev mode")
    except Exception as e:
        print(f"❌ MockMLInferenceService: Failed in dev mode: {e}")
        return False

    # Test RealMLInferenceService (may fail due to CH connection, but should not fail due to ENV_MODE)
    try:
        from services.ml.real_inference_service import RealMLInferenceService
        service = RealMLInferenceService()
        print("✅ RealMLInferenceService: Initialized successfully in dev mode")
    except RuntimeError as e:
        if "disabled in production" in str(e):
            print(f"❌ RealMLInferenceService: Blocked incorrectly in dev mode: {e}")
            return False
        else:
            print(f"⚠️  RealMLInferenceService: Failed for other reason (OK): {e}")
    except Exception as e:
        print(f"⚠️  RealMLInferenceService: Failed for other reason (OK): {e}")

    return True


def test_production_mode():
    """Test that services are blocked in production mode."""
    print("\n" + "=" * 70)
    print("TEST 2: Production Mode (ENV_MODE=production) - Services should be blocked")
    print("=" * 70)

    os.environ["ENV_MODE"] = "production"

    # Need to reload modules to pick up new ENV_MODE
    import importlib

    # Test MockMLInferenceService
    try:
        import services.ml.mock_inference_service as mock_module
        importlib.reload(mock_module)

        service = mock_module.MockMLInferenceService(seed=42)
        print("❌ MockMLInferenceService: Should have been blocked in production!")
        return False
    except RuntimeError as e:
        error_msg = str(e).lower()
        if "disabled" in error_msg and ("production" in error_msg or "live" in error_msg or "paper" in error_msg):
            print(f"✅ MockMLInferenceService: Correctly blocked in production")
            print(f"   Error message: {e}")
        else:
            print(f"❌ MockMLInferenceService: Blocked with wrong error: {e}")
            return False
    except Exception as e:
        print(f"❌ MockMLInferenceService: Unexpected error: {e}")
        return False

    # Test RealMLInferenceService
    try:
        import services.ml.real_inference_service as real_module
        importlib.reload(real_module)

        service = real_module.RealMLInferenceService()
        print("❌ RealMLInferenceService: Should have been blocked in production!")
        return False
    except RuntimeError as e:
        error_msg = str(e).lower()
        if "disabled" in error_msg and "production" in error_msg:
            print(f"✅ RealMLInferenceService: Correctly blocked in production")
            print(f"   Error message: {e}")
        else:
            print(f"❌ RealMLInferenceService: Blocked with wrong error: {e}")
            return False
    except Exception as e:
        print(f"❌ RealMLInferenceService: Unexpected error: {e}")
        return False

    return True


def test_prod_alias():
    """Test that 'prod', 'live', and 'paper' aliases also block services."""
    print("\n" + "=" * 70)
    print("TEST 3: Production Aliases (ENV_MODE=prod/live/paper) - Services should be blocked")
    print("=" * 70)

    import importlib

    for env_value in ["prod", "live", "paper", "PRODUCTION", "PROD", "LIVE", "PAPER"]:
        os.environ["ENV_MODE"] = env_value

        try:
            import services.ml.mock_inference_service as mock_module
            importlib.reload(mock_module)

            service = mock_module.MockMLInferenceService(seed=42)
            print(f"❌ MockMLInferenceService: Should have been blocked for ENV_MODE={env_value}")
            return False
        except RuntimeError as e:
            if "disabled in" in str(e).lower() and ("production" in str(e).lower() or "paper" in str(e).lower() or "live" in str(e).lower()):
                print(f"✅ MockMLInferenceService: Correctly blocked for ENV_MODE={env_value}")
            else:
                print(f"❌ MockMLInferenceService: Wrong error for ENV_MODE={env_value}: {e}")
                return False

    return True


def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("PRODUCTION SAFETY BLOCK VERIFICATION")
    print("=" * 70)
    print("\nTesting that ML inference services are correctly blocked in production...")

    # Store original ENV_MODE
    original_env = os.environ.get("ENV_MODE")

    try:
        # Run tests
        test1_pass = test_dev_mode()
        test2_pass = test_production_mode()
        test3_pass = test_prod_alias()

        # Summary
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        print(f"Test 1 (Dev Mode):           {'✅ PASS' if test1_pass else '❌ FAIL'}")
        print(f"Test 2 (Production Mode):    {'✅ PASS' if test2_pass else '❌ FAIL'}")
        print(f"Test 3 (Production Aliases): {'✅ PASS' if test3_pass else '❌ FAIL'}")

        all_pass = test1_pass and test2_pass and test3_pass
        print("\n" + "=" * 70)
        if all_pass:
            print("✅ ALL TESTS PASSED - Production safety blocks are working correctly!")
        else:
            print("❌ SOME TESTS FAILED - Review implementation")
        print("=" * 70)

        return 0 if all_pass else 1

    finally:
        # Restore original ENV_MODE
        if original_env is not None:
            os.environ["ENV_MODE"] = original_env
        elif "ENV_MODE" in os.environ:
            del os.environ["ENV_MODE"]


if __name__ == "__main__":
    sys.exit(main())
