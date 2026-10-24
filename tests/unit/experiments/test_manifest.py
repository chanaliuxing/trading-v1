"""Tests for experiment manifest module."""

import json
import tempfile
from pathlib import Path

import pytest

from experiments.manifest import (
    ExperimentManifest,
    generate_job_id,
    create_manifest,
    save_manifest,
)


class TestGenerateJobId:
    """Test job ID generation."""

    def test_basic_job_id(self):
        """Test basic job ID format."""
        job_id = generate_job_id(
            symbol="TSLA",
            freq="1m",
            label_id="ret_10b_fwd",
            start_ts="2024-01-01",
            end_ts="2024-03-01"
        )
        assert job_id == "TSLA_1m_ret_10b_fwd_2024-01-01_2024-03-01"

    def test_special_chars_in_label(self):
        """Test that slashes in label_id are replaced."""
        job_id = generate_job_id(
            symbol="AAPL",
            freq="5m",
            label_id="ret/10b/fwd",
            start_ts="2024-01-01",
            end_ts="2024-02-01"
        )
        assert "/" not in job_id
        assert "ret-10b-fwd" in job_id


class TestCreateManifest:
    """Test manifest creation."""

    def test_create_basic_manifest(self):
        """Test creating a basic manifest."""
        manifest = create_manifest(
            symbol="NVDA",
            freq="1m",
            label_id="ret_10b_fwd",
            start_ts="2024-01-01",
            end_ts="2024-03-01",
            data_source={"table": "trading.bars_1m"},
            feature_recipe={"base_features": ["close", "volume"]},
            training_plan={"task": "reg", "model": "xgb"},
        )

        assert isinstance(manifest, ExperimentManifest)
        assert manifest.symbol == "NVDA"
        assert manifest.freq == "1m"
        assert manifest.label_id == "ret_10b_fwd"
        assert "NVDA_1m_ret_10b_fwd" in manifest.job_id

    def test_manifest_with_sanity_summary(self):
        """Test manifest with sanity summary."""
        sanity_summary = {
            "overall_status": "ok",
            "issues": [],
            "basic": {"n_rows": 1000, "n_cols": 10}
        }

        manifest = create_manifest(
            symbol="TSLA",
            freq="1m",
            label_id="ret_10b_fwd",
            start_ts="2024-01-01",
            end_ts="2024-02-01",
            data_source={"table": "trading.bars_1m"},
            feature_recipe={},
            training_plan={},
            sanity_summary=sanity_summary,
        )

        assert manifest.sanity_summary == sanity_summary
        assert manifest.sanity_summary["overall_status"] == "ok"

    def test_manifest_with_notes_and_meta(self):
        """Test manifest with notes and metadata."""
        manifest = create_manifest(
            symbol="AAPL",
            freq="1m",
            label_id="ret_10b_fwd",
            start_ts="2024-01-01",
            end_ts="2024-02-01",
            data_source={"table": "test"},
            feature_recipe={},
            training_plan={},
            notes="This is a test experiment",
            meta={"experiment_version": "v2", "researcher": "PI"}
        )

        assert manifest.notes == "This is a test experiment"
        assert manifest.meta["experiment_version"] == "v2"
        assert manifest.meta["researcher"] == "PI"


class TestSaveManifest:
    """Test manifest saving."""

    def test_save_and_load_manifest(self):
        """Test saving manifest to file and loading it back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = create_manifest(
                symbol="TSLA",
                freq="1m",
                label_id="ret_10b_fwd",
                start_ts="2024-01-01",
                end_ts="2024-02-01",
                data_source={"table": "trading.bars_1m"},
                feature_recipe={"base_features": ["close"]},
                training_plan={"task": "reg"},
            )

            # Save manifest
            manifest_path = save_manifest(manifest, base_dir=tmpdir)

            # Verify file exists
            assert manifest_path.exists()
            assert manifest_path.suffix == ".json"

            # Load and verify
            loaded_data = json.loads(manifest_path.read_text())
            assert loaded_data["symbol"] == "TSLA"
            assert loaded_data["label_id"] == "ret_10b_fwd"
            assert loaded_data["feature_recipe"]["base_features"] == ["close"]

    def test_save_creates_directory(self):
        """Test that save_manifest creates directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent_dir = Path(tmpdir) / "manifests" / "subdir"
            assert not nonexistent_dir.exists()

            manifest = create_manifest(
                symbol="AAPL",
                freq="1m",
                label_id="ret_10b_fwd",
                start_ts="2024-01-01",
                end_ts="2024-02-01",
                data_source={"table": "test"},
                feature_recipe={},
                training_plan={},
            )

            manifest_path = save_manifest(manifest, base_dir=str(nonexistent_dir))
            assert manifest_path.exists()
            assert nonexistent_dir.exists()


class TestManifestToDict:
    """Test manifest serialization."""

    def test_to_dict(self):
        """Test converting manifest to dictionary."""
        manifest = create_manifest(
            symbol="NVDA",
            freq="1m",
            label_id="ret_10b_fwd",
            start_ts="2024-01-01",
            end_ts="2024-02-01",
            data_source={"table": "trading.bars_1m"},
            feature_recipe={"base_features": ["close", "volume"]},
            training_plan={"task": "reg"},
        )

        manifest_dict = manifest.to_dict()

        assert isinstance(manifest_dict, dict)
        assert manifest_dict["symbol"] == "NVDA"
        assert manifest_dict["freq"] == "1m"
        assert "created_at" in manifest_dict
        assert "job_id" in manifest_dict
