"""Tests for the Claude Code presence probe."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cc_usagemonitor import claude_detection as cd


def test_status_is_installed_when_either_signal_true():
    assert cd.ClaudeStatus(True, True).is_installed is True
    assert cd.ClaudeStatus(True, False).is_installed is True
    assert cd.ClaudeStatus(False, True).is_installed is True
    assert cd.ClaudeStatus(False, False).is_installed is False


def test_status_is_missing_only_when_both_false():
    assert cd.ClaudeStatus(False, False).is_missing is True
    assert cd.ClaudeStatus(True, False).is_missing is False
    assert cd.ClaudeStatus(False, True).is_missing is False


def test_has_project_data_returns_false_for_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path / "nonexistent")
    assert cd._has_project_data() is False


def test_has_project_data_returns_false_for_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path)
    assert cd._has_project_data() is False


def test_has_project_data_returns_false_when_subdirs_have_no_jsonl(
    tmp_path, monkeypatch,
):
    proj = tmp_path / "-some-project"
    proj.mkdir()
    (proj / "stale.txt").write_text("not jsonl")
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path)
    assert cd._has_project_data() is False


def test_has_project_data_returns_true_with_one_jsonl(tmp_path, monkeypatch):
    proj = tmp_path / "-some-project"
    proj.mkdir()
    (proj / "abc.jsonl").write_text("{}\n")
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path)
    assert cd._has_project_data() is True


def test_detect_combines_both_signals(tmp_path, monkeypatch):
    # No binary, no data → missing.
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path / "missing")
    monkeypatch.setattr("shutil.which", lambda name: None)
    status = cd.detect_claude_install()
    assert status.binary_in_path is False
    assert status.has_project_data is False
    assert status.is_missing is True


def test_detect_with_binary_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path / "missing")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    status = cd.detect_claude_install()
    assert status.binary_in_path is True
    assert status.has_project_data is False
    assert status.is_installed is True
    assert status.is_missing is False


def test_detect_with_data_only(tmp_path, monkeypatch):
    proj = tmp_path / "-x"
    proj.mkdir()
    (proj / "a.jsonl").write_text("{}\n")
    monkeypatch.setattr(cd, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)
    status = cd.detect_claude_install()
    assert status.binary_in_path is False
    assert status.has_project_data is True
    assert status.is_installed is True
