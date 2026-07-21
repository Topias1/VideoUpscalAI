"""The disk-space check must account for concurrently processed segments.

estimate_disk_usage sizes a single segment, but each worker keeps its own
raw + upscaled frame set on disk simultaneously. Checking one segment's worth
while running N workers under-reports the requirement by a factor of N.
"""
from unittest.mock import patch

import pytest

from upscaler import DiskEstimateError
from upscaler.plan import verify_disk_space


ONE_SEGMENT = 10 * 1024 ** 3   # 10 GB of transient frames per segment
SOURCE_SIZE = 1 * 1024 ** 3


def free_space(gb):
    """shutil.disk_usage returns (total, used, free)."""
    return (100 * 1024 ** 3, 0, int(gb * 1024 ** 3))


@patch("upscaler.plan.shutil.disk_usage")
def test_single_segment_fits(mock_usage, tmp_path):
    mock_usage.return_value = free_space(20)
    verify_disk_space(str(tmp_path), ONE_SEGMENT, SOURCE_SIZE)


@patch("upscaler.plan.shutil.disk_usage")
def test_four_workers_need_four_times_the_transient_space(mock_usage, tmp_path):
    """20 GB free fits one segment but not four in flight."""
    mock_usage.return_value = free_space(20)
    with pytest.raises(DiskEstimateError):
        verify_disk_space(str(tmp_path), ONE_SEGMENT * 4, SOURCE_SIZE)


@patch("upscaler.plan.shutil.disk_usage")
def test_error_message_reports_the_scaled_requirement(mock_usage, tmp_path):
    mock_usage.return_value = free_space(5)
    with pytest.raises(DiskEstimateError) as exc:
        verify_disk_space(str(tmp_path), ONE_SEGMENT * 2, SOURCE_SIZE)
    # 2 x 10 GB transient + 2.5 x 1 GB source = 22.5 GB
    assert "22.5" in str(exc.value)
