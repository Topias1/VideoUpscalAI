"""--realesrgan-bin must actually select the binary it names.

find_realesrgan() used to accept custom_path and then ignore it, always
returning the bundled upscayl-bin. The integration suite passed a stub and
silently drove the real GPU model instead, which is why it took minutes and
left .work_* directories behind.
"""
import os
import stat

import pytest

from upscaler import ToolError
from upscaler.tools import find_realesrgan


@pytest.fixture
def fake_bin(tmp_path):
    p = tmp_path / "stub_realesrgan.py"
    p.write_text("#!/usr/bin/env python3\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_custom_path_is_used(fake_bin):
    assert find_realesrgan(str(fake_bin)) == os.path.abspath(str(fake_bin))


def test_custom_path_expands_user(fake_bin, monkeypatch):
    monkeypatch.setenv("HOME", str(fake_bin.parent))
    assert find_realesrgan("~/stub_realesrgan.py") == os.path.abspath(str(fake_bin))


def test_missing_custom_path_raises(tmp_path):
    with pytest.raises(ToolError, match="not found"):
        find_realesrgan(str(tmp_path / "nope"))


def test_non_executable_custom_path_raises(tmp_path):
    p = tmp_path / "not_exec"
    p.write_text("x")
    p.chmod(0o644)
    with pytest.raises(ToolError, match="not executable"):
        find_realesrgan(str(p))


def test_no_custom_path_falls_back_to_bundled():
    """Default behaviour must be unchanged when no override is given."""
    assert find_realesrgan().endswith("upscayl-bin")
