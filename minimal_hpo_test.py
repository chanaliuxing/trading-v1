#!/usr/bin/env python3
"""
Minimal test to isolate where HPO hangs
"""
import sys
print("[1] Starting minimal test", flush=True)

# Test imports one by one
print("[2] Importing os", flush=True)
import os

print("[3] Importing optuna", flush=True)
import optuna

print("[4] Importing boot_logging", flush=True)
import boot_logging  # noqa: F401

print("[5] Creating study", flush=True)
study = optuna.create_study(
    direction="maximize",
    sampler=optuna.samplers.TPESampler(seed=42),
    storage="sqlite:///minimal_test.db",
    study_name="minimal_test",
    load_if_exists=True
)

print("[6] Study created successfully", flush=True)

def objective(trial):
    print(f"[TRIAL] Trial {trial.number} started", flush=True)
    return 1.0

print("[7] Starting optimization", flush=True)
study.optimize(objective, n_trials=1, show_progress_bar=False)

print("[8] Optimization complete", flush=True)
