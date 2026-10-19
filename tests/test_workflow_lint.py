import datetime
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('workflow_lint', ROOT / 'scripts/workflow_lint.py')
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)
CONFIG = json.loads((ROOT / 'config/workflow-lint.json').read_text())
TODAY = datetime.date(2026, 10, 19)


@pytest.mark.parametrize('case', json.loads((ROOT / 'tests/fixtures/workflow_lint/cases.json').read_text()), ids=lambda case: case['name'])
def test_policy_fixtures(case):
    findings = lint.lint_files(case['files'], case.get('repository', 'dflippojr/example'), case.get('visibility', 'public'), CONFIG, TODAY)
    assert [(item['severity'], item['rule']) for item in findings] == [tuple(item) for item in case['expected']]


@pytest.mark.parametrize('tool,option', [('pytest', '-n'), ('pytest', '--numprocesses'), ('playwright test', '--workers')])
@pytest.mark.parametrize('count', ['2', '4', '5', 'auto', '50%'])
def test_command_caps(tool, option, count):
    findings = lint.parallelism_findings('ci-local.sh', f'{tool} {option}={count}')
    assert any(f['severity'] == 'error' for f in findings) == (count in {'5', 'auto', '50%'})


def test_exception_expires_and_cannot_move_or_drop_fork_guard():
    entry = CONFIG['review_exceptions'][0]
    job = {'if': entry['fork_guard']}
    args = (CONFIG, entry['repository'], entry['workflow'], entry['job'], job)
    document = {'on': 'pull_request'}
    assert lint.exception_allowed(*args, TODAY, document)
    assert not lint.exception_allowed(*args, datetime.date(2027, 4, 12), document)
    assert not lint.exception_allowed(CONFIG, entry['repository'], entry['workflow'], 'build', job, TODAY, document)
    assert not lint.exception_allowed(CONFIG, entry['repository'], '.github/workflows/ci.yml', entry['job'], job, TODAY, document)
    assert not lint.exception_allowed(CONFIG, 'someone/agent-harness', entry['workflow'], entry['job'], job, TODAY, document)
    assert not lint.exception_allowed(CONFIG, entry['repository'], entry['workflow'], entry['job'], {}, TODAY, document)


def test_recursive_yaml_matrix_cannot_loop():
    document = yaml.safe_load('matrix: &matrix [*matrix, self-hosted]')
    assert list(lint.strings(document)) == ['self-hosted']


REAL = ROOT / 'tests/fixtures/workflow_lint/real'
ALLOWED_EVENTS = {
    'dflippojr/agent-harness': {'pull_request', 'workflow_dispatch', 'workflow_call'},
    'dflippojr/agent-harness-hub': {'pull_request_target', 'workflow_dispatch'},
}
EVENTS = ['pull_request', 'pull_request_target', 'workflow_dispatch', 'workflow_call', 'repository_dispatch', 'workflow_run', 'push']


def review_job(entry):
    return f'jobs:\n  review:\n    if: {entry["fork_guard"]}\n    runs-on: self-hosted\n    steps:\n      - run: echo review\n'


def review_findings(entry, trigger, extra=None):
    files = {entry['workflow']: f'on: {trigger}\n' + review_job(entry), **(extra or {})}
    return lint.lint_files(files, entry['repository'], 'public', CONFIG, TODAY)


def test_exception_events_are_the_ones_each_real_review_workflow_declares():
    assert {entry['repository']: set(entry['allowed_events']) for entry in CONFIG['review_exceptions']} == ALLOWED_EVENTS


@pytest.mark.parametrize('event', EVENTS)
@pytest.mark.parametrize('form', ['{event}', '[{event}]', '{{{event}: null}}', '{{{event}: {{types: [opened]}}}}'], ids=['scalar', 'list', 'mapping', 'mapping-with-types'])
@pytest.mark.parametrize('entry', CONFIG['review_exceptions'], ids=lambda entry: entry['repository'])
def test_review_exception_admits_exactly_its_own_events(entry, form, event):
    findings = review_findings(entry, form.format(event=event))
    assert [item['rule'] for item in findings] == ([] if event in ALLOWED_EVENTS[entry['repository']] else ['public-self-hosted'])


@pytest.mark.parametrize('unsafe', [event for event in EVENTS if event not in {'pull_request', 'workflow_dispatch'}])
@pytest.mark.parametrize('entry', CONFIG['review_exceptions'], ids=lambda entry: entry['repository'])
def test_review_exception_rejects_an_unapproved_event_next_to_an_approved_one(entry, unsafe):
    if unsafe in ALLOWED_EVENTS[entry['repository']]:
        pytest.skip('approved for this repository')
    assert [item['rule'] for item in review_findings(entry, f'[workflow_dispatch, {unsafe}]')] == ['public-self-hosted']


REAL_REVIEWS = [
    ('dflippojr/agent-harness', 'agent-harness-review.yml', 'daf8267f499520b3d942b2b45342cd8c99d822bb'),
    ('dflippojr/agent-harness-hub', 'agent-harness-hub-review.yml', '5d82be89d6445e243b205f0a80b23ff3d19317bb'),
]


@pytest.mark.parametrize('repository,name,revision', REAL_REVIEWS, ids=lambda value: value if value.startswith('dflippojr') else None)
def test_real_allowlisted_review_workflows_pass(repository, name, revision):
    """Copied byte for byte from each repository's default branch at `revision`."""
    content = (REAL / name).read_bytes().decode('utf-8')
    assert lint.lint_files({'.github/workflows/review.yml': content}, repository, 'public', CONFIG, TODAY) == []


@pytest.mark.parametrize('repository,name,old,new', [
    ('dflippojr/agent-harness', 'agent-harness-review.yml', '  pull_request:\n    types: [opened]\n', '  pull_request_target:\n    types: [opened]\n'),
    ('dflippojr/agent-harness', 'agent-harness-review.yml', '  workflow_dispatch:\n', '  workflow_run:\n    workflows: [CI]\n  workflow_dispatch:\n'),
    ('dflippojr/agent-harness-hub', 'agent-harness-hub-review.yml', '  pull_request_target:\n', '  pull_request:\n'),
    ('dflippojr/agent-harness-hub', 'agent-harness-hub-review.yml', '  workflow_dispatch:\n', '  workflow_run:\n    workflows: [CI]\n  workflow_dispatch:\n'),
    ('dflippojr/agent-harness-hub', 'agent-harness-hub-review.yml', "      github.event_name == 'workflow_dispatch' ||\n", ''),
], ids=['harness-pull_request_target', 'harness-workflow_run', 'hub-pull_request', 'hub-workflow_run', 'hub-guard-dropped'])
def test_real_review_workflows_fail_when_only_the_trigger_or_guard_changes(repository, name, old, new):
    content = (REAL / name).read_bytes().decode('utf-8')
    assert content.count(old) == 1
    findings = lint.lint_files({'.github/workflows/review.yml': content.replace(old, new)}, repository, 'public', CONFIG, TODAY)
    assert [item['rule'] for item in findings] == ['public-self-hosted']


CALLER = 'on: {trigger}\njobs:\n  review:\n    uses: {uses}\n'
LOCAL_USES = ['./.github/workflows/review.yml', 'dflippojr/agent-harness/.github/workflows/review.yml@review-v3']


@pytest.mark.parametrize('uses', LOCAL_USES)
@pytest.mark.parametrize('trigger,rules', [
    ('pull_request', []), ('[pull_request, workflow_dispatch]', []),
    ('pull_request_target', ['review-exception-caller']),
    ('[pull_request, pull_request_target]', ['review-exception-caller']),
    ('{issue_comment: {types: [created]}}', ['review-exception-caller']),
    ('push', ['review-exception-caller']),
])
def test_local_callers_of_an_excepted_workflow_use_only_its_events(uses, trigger, rules):
    """A caller that omits runs_on is skipped by the runner rule, so its own trigger must be checked."""
    entry = CONFIG['review_exceptions'][0]
    extra = {'.github/workflows/ci.yml': CALLER.format(trigger=trigger, uses=uses)}
    findings = review_findings(entry, 'workflow_call', extra)
    assert [item['rule'] for item in findings] == rules


def test_callers_of_callers_are_followed():
    entry = CONFIG['review_exceptions'][0]
    extra = {
        '.github/workflows/mid.yml': CALLER.format(trigger='workflow_call', uses=LOCAL_USES[0]),
        '.github/workflows/top.yml': CALLER.format(trigger='pull_request_target', uses='./.github/workflows/mid.yml'),
    }
    assert [(item['path'], item['rule']) for item in review_findings(entry, 'workflow_call', extra)] == [('.github/workflows/top.yml', 'review-exception-caller')]


def test_callers_are_only_checked_in_the_excepted_repository_and_for_that_workflow():
    entry = CONFIG['review_exceptions'][0]
    caller = CALLER.format(trigger='pull_request_target', uses=LOCAL_USES[0])
    assert lint.lint_files({'.github/workflows/ci.yml': caller}, 'someone/else', 'public', CONFIG, TODAY) == []
    other = CALLER.format(trigger='pull_request_target', uses='./.github/workflows/other.yml')
    assert review_findings(entry, 'workflow_call', {'.github/workflows/ci.yml': other}) == []


def test_runner_json_escapes_and_matrix_include():
    runner = "${{ fromJSON('[\"\\u0073elf-hosted\"]') }}"
    job = {'runs-on': runner}
    results = lint.runner_findings('ci.yml', {'jobs': {'test': job}}, 'a/b', 'public', CONFIG, TODAY)
    assert results[0]['rule'] == 'public-self-hosted'
    matrix_job = {'runs-on': '${{ matrix.os }}', 'strategy': {'matrix': {'os': ['ubuntu-latest'], 'note': ['self-hosted']}}}
    assert not lint.runner_findings('ci.yml', {'jobs': {'test': matrix_job}}, 'a/b', 'public', CONFIG, TODAY)
    matrix_job['strategy']['matrix']['include'] = [{'os': 'self-hosted'}]
    assert lint.runner_findings('ci.yml', {'jobs': {'test': matrix_job}}, 'a/b', 'public', CONFIG, TODAY)[0]['rule'] == 'public-self-hosted'
    matrix_job['strategy']['matrix'] = {'os': ['["\\u0073elf-hosted"]']}
    matrix_job['runs-on'] = '${{ fromJSON(matrix.os) }}'
    assert lint.runner_findings('ci.yml', {'jobs': {'test': matrix_job}}, 'a/b', 'public', CONFIG, TODAY)[0]['rule'] == 'public-self-hosted'


def runner_job(runs_on, extra=''):
    return f'on: pull_request\njobs:\n  test:\n    runs-on: {runs_on}\n{extra}    steps:\n      - run: echo ok\n'


def rules(files, visibility='public', repository='dflippojr/example', config=CONFIG):
    return [(item['severity'], item['rule']) for item in lint.lint_files(files, repository, visibility, config, TODAY)]


CI = '.github/workflows/ci.yml'
PLACEMENT = [('error', 'public-self-hosted')]


@pytest.mark.parametrize('runs_on', [
    '[Windows, X64]', 'agent-harness-review', '[linux, x64]', '[ubuntu-latest, gpu]', 'tower',
    '[Self-Hosted, Linux]', '{group: tower-pool}', '{group: tower-pool, labels: [linux]}',
    '${{ fromJSON(\'["Windows","X64"]\') }}', '${{ fromJSON(vars.RUNS_ON || \'["linux"]\') }}',
    '${{ fromJSON(vars.RUNS_ON || \'["\\u0057indows"]\') }}', "${{ github.event.pull_request.head.repo.fork && 'ubuntu-latest' || 'tower' }}",
    '[ubuntu-latest, "ubuntu-latest-custom"]', 'ubuntu', 'windows', 'macos',
], ids=lambda value: value[:40])
def test_public_repositories_reject_runner_labels_that_are_not_github_hosted(runs_on):
    assert rules({CI: runner_job(runs_on)}) == PLACEMENT


@pytest.mark.parametrize('runs_on', [
    'ubuntu-latest', 'ubuntu-24.04', 'ubuntu-22.04-arm', 'windows-latest', 'windows-2022', 'windows-11-arm', 'macos-15', 'macos-latest',
    '[ubuntu-latest]', 'Ubuntu-Latest', '${{ fromJSON(vars.CI_RUNS_ON || \'["ubuntu-latest"]\') }}',
    "${{ github.event.pull_request.head.repo.fork && 'ubuntu-latest' || 'ubuntu-24.04' }}",
])
def test_public_repositories_accept_github_hosted_labels(runs_on):
    assert rules({CI: runner_job(runs_on)}) == []


def test_public_matrix_runner_values_must_all_be_github_hosted():
    job = '    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [%s]\n'
    body = lambda labels: f'on: pull_request\njobs:\n  test:\n{job % labels}    steps:\n      - run: echo ok\n'
    assert rules({CI: body('ubuntu-latest, windows-latest')}) == []
    assert rules({CI: body('ubuntu-latest, [Windows, X64]')}) == PLACEMENT
    assert rules({CI: body('ubuntu-latest, tower')}) == PLACEMENT


def test_configured_larger_runner_names_are_github_hosted():
    custom = {**CONFIG, 'hosted_runner_labels': ['ubuntu-latest-8-cores']}
    files = {CI: runner_job('ubuntu-latest-8-cores')}
    assert rules(files) == PLACEMENT
    assert rules(files, config=custom) == []
    assert rules({CI: runner_job('[ubuntu-latest-8-cores, tower]')}, config=custom) == PLACEMENT


@pytest.mark.parametrize('runs_on', ['[Windows, X64]', 'agent-harness-review', '[self-hosted, tower]'])
def test_private_repositories_may_use_any_runner_labels(runs_on):
    assert rules({CI: runner_job(runs_on)}, visibility='private') == []


@pytest.mark.parametrize('runs_on', ['${{ inputs.runner }}', '${{ vars.RUNNER }}'])
def test_unresolvable_public_runner_expressions_still_warn(runs_on):
    assert rules({CI: runner_job(runs_on)}) == [('warning', 'dynamic-runner')]


def reusable_call(with_lines, uses='./.github/workflows/reusable.yml'):
    return f'on: pull_request\njobs:\n  call:\n    uses: {uses}\n    with:\n{with_lines}'


@pytest.mark.parametrize('with_lines', [
    "      runs_on: '[\"Windows\",\"X64\",\"agent-harness-review\"]'\n",
    '      runs_on: agent-harness-review\n',
    '      runs-on: tower\n',
    '      runner: self-hosted\n',
    "      runner: '[\"self-hosted\"]'\n",
    '      labels: [linux, x64]\n',
    "      runs_on: '[\"ubuntu-latest\",\"gpu\"]'\n",
    '      pool: self-hosted\n',
    "      pool: ['Self-Hosted']\n",
])
@pytest.mark.parametrize('uses', ['./.github/workflows/reusable.yml', 'someone/else/.github/workflows/review.yml@main'])
def test_public_reusable_calls_reject_runner_inputs_under_any_runner_style_name(uses, with_lines):
    assert rules({CI: reusable_call(with_lines, uses)}) == PLACEMENT


@pytest.mark.parametrize('with_lines', [
    "      runs_on: '[\"ubuntu-latest\"]'\n",
    '      runs_on: ubuntu-latest\n',
    '      runner: ubuntu-24.04\n',
    '      name: self-hosted-docs\n',
    '      version: 3\n',
    '      description: run on self-hosted hardware\n',
])
def test_public_reusable_calls_accept_hosted_runner_inputs_and_unrelated_values(with_lines):
    assert rules({CI: reusable_call(with_lines)}) == []


CALLEE = ('on:\n  workflow_call:\n    inputs:\n      target:\n        type: string\n'
          '        default: ubuntu-latest\n      title:\n        type: string\n'
          'jobs:\n  run:\n    runs-on: ${{ fromJSON(inputs.target) }}\n    name: ${{ inputs.title }}\n    steps:\n      - run: echo ok\n')


@pytest.mark.parametrize('with_lines,expected', [
    ("      target: '[\"tower\"]'\n", PLACEMENT),
    ("      target: '[\"Windows\",\"X64\"]'\n", PLACEMENT),
    ("      target: '[\"ubuntu-latest\"]'\n", []),
    ('      title: tower\n', []),
])
def test_local_callee_inputs_that_reach_runs_on_are_runner_inputs(with_lines, expected):
    files = {'.github/workflows/callee.yml': CALLEE, CI: reusable_call(with_lines, './.github/workflows/callee.yml')}
    assert rules(files) == [('warning', 'dynamic-runner')] + expected


def test_reusable_call_exception_still_covers_the_hub_review_job():
    entry = CONFIG['review_exceptions'][1]
    content = (REAL / 'agent-harness-hub-review.yml').read_text(encoding='utf-8')
    assert rules({entry['workflow']: content}, repository=entry['repository']) == []
    assert rules({entry['workflow']: content}, repository='someone/else') == PLACEMENT


@pytest.mark.parametrize('filename,content', [
    ('ci-local.py', 'subprocess.run("pytest -n auto", shell=True)'),
    ('scripts/ci-local.mjs', 'execSync(`npx playwright test --workers=8`);'),
])
def test_script_command_literals(filename, content):
    assert lint.lint_files({filename: content}, 'a/b', 'public', CONFIG, TODAY)[0]['rule'] == 'parallelism-cap'


def workflow(env=(), job_env=(), step_env=(), run='python -m pytest -q', extra_steps=''):
    """A one-job workflow with optional workflow, job and step level env blocks."""
    lines = ['on: pull_request']
    if env:
        lines += ['env:'] + [f'  {item}' for item in env]
    lines += ['jobs:', '  test:', '    runs-on: ubuntu-latest']
    if job_env:
        lines += ['    env:'] + [f'      {item}' for item in job_env]
    lines.append('    steps:')
    if step_env:
        lines += ['      - env:'] + [f'          {item}' for item in step_env] + ['        run: ' + json.dumps(run)]
    else:
        lines.append('      - run: ' + json.dumps(run))
    return '\n'.join(lines) + '\n' + extra_steps


def policy_rules(files):
    findings = lint.lint_files(files, 'a/b', 'public', CONFIG, TODAY)
    return sorted({(item['severity'], item['rule']) for item in findings})


CAP = [('error', 'parallelism-cap')]
WORKFLOW = '.github/workflows/ci.yml'
INI = '[pytest]\naddopts = '


@pytest.mark.parametrize('name,files,expected', [
    ('workflow-env-auto', {WORKFLOW: workflow(env=['PYTEST_ADDOPTS: -n auto'])}, CAP),
    ('job-env-8', {WORKFLOW: workflow(job_env=['PYTEST_ADDOPTS: -n 8'])}, CAP),
    ('step-env-auto', {WORKFLOW: workflow(step_env=['PYTEST_ADDOPTS: "-n auto"'])}, CAP),
    ('step-env-long-flag', {WORKFLOW: workflow(step_env=['PYTEST_ADDOPTS: --numprocesses=8'])}, CAP),
    ('step-env-lowercase-name', {WORKFLOW: workflow(step_env=['pytest_addopts: -n8'])}, CAP),
    ('step-env-4-allowed', {WORKFLOW: workflow(step_env=['PYTEST_ADDOPTS: -n 4'])}, []),
    ('step-env-unrelated-options', {WORKFLOW: workflow(step_env=['PYTEST_ADDOPTS: -q --maxfail=1'])}, []),
    ('step-env-cleared', {WORKFLOW: workflow(env=['PYTEST_ADDOPTS: -n auto'], step_env=['PYTEST_ADDOPTS: ""'])}, []),
    ('command-line-override-allows', {WORKFLOW: workflow(env=['PYTEST_ADDOPTS: -n auto'], run='python -m pytest -n 2')}, []),
    ('command-line-override-cannot-raise', {WORKFLOW: workflow(env=['PYTEST_ADDOPTS: -n 2'], run='python -m pytest -n 8')}, CAP),
    ('step-env-dynamic', {WORKFLOW: workflow(step_env=['PYTEST_ADDOPTS: "${{ vars.PYTEST_OPTS }}"'])}, [('warning', 'dynamic-parallelism')]),
    ('step-env-on-action-step', {WORKFLOW: 'on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/actions/test\n        env:\n          PYTEST_ADDOPTS: -n auto\n'}, CAP),
    ('inline-assignment-in-run', {WORKFLOW: workflow(run="PYTEST_ADDOPTS='-n auto' python -m pytest")}, CAP),
    ('inline-assignment-4-allowed', {WORKFLOW: workflow(run="PYTEST_ADDOPTS='-n 4' python -m pytest")}, []),
    ('env-command', {WORKFLOW: workflow(run='env PYTEST_ADDOPTS="-n 12" pytest')}, CAP),
    ('export-then-pytest', {WORKFLOW: workflow(run='export PYTEST_ADDOPTS="-n auto"\npytest')}, CAP),
    ('export-then-other-runner', {WORKFLOW: workflow(run='export PYTEST_ADDOPTS="-n auto"\nmake test')}, CAP),
    ('github-env-carries-to-next-step', {WORKFLOW: workflow(run='echo "PYTEST_ADDOPTS=-n 16" >> "$GITHUB_ENV"', extra_steps='      - run: python -m pytest\n')}, CAP),
    ('unset-clears', {WORKFLOW: workflow(env=['PYTEST_ADDOPTS: -n auto'], run='unset PYTEST_ADDOPTS\npytest')}, []),
    ('powershell-env', {WORKFLOW: workflow(run='$env:PYTEST_ADDOPTS = "-n auto"\npytest')}, CAP),
    ('cmd-set', {WORKFLOW: workflow(run='set PYTEST_ADDOPTS=-n auto\npytest')}, CAP),
    ('cmd-set-crlf', {WORKFLOW: workflow(run='set PYTEST_ADDOPTS=-n 5\r\npytest')}, CAP),
    ('package-script', {'package.json': json.dumps({'scripts': {'test': "PYTEST_ADDOPTS='-n auto' pytest"}})}, CAP),
    ('package-script-cross-env', {'package.json': json.dumps({'scripts': {'test': 'cross-env PYTEST_ADDOPTS=-n8 pytest'}})}, CAP),
    ('ci-local-shell', {'ci-local.sh': "PYTEST_ADDOPTS='-n auto' pytest\n"}, CAP),
    ('ci-local-python-env-dict', {'ci-local.py': "subprocess.run(['pytest'], env={**os.environ, 'PYTEST_ADDOPTS': '-n auto'})"}, CAP),
    ('ci-local-python-environ', {'ci-local.py': "os.environ['PYTEST_ADDOPTS'] = '-n 8'"}, CAP),
    ('ci-local-js-env', {'ci-local.mjs': "execSync('pytest', {env: {...process.env, PYTEST_ADDOPTS: '-n auto'}});"}, CAP),
    ('ci-local-python-4-allowed', {'ci-local.py': "os.environ['PYTEST_ADDOPTS'] = '-n 4'"}, []),
    ('pytest-ini', {'pytest.ini': INI + '-q -n auto\n'}, CAP),
    ('pytest-ini-multiline', {'pytest.ini': INI + '\n    -q\n    --numprocesses=8\n'}, CAP),
    ('pytest-ini-4-allowed', {'pytest.ini': INI + '-n 4\n'}, []),
    ('tox-ini', {'tox.ini': INI + '-n logical\n[testenv]\ncommands = pytest\n'}, CAP),
    ('setup-cfg', {'setup.cfg': '[tool:pytest]\naddopts = -n 50%\n'}, CAP),
    ('pyproject-string', {'pyproject.toml': '[tool.pytest.ini_options]\naddopts = "-ra -n auto"\n'}, CAP),
    ('pyproject-list', {'pyproject.toml': '[tool.pytest.ini_options]\naddopts = ["-ra", "-n", "9"]\n'}, CAP),
    ('pyproject-unrelated-section', {'pyproject.toml': '[tool.other]\naddopts = "-n auto"\n'}, []),
    ('hidden-pytest-ini', {'.pytest.ini': INI + '-n auto\n'}, CAP),
], ids=lambda value: value if isinstance(value, str) else None)
def test_pytest_environment_and_config_caps(name, files, expected):
    assert policy_rules(files) == expected


def test_policy_files_include_pytest_config():
    for name in ['pytest.ini', '.pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'sub/pyproject.toml']:
        assert lint.policy_path(name), name


def test_git_tree_does_not_read_symlinks(tmp_path):
    # A tree object with a symlink needs no platform symlink privileges.
    lint.git(tmp_path, 'init', '--bare', '--template=')
    blob = subprocess.check_output(['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], input=b'/outside/package.json').strip()
    tree = subprocess.check_output(['git', '-C', str(tmp_path), 'mktree'], input=b'120000 blob ' + blob + b'\tpackage.json\n').strip().decode()
    with pytest.raises(ValueError, match='Unsupported file type'):
        lint.read_tree(tmp_path, tree)


def test_actionlint_only_receives_changed_workflows_and_trusted_config(monkeypatch):
    seen = []

    def execute(argv, **kwargs):
        seen.append(argv)
        assert Path(kwargs['cwd'], '.github/workflows/changed.yml').read_text() == 'changed'
        assert Path(kwargs['cwd'], '.git').is_dir()
        assert not list(Path(kwargs['cwd'], '.git').iterdir())
        assert Path(kwargs['cwd'], '.github/workflows/old.yml').read_text() == 'old'
        assert str(Path(kwargs['cwd'], '.github/workflows/old.yml')) not in argv
        assert not Path(kwargs['cwd'], '.github/actionlint.yaml').exists()
        return subprocess.CompletedProcess(argv, 1, stdout='schema error', stderr='')

    monkeypatch.setattr(lint.subprocess, 'run', execute)
    files = {'.github/workflows/changed.yml': 'changed', '.github/workflows/old.yml': 'old', '.github/actionlint.yaml': 'untrusted'}
    result = lint.run_actionlint(files, ['.github/workflows/changed.yml', '.github/workflows/deleted.yml'], 'actionlint')
    assert result['files'] == ['.github/workflows/changed.yml']
    assert result['exit_code'] == 1
    assert result['output'] == 'schema error'
    assert '-shellcheck=' in seen[0] and '-pyflakes=' in seen[0]
    assert seen[0][seen[0].index('-config-file') + 1] == str(ROOT / 'config/actionlint.yaml')


def test_no_changed_workflows_does_not_invoke_actionlint(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('actionlint must not run without explicit files')
    monkeypatch.setattr(lint.subprocess, 'run', forbidden)
    assert lint.run_actionlint({}, [], 'actionlint')['exit_code'] == 0


def test_remote_reads_visibility_and_merge_base_from_api(monkeypatch):
    head, base, merge = 'a' * 40, 'b' * 40, 'c' * 40
    calls, git_calls = [], []

    def api(endpoint):
        calls.append(endpoint)
        return {'visibility': 'private'} if endpoint == 'repos/a/b' else {'merge_base_commit': {'sha': merge}}

    def git(directory, *args):
        git_calls.append(args)
        return b'ci-local.sh\0' if args[0] == 'diff' else b''

    monkeypatch.setattr(lint, 'api', api)
    monkeypatch.setattr(lint, 'git', git)
    monkeypatch.setattr(lint, 'read_tree', lambda *args: {'ci-local.sh': 'pytest -n 8'})
    report = lint.inspect_remote('a/b', 'actionlint', base, head)
    assert calls == ['repos/a/b', f'repos/a/b/compare/{base}...{head}']
    assert ('fetch', '--no-tags', '--depth=1', 'https://github.com/a/b.git', head, merge) in git_calls
    assert report['visibility'] == 'private'
    assert lint.failed(report)


def test_new_workflow_security_contract():
    document = yaml.safe_load((ROOT / '.github/workflows/workflow-lint.yml').read_text())
    assert document['permissions'] == {'contents': 'read'}
    job = document['jobs']['workflow-lint']
    assert job['name'] == 'workflow-lint'
    for step in job['steps']:
        if 'uses' in step:
            assert lint.SHA.fullmatch(step['uses'].split('@')[1])
    text = (ROOT / '.github/workflows/workflow-lint.yml').read_text()
    assert '914e7df21a07ef503a81201c76d2b11c789d3fca' in text
    assert 'secrets.' not in text
    assert 'persist-credentials: false' in text


def test_caller_uses_read_only_pull_request_without_secrets():
    text = (ROOT / 'templates/workflow-lint-caller.yml').read_text()
    document = yaml.safe_load(text)
    assert document['permissions'] == {'contents': 'read'}
    assert document[True] == {'pull_request': None}  # PyYAML's YAML 1.1 'on'.
    assert document['jobs']['workflow-lint']['with']['lint-ref'] == '<full-sha>'
    assert 'secrets' not in document['jobs']['workflow-lint']
