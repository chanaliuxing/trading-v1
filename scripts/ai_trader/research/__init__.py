"""
Research pipeline scaffolding (state + policy + agent + orchestrator).

This package provides a thin glue layer to connect ResearchState, ResearchPolicy,
an LLM-driven ResearchAgent, and an ExperimentOrchestrator. The goal is to make
the meta-AI loop explicit and testable without changing the existing training
pipelines.
"""

