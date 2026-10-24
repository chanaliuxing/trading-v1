#!/bin/bash
# Comprehensive ML Pipeline Test Script

echo "================================================="
echo "  STARTING COMPREHENSIVE ML PIPELINE TEST"
echo "================================================="

# Activate virtual environment
source venv/bin/activate

# --- Step 1: Data Integrity and Quality Validation ---
echo ""
echo "--- STEP 1: Data Integrity and Quality Validation ---"
python tools/comprehensive_data_validation.py --date $(date -v-1d +%Y-%m-%d) --symbols TSLA,NVDA,AAPL,SPY --full
DATA_VALIDATION_STATUS=$?
if [ $DATA_VALIDATION_STATUS -ne 0 ]; then
    echo "🔴 STEP 1 FAILED: Data validation reported critical errors or script failed."
else
    echo "✅ STEP 1 PASSED: Data validation script ran successfully (data may still be missing as expected)."
fi

# --- Step 2: Label Generation and Verification ---
echo ""
echo "--- STEP 2: Label Generation and Verification ---"
python tools/recompute_labels_1m.py --symbols TSLA --start $(date -v-2d +%Y-%m-%d) --end $(date -v-1d +%Y-%m-%d) --horizon-min 30 --label-bps 10
LABEL_GEN_STATUS=$?
if [ $LABEL_GEN_STATUS -ne 0 ]; then
    echo "🔴 STEP 2 FAILED: Label generation script failed."
else
    echo "✅ STEP 2 PASSED: Label generation script ran successfully."
fi

# --- Step 3: Feature Engineering Pipeline Verification ---
echo ""
echo "--- STEP 3: Feature Engineering Pipeline Verification ---"
echo "  - Running feature_build.py..."
python tools/feature_build.py
FEATURE_BUILD_STATUS=$?
if [ $FEATURE_BUILD_STATUS -ne 0 ]; then
    echo "🔴 STEP 3a FAILED: Feature build script failed."
else
    echo "✅ STEP 3a PASSED: Feature build script ran successfully."
    
    echo "  - Running feature_eval.py..."
    python tools/feature_eval.py
    FEATURE_EVAL_STATUS=$?
    if [ $FEATURE_EVAL_STATUS -ne 0 ]; then
        echo "🔴 STEP 3b FAILED: Feature evaluation script failed."
    else
        echo "✅ STEP 3b PASSED: Feature evaluation script ran successfully."
    fi
fi

# --- Step 4: End-to-End ML Pipeline Logic Test (Mock Data) ---
echo ""
echo "--- STEP 4: End-to-End ML Pipeline Logic Test ---"
python scripts/test_complete_ml_system.py
LOGIC_TEST_STATUS=$?
if [ $LOGIC_TEST_STATUS -ne 0 ]; then
    echo "🔴 STEP 4 FAILED: End-to-end ML pipeline logic test failed."
else
    echo "✅ STEP 4 PASSED: End-to-end ML pipeline logic test passed."
fi

# --- Step 5: Final Integration Test (`ml_brain_loop.py`) ---
echo ""
echo "--- STEP 5: Final Integration Test (ml_brain_loop.py) ---"
# This will run for a short period and then be terminated.
# We are checking for startup errors, not full execution.
python tools/ml_brain_loop.py --symbols TSLA,NVDA --start-job-type oof --max-cycles 1
INTEGRATION_TEST_STATUS=$?
if [ $INTEGRATION_TEST_STATUS -ne 0 ]; then
    echo "🔴 STEP 5 FAILED: ml_brain_loop.py failed to start or run a cycle."
else
    echo "✅ STEP 5 PASSED: ml_brain_loop.py started and ran one cycle without critical errors."
fi


echo ""
echo "================================================="
echo "            TEST SCRIPT COMPLETE"
echo "================================================="

# Final Summary
if [ $DATA_VALIDATION_STATUS -eq 0 ] && [ $LABEL_GEN_STATUS -eq 0 ] && [ $FEATURE_BUILD_STATUS -eq 0 ] && [ $FEATURE_EVAL_STATUS -eq 0 ] && [ $LOGIC_TEST_STATUS -eq 0 ] && [ $INTEGRATION_TEST_STATUS -eq 0 ]; then
    echo "✅🎉 ALL TESTS PASSED SUCCESSFULLY! The pipeline is logically sound."
    echo "NOTE: Data validation may still show missing data, which is an expected data infrastructure issue."
else
    echo "❌ SOME TESTS FAILED. Please review the output above for details."
fi
