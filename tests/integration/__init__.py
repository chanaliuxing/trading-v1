"""Integration test package marker.

This prevents subfolders like `tests/integration/strategies` from being treated as a top-level
package named `strategies`, which can shadow the real `strategies/` package in the repo root.
"""

