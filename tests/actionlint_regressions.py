"""Real-tool regressions: put pinned actionlint on PATH, then run this file explicitly."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('workflow_lint', ROOT / 'scripts/workflow_lint.py')
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)
FIXTURES = ROOT / 'tests/fixtures/workflow_lint'


@pytest.mark.parametrize('caller,diagnostic', [
    ('caller-missing-input.yml', 'input "required_value" is required'),
    ('caller-unknown-input.yml', 'input "unknown" is not defined'),
    ('caller-missing-workflow.yml', 'could not read reusable workflow file'),
    ('caller-valid.yml', None),
])
def test_local_reusable_workflow_metadata(caller, diagnostic):
    files = {
        '.github/workflows/caller.yml': (FIXTURES / caller).read_text(),
        '.github/workflows/reusable.yml': (FIXTURES / 'reusable.yml').read_text(),
    }
    result = lint.run_actionlint(files, ['.github/workflows/caller.yml'], 'actionlint')
    assert result['files'] == ['.github/workflows/caller.yml']
    assert result['exit_code'] == (1 if diagnostic else 0), result['output']
    if diagnostic:
        assert diagnostic in result['output']
