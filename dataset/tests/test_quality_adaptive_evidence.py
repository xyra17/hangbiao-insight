import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from run_quality_adaptive_evidence import passed_count


def test_actual_success_count_parses_complete_log(tmp_path):
    log = tmp_path / 'test.log'
    log.write_text('81 passed in 3.21s\n')
    assert passed_count(log) == 81


@pytest.mark.parametrize('text', ['3 passed, 1 failed', '3 passed, 1 error', '3 passed\nERROR collecting', '3 passed\nTraceback error', 'plain successful execution'])
def test_partial_success_does_not_hide_failures_or_setup_errors(tmp_path, text):
    log = tmp_path / 'test.log'
    log.write_text(text)
    with pytest.raises(ValueError, match='regression result'):
        passed_count(log)
