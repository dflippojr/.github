#!/usr/bin/env python3
"""Read workflow and test commands as data; never execute target repository code."""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
HOSTED = re.compile(r"\b(?:ubuntu|windows|macos)-(?:latest|\d[^\s'\"\],}]*)", re.I)
CI_SWITCH = re.compile(r"^\s*\$\{\{\s*fromJSON\(\s*vars\.CI_RUNS_ON\s*\|\|\s*'[^']+'\s*\)\s*}}\s*$", re.I)


def workflow_path(name):
    p = PurePosixPath(name)
    return p.parent == PurePosixPath('.github/workflows') and p.suffix in {'.yml', '.yaml'}


def policy_path(name):
    return workflow_path(name) or PurePosixPath(name).name.startswith('ci-local') or PurePosixPath(name).name == 'package.json'


def strings(value):
    """Flatten YAML data, including matrix objects, without looping on aliases."""
    pending, seen = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)


def exception_allowed(config, repository, filename, job_id, job, today):
    guard = ' '.join(str(job.get('if', '')).split())
    for entry in config.get('review_exceptions', []):
        if (entry['repository'], entry['workflow'], entry['job']) != (repository, filename, job_id):
            continue
        if today < datetime.date.fromisoformat(entry['expires']) and guard == entry['fork_guard']:
            return True
    return False


def runner_values(runner, job):
    values = list(strings(runner))
    for value in list(values):
        matrix = job.get('strategy', {}).get('matrix', {})
        if isinstance(matrix, dict):
            for key in re.findall(r'\bmatrix\.([\w-]+)', value):
                values.extend(strings(matrix.get(key, [])))
                for row in matrix.get('include', []):
                    if isinstance(row, dict):
                        values.extend(strings(row.get(key, [])))
        elif 'matrix.' in value:
            values.extend(strings(matrix))
    # JSON label arrays also occur in reusable inputs and fromJSON expressions.
    # Decode literals so JSON Unicode escapes cannot hide a self-hosted label.
    for value in list(values):
        for candidate in [value] + re.findall(r"'([^']*)'", value):
            try:
                values.extend(strings(json.loads(candidate)))
            except (ValueError, TypeError):
                pass
    return values


def finding(severity, rule, filename, detail):
    return dict(severity=severity, rule=rule, path=filename, detail=detail)


def runner_findings(filename, document, repository, visibility, config, today):
    results = []
    jobs = document.get('jobs', {})
    if not isinstance(jobs, dict):
        return results  # actionlint supplies schema diagnostics.
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        runner = job.get('runs-on')
        # Reusable review callers supply their runner as JSON inputs.
        if runner is None and isinstance(job.get('with'), dict):
            runner = job['with'].get('runs_on', job['with'].get('runs-on'))
        if runner is None:
            continue
        values = runner_values(runner, job)
        text = ' '.join(values)
        self_hosted = re.search(r'(?<![\w-])self-hosted(?![\w-])', text, re.I)
        if visibility == 'public' and self_hosted:
            if not exception_allowed(config, repository, filename, job_id, job, today):
                results.append(finding('error', 'public-self-hosted', filename, f'{job_id}: self-hosted is forbidden on public repositories'))
        elif visibility == 'private' and HOSTED.search(text) and not (isinstance(runner, str) and CI_SWITCH.fullmatch(runner)):
            results.append(finding('warning', 'private-hosted', filename, f'{job_id}: GitHub-hosted runner without the CI_RUNS_ON switch'))
        if '${{' in text and not self_hosted and not HOSTED.search(text):
            results.append(finding('warning', 'dynamic-runner', filename, f'{job_id}: runner expression cannot be resolved statically'))
    return results


def parallelism_findings(filename, command):
    """Recognize literal CLI caps, scoped to pytest or Playwright invocations."""
    results = []
    # Shell, cmd and PowerShell line continuations.
    command = re.sub(r'(?:\\|`|\^)\r?\n', ' ', command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [finding('warning', 'unparsed-command', filename, 'Could not tokenize command; inspect manually')]
    tool = None
    for index, token in enumerate(tokens):
        if token and all(c in ';&|\n' for c in token):
            tool = None
            continue
        if token in {'pytest', 'pytest.exe', 'py.test'} or token.endswith('/pytest'):
            tool = 'pytest'
        elif token in {'playwright', 'playwright.exe'} or token.endswith('/playwright'):
            tool = 'playwright'
        if tool is None:
            continue
        value = None
        if tool == 'pytest':
            match = re.fullmatch(r'(?:-n|--numprocesses)(?:=?(.+))?', token)
        else:
            match = re.fullmatch(r'--workers(?:=(.+))?', token)
        if match:
            value = match.group(1)
            if value is None and index + 1 < len(tokens):
                value = tokens[index + 1]
        if value is None:
            continue
        invalid = value.lower() in {'auto', 'logical'} or (value.isdigit() and int(value) > 4) or bool(re.fullmatch(r'\d+%', value))
        if invalid:
            results.append(finding('error', 'parallelism-cap', filename, f'{tool} {token} {value}: maximum is 4 workers (use a fixed count)'))
        elif not value.isdigit():
            results.append(finding('warning', 'dynamic-parallelism', filename, f'{tool}: cannot verify worker count {value!r}'))
    return results


def script_findings(filename, content):
    # Shell and PowerShell files are command text. Python/JS launchers often
    # embed their command as a string; inspect those literals separately.
    if PurePosixPath(filename).suffix in {'.py', '.js', '.mjs', '.cjs', '.ts'}:
        results = []
        for match in re.finditer(r'''(["'`])((?:\\.|(?!\1).)*?)\1''', content, re.S):
            literal = match.group(2)
            if re.search(r'\b(?:pytest|playwright)\b', literal):
                results.extend(parallelism_findings(filename, literal))
        return results
    return parallelism_findings(filename, content)


def lint_files(files, repository, visibility, config, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    results = []
    for filename, content in sorted(files.items()):
        try:
            if workflow_path(filename):
                document = yaml.safe_load(content)
                if not isinstance(document, dict):
                    raise ValueError('Workflow must be a mapping')
                results.extend(runner_findings(filename, document, repository, visibility, config, today))
                for job in (document.get('jobs') or {}).values():
                    if not isinstance(job, dict):
                        continue
                    for step in job.get('steps', []):
                        if isinstance(step, dict) and isinstance(step.get('run'), str):
                            results.extend(parallelism_findings(filename, step['run']))
            elif PurePosixPath(filename).name == 'package.json':
                for script in json.loads(content).get('scripts', {}).values():
                    if isinstance(script, str):
                        results.extend(parallelism_findings(filename, script))
            elif PurePosixPath(filename).name.startswith('ci-local'):
                results.extend(script_findings(filename, content))
        except (yaml.YAMLError, ValueError, TypeError, AttributeError) as error:
            results.append(finding('error', 'invalid-file', filename, str(error)))
    return results


def api(endpoint):
    request = urllib.request.Request('https://api.github.com/' + endpoint, headers={
        'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def git(directory, *args):
    env = os.environ.copy()
    if env.get('GITHUB_TOKEN'):
        credential = base64.b64encode(('x-access-token:' + env['GITHUB_TOKEN']).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + credential)
    env['GIT_TERMINAL_PROMPT'] = '0'
    return subprocess.check_output(['git', '-C', str(directory), *args], env=env, stderr=subprocess.PIPE)


def read_tree(directory, head):
    files = {}
    for record in git(directory, 'ls-tree', '-rz', '--full-tree', head).split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        name = raw_name.decode('utf-8')
        if policy_path(name):
            mode, kind, blob = metadata.split()
            if kind != b'blob' or mode not in {b'100644', b'100755'}:
                raise ValueError(f'Unsupported file type for {name}')
            files[name] = git(directory, 'cat-file', 'blob', blob.decode()).decode('utf-8')
    return files


def run_actionlint(files, changed, executable):
    selected = sorted(name for name in changed if workflow_path(name) and name in files)
    if not selected:
        return {'files': [], 'exit_code': 0, 'output': 'No changed workflows; actionlint not needed.'}
    # A fresh directory excludes PR-controlled actionlint config, actions, executables,
    # symlinks and shell startup files. Disable external command execution by actionlint.
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for name in selected:
            target = Path(directory, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[name], encoding='utf-8')
            paths.append(str(target))
        result = subprocess.run([executable, '-shellcheck=', '-pyflakes=', '-config-file', str(ROOT / 'config/actionlint.yaml'), *paths], cwd=directory, capture_output=True, text=True, check=False)
        return {'files': selected, 'exit_code': result.returncode, 'output': (result.stdout + result.stderr).replace(directory, '<target>')}


def inspect_remote(repository, actionlint, base=None, head=None):
    if not REPOSITORY.fullmatch(repository):
        raise ValueError('Invalid repository name')
    metadata = api('repos/' + repository)
    visibility = metadata['visibility']
    if visibility not in {'public', 'private'}:
        raise ValueError('Unsupported repository visibility: ' + visibility)
    if head is None:
        head = api(f'repos/{repository}/commits/{metadata["default_branch"]}')['sha']
    for revision in (base, head):
        if revision is not None and not SHA.fullmatch(revision):
            raise ValueError('Revisions must be full commit SHAs')
    if base:
        base = api(f'repos/{repository}/compare/{base}...{head}')['merge_base_commit']['sha']
        if not SHA.fullmatch(base):
            raise ValueError('Invalid merge-base SHA')
    with tempfile.TemporaryDirectory() as directory:
        git(directory, 'init', '--bare', '--template=')
        git(directory, 'fetch', '--no-tags', '--depth=1', 'https://github.com/' + repository + '.git', *dict.fromkeys([head] + ([base] if base else [])))
        files = read_tree(directory, head)
        changed = set(files) if base is None else set(git(directory, 'diff', '--name-only', '-z', '--diff-filter=ACMRT', base, head).decode().split('\0'))
    config = json.loads((ROOT / 'config/workflow-lint.json').read_text(encoding='utf-8'))
    return {'repository': repository, 'visibility': visibility, 'head': head, 'policy_files': len(files),
            'findings': lint_files(files, repository, visibility, config),
            'actionlint': run_actionlint(files, changed, actionlint)}


def failed(report):
    return report['actionlint']['exit_code'] != 0 or any(f['severity'] == 'error' for f in report['findings'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--event-file', type=Path)
    parser.add_argument('--actionlint', default='actionlint')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    base = head = None
    if args.event_file:
        event = json.loads(args.event_file.read_text(encoding='utf-8'))
        pr = event['pull_request']
        if pr['base']['repo']['full_name'] != args.repository:
            raise ValueError('PR base does not match repository')
        base, head = pr['base']['sha'], pr['head']['sha']
    report = inspect_remote(args.repository, args.actionlint, base, head)
    output = json.dumps(report, indent=2, ensure_ascii=True)
    print(output)
    if args.output:
        args.output.write_text(output + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            # Escape HTML and backticks from untrusted file contents in the report.
            safe = output.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', '&#96;')
            summary.write('## workflow-lint\n\n<pre>' + safe + '</pre>\n')
    return int(failed(report))


if __name__ == '__main__':
    raise SystemExit(main())
