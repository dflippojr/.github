#!/usr/bin/env python3
"""Read workflow and test commands as data; never execute target repository code."""
import argparse
import base64
import configparser
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
# Labels GitHub assigns to its own hosted runners. Larger-runner names are chosen
# by the organization, so config/workflow-lint.json lists those as hosted_runner_labels.
HOSTED_LABEL = re.compile(r'(?:ubuntu|windows|macos)-(?:latest|slim|\d[\d.]*)(?:-(?:arm|arm64|intel|large|xlarge))?', re.I)
RUNNER_INPUT = re.compile(r'runs?[-_]?on|runners?|runner[-_]?labels?', re.I)
# Actions that prepare or move files and never run a command from their inputs.
# Matching on a name pattern such as setup-* would exempt coactions/setup-xvfb, which runs one.
NEUTRAL_ACTION = re.compile(r'actions/(?:checkout|cache(?:/(?:restore|save))?|upload-artifact|download-artifact|setup-(?:python|node|java|go|dotnet))'
                            r'|astral-sh/setup-uv|ruby/setup-ruby|pnpm/action-setup|docker/setup-(?:buildx|qemu)-action', re.I)
TEST_TOOL = re.compile(r'(?<![\w-])(?:pytest|py\.test|playwright)(?![\w-])', re.I)
PYTEST_CONFIG = {'pytest.ini', '.pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'pytest.toml', '.pytest.toml'}
ADDOPTS_TOKEN = re.compile(r'PYTEST_ADDOPTS=(.*)\Z', re.I | re.S)
ADDOPTS_LITERAL = re.compile(r"""PYTEST_ADDOPTS["'`\]]*\s*[:=,)]\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.I | re.S)
ASSIGNMENT = re.compile(r'[A-Za-z_]\w*=')
# Characters a shell may still expand or unescape in a value the scan has already unquoted:
# bash $'...', $"...", $- and other parameters and backquotes; cmd %VAR%, !VAR! and ^.
SHELL_EXPANSION = re.compile(r'[$`%!^]')
# The environment file as one raw word, bare or double-quoted: bash's $GITHUB_ENV or
# ${GITHUB_ENV}, PowerShell's $env:GITHUB_ENV and cmd's %GITHUB_ENV%.
# A single-quoted '$GITHUB_ENV' is a literal path and $github_env another bash variable.
ANY_ENV = re.compile(r'(")?(?:\$GITHUB_ENV|\$\{GITHUB_ENV\}|(?i:\$env:GITHUB_ENV|%GITHUB_ENV%))(?(1)")')
# Shell text split into raw words that keep their quotes, so a quoted or escaped '>>' stays an argument.
REDIRECT = r'\d*(?:>>|>&|>\||<<<|<<-|<<|<&|<>|>|<)|&>>|&>'
SHELL_SYNTAX = re.compile(r'''[ \t\r]+|(?P<comment>\#[^\n]*)|(?P<redirect>''' + REDIRECT + r''')|(?P<op>[;&|\n]+)'''
                          r'''|(?P<word>(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|<>'"\\])+)''', re.S)
HEREDOC = re.compile(r'''(?<!<)<<(-?)[ \t]*(['"]?)([\w.-]+)\2''')
FILE_WRITERS = {'tee', 'add-content', 'set-content', 'out-file'}
EXPORTERS = {'export', 'declare', 'typeset', 'readonly', 'local'}
# Words that precede the command in a shell segment without being the command.
SHELL_PREFIX = EXPORTERS | {'env', 'cross-env', 'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', '(', 'time'}
# Commands that cannot start pytest, so an inherited PYTEST_ADDOPTS does not matter to them.
NEUTRAL = {'echo', 'printf', 'cd', 'pushd', 'popd', 'mkdir', 'ls', 'dir', 'cat', 'pwd', 'true', 'false', ':', 'test', '[', '[[', 'sleep',
           'rm', 'cp', 'mv', 'touch', 'exit', 'return', 'unset', 'set', 'fi', 'done', 'esac', '}', ')', 'for', 'case', 'git', 'gh', 'curl',
           'wget', 'pip', 'pip3', 'apt', 'apt-get', 'brew', 'chmod', 'tar', 'unzip', 'write-host', 'write-output'} | FILE_WRITERS
CI_SWITCH = re.compile(r"^\s*\$\{\{\s*fromJSON\(\s*vars\.CI_RUNS_ON\s*\|\|\s*'[^']+'\s*\)\s*}}\s*$", re.I)


def workflow_path(name):
    p = PurePosixPath(name)
    return p.parent == PurePosixPath('.github/workflows') and p.suffix in {'.yml', '.yaml'}


def policy_path(name):
    base = PurePosixPath(name).name
    return workflow_path(name) or base.startswith('ci-local') or base == 'package.json' or base in PYTEST_CONFIG


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


def trigger_events(document):
    """The event names a workflow declares. PyYAML's YAML 1.1 loader reads the unquoted key `on` as True."""
    trigger = document.get('on', document.get(True))
    if isinstance(trigger, str):
        return {trigger}
    if isinstance(trigger, (list, dict)):
        return set(trigger)
    return set()


def active_exceptions(config, repository, today):
    return [entry for entry in config.get('review_exceptions', [])
            if entry['repository'] == repository and today < datetime.date.fromisoformat(entry['expires'])]


def exception_allowed(config, repository, filename, job_id, job, today, document):
    events = trigger_events(document)
    guard = ' '.join(str(job.get('if', '')).split())
    return any((entry['workflow'], entry['job']) == (filename, job_id)
               and events and events <= set(entry['allowed_events']) and guard == entry['fork_guard']
               for entry in active_exceptions(config, repository, today))


def local_workflow(uses, repository):
    """The workflow path a `uses:` reference calls inside this repository, or None."""
    if uses.startswith('./'):
        return PurePosixPath(uses[2:]).as_posix()
    match = re.fullmatch(r'([^/@\s]+/[^/@\s]+)/(\.github/workflows/[^@\s]+)@\S+', uses)
    return match.group(2) if match and match.group(1).lower() == repository.lower() else None


def exception_caller_findings(documents, repository, config, today):
    """Callers of an excepted workflow run it on its self-hosted runner, so their triggers must be safe too.

    A caller that leaves out the runner input has no runner label for the runner
    rule to read, and the callee's fork guard cannot see events it was not written for.
    """
    allowed = {entry['workflow']: set(entry['allowed_events']) for entry in active_exceptions(config, repository, today)}
    results, changed = [], bool(allowed)
    while changed:
        changed = False
        for filename, document in sorted(documents.items()):
            events = trigger_events(document)
            for job_id, job in (document.get('jobs') or {}).items():
                target = local_workflow(job['uses'], repository) if isinstance(job, dict) and isinstance(job.get('uses'), str) else None
                if target not in allowed:
                    continue
                if not events or not events <= allowed[target]:
                    results.append(finding('error', 'review-exception-caller', filename, f'{job_id}: calls {target}, which may run on a self-hosted runner, from events outside {sorted(allowed[target])}'))
                elif 'workflow_call' in events and filename not in allowed:
                    allowed[filename], changed = allowed[target], True
    return results


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


def static_labels(values):
    """Runner labels written out in the workflow: plain strings, JSON arrays and quoted literals inside expressions."""
    labels = set()
    for value in values:
        parts = re.findall(r"'((?:[^']|'')*)'", value) if '${{' in value else [value]
        for part in parts:
            part = part.replace("''", "'")
            try:
                decoded = json.loads(part)
            except ValueError:
                decoded = part
            labels.update(label.strip() for label in strings(decoded) if label.strip())
    return labels


def hosted_label(label, config):
    return bool(HOSTED_LABEL.fullmatch(label)) or label.lower() in {name.lower() for name in config.get('hosted_runner_labels', [])}


def callee_runner_inputs(documents):
    """For each workflow, the workflow_call input names its jobs use in runs-on."""
    names = {}
    for filename, document in documents.items():
        jobs = document.get('jobs') if isinstance(document, dict) else None
        used = set()
        for job in (jobs.values() if isinstance(jobs, dict) else []):
            if isinstance(job, dict):
                used.update(name for value in strings(job.get('runs-on')) for name in re.findall(r'\binputs\.([\w-]+)', value))
        names[filename] = used
    return names


def reusable_runner_inputs(job, repository, callee_inputs):
    """Values a reusable-workflow call passes that can select its runner."""
    values = job.get('with')
    if not isinstance(job.get('uses'), str) or not isinstance(values, dict):
        return []
    used = (callee_inputs or {}).get(local_workflow(job['uses'], repository), set())
    return [value for key, value in values.items()
            if RUNNER_INPUT.fullmatch(str(key)) or str(key) in used
            or any(label.lower() == 'self-hosted' for label in static_labels(strings(value)))]


def runner_findings(filename, document, repository, visibility, config, today, callee_inputs=None):
    results = []
    jobs = document.get('jobs', {})
    if not isinstance(jobs, dict):
        return results  # actionlint supplies schema diagnostics.
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        # Reusable review callers supply their runner as inputs.
        subjects = [item for item in [job.get('runs-on')] + reusable_runner_inputs(job, repository, callee_inputs) if item is not None]
        if not subjects:
            continue
        values = [value for subject in subjects for value in runner_values(subject, job)]
        text = ' '.join(values)
        self_hosted = re.search(r'(?<![\w-])self-hosted(?![\w-])', text, re.I)
        placed = sorted(label for label in static_labels(values) if not hosted_label(label, config))
        if visibility == 'public' and (self_hosted or placed):
            if not exception_allowed(config, repository, filename, job_id, job, today, document):
                labels = ', '.join(placed or ['self-hosted'])
                results.append(finding('error', 'public-self-hosted', filename, f'{job_id}: self-hosted is forbidden on public repositories (labels not hosted by GitHub: {labels})'))
        elif visibility == 'private' and HOSTED.search(text) and not (len(subjects) == 1 and isinstance(subjects[0], str) and CI_SWITCH.fullmatch(subjects[0])):
            results.append(finding('warning', 'private-hosted', filename, f'{job_id}: GitHub-hosted runner without the CI_RUNS_ON switch'))
        if '${{' in text and not self_hosted and not placed and not HOSTED.search(text):
            results.append(finding('warning', 'dynamic-runner', filename, f'{job_id}: runner expression cannot be resolved statically'))
    return results


def is_pytest(token):
    return token in {'pytest', 'pytest.exe', 'py.test'} or token.endswith('/pytest')


def is_playwright(token):
    return token in {'playwright', 'playwright.exe'} or token.endswith('/playwright')


def worker_flags(tool, tokens):
    """Yield (flag, value) for each literal worker-count option in an argument list."""
    for index, token in enumerate(tokens):
        if tool == 'pytest':
            match = re.fullmatch(r'(?:-n|--numprocesses)(?:=?(.+))?', token)
        else:
            match = re.fullmatch(r'(?:--workers|-j)(?:=?(.+))?', token)
        if match:
            value = match.group(1)
            if value is None and index + 1 < len(tokens):
                value = tokens[index + 1]
            if value is not None:
                yield token, value


def worker_finding(filename, label, token, value):
    invalid = value.lower() in {'auto', 'logical'} or (value.isdigit() and int(value) > 4) or bool(re.fullmatch(r'\d+%', value))
    if invalid:
        return finding('error', 'parallelism-cap', filename, f'{label} {token} {value}: maximum is 4 workers (use a fixed count)')
    if not value.isdigit():
        return finding('warning', 'dynamic-parallelism', filename, f'{label}: cannot verify worker count {value!r}')
    return None


def unreadable_addopts(filename, value):
    return finding('error', 'parallelism-cap', filename, f'PYTEST_ADDOPTS {value!r}: cannot read the options exactly (use a literal value)')


def addopts_findings(filename, value):
    """Judge a PYTEST_ADDOPTS or addopts value, which pytest puts before the command line.
    A value the scan cannot read exactly fails: its worker count is unknown."""
    try:
        tokens = shlex.split(value)
    except ValueError:
        return [unreadable_addopts(filename, value)]
    if re.search(r'\$[\w{(]', value):
        return [unreadable_addopts(filename, value)]
    return [finding('error', 'parallelism-cap', filename, f'PYTEST_ADDOPTS {token} {count}: maximum is 4 workers (use a fixed count)')
            for token, count in worker_flags('pytest', tokens) if not (count.isdigit() and int(count) <= 4)]


def shell_addopts_findings(filename, value):
    """Judge a PYTEST_ADDOPTS value that a shell assigns or writes, after the scan removed its quotes.
    A character the shell may still expand or unescape means the scan did not read what pytest gets."""
    return [unreadable_addopts(filename, value)] if SHELL_EXPANSION.search(value) else addopts_findings(filename, value)


def posix_env(command):
    """Rewrite cmd.exe and PowerShell PYTEST_ADDOPTS assignments as POSIX NAME='value'.
    A PowerShell right-hand side other than one quoted string is an expression, kept as $(...) so it fails as unreadable."""
    def assignment(value):
        return "PYTEST_ADDOPTS='" + value.strip().replace("'", "'\\''") + "'"
    command = re.sub(r'(?im)^[ \t]*set[ \t]+"?PYTEST_ADDOPTS=([^\r\n"]*)"?[ \t]*\r?$', lambda m: assignment(m.group(1)), command)
    return re.sub(r'''(?i)\$env:PYTEST_ADDOPTS[ \t]*=[ \t]*(?:(?:"([^"\r\n]*)"|'([^'\r\n]*)')(?=[ \t]*(?:[;#\r\n]|\Z))|([^\r\n;]*))''',
                  lambda m: assignment(m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None
                                       else '$(' + m.group(3) + ')'), command)


def command_word(segment):
    """Split a simple command into (inline PYTEST_ADDOPTS or None, whether assignments persist, command words)."""
    inline, exporter, index = None, False, 0
    while index < len(segment):
        token = segment[index]
        assign = ADDOPTS_TOKEN.fullmatch(token)
        if token in SHELL_PREFIX:
            exporter = exporter or token in EXPORTERS
        elif token.startswith('-') and index and segment[index - 1] in {'env', 'cross-env'}:
            pass  # env -i and similar options
        elif assign:
            inline = assign.group(1)
        elif not ASSIGNMENT.match(token):
            break
        index += 1
    words = [] if exporter else segment[index:]  # what follows `export` is a list of names
    return inline, exporter or not words, words


def neutral_command(words):
    """True for commands that cannot start pytest, so an inherited PYTEST_ADDOPTS does not matter to them."""
    name = PurePosixPath(words[0].replace('\\', '/')).name.lower()
    name = name[:-4] if name.endswith('.exe') else name
    return name in NEUTRAL or (name.startswith('python') or name == 'py') and words[1:3] == ['-m', 'pip']


def split_heredocs(command):
    """Cut heredoc bodies out of command text, leaving a marker word (NUL and an index) after
    each `<<WORD`, so a body line such as `PYTEST_ADDOPTS=` is never read as an assignment."""
    lines, kept, bodies, index = command.split('\n'), [], [], 0
    while index < len(lines):
        line, start, index = lines[index], 0, index + 1
        while match := HEREDOC.search(line, start):
            strip, delimiter = match.group(1), match.group(3)
            end = next((number for number in range(index, len(lines))
                        if (lines[number].lstrip('\t') if strip else lines[number]).rstrip('\r') == delimiter), None)
            if end is None:
                break  # no closing line, so this is not a heredoc (for example `echo "1 << 2"`)
            marker = f' \0{len(bodies)} '
            bodies.append('\n'.join(lines[index:end]))
            line, start, index = line[:match.end()] + marker + line[match.end():], match.end() + len(marker), end + 1
        kept.append(line)
    return '\n'.join(kept), bodies


def shell_pipelines(command):
    """Split command text into pipelines of simple commands made of raw words with their quotes.
    Each pipeline is [words of each command]. None when a quote never closes."""
    pipelines, words, commands, position = [], [], [], 0
    while position < len(command):
        match = SHELL_SYNTAX.match(command, position)
        if not match:
            return None
        position = match.end()
        if match.lastgroup == 'op':
            commands.append(words)
            words = []
            if match.group() not in {'|', '|&'}:
                pipelines.append(commands)
                commands = []
        elif match.lastgroup in {'redirect', 'word'}:
            words.append(match.group())
    pipelines.append(commands + [words])
    return pipelines


def touches_env_file(commands):
    """True when a pipeline sends anything to the environment file in any spelling: a redirect of
    any stream, or tee or a PowerShell file cmdlet naming it."""
    for words in commands:
        if any(re.fullmatch(REDIRECT, word) and '>' in word and ANY_ENV.fullmatch(target)
               for word, target in zip(words, words[1:])):
            return True
        if words and words[0].lower() in FILE_WRITERS and any(ANY_ENV.fullmatch(word) for word in words[1:]):
            return True
    return False


def env_file_findings(filename, command, bodies):
    """Judge the PYTEST_ADDOPTS values a command may write to $GITHUB_ENV.

    Such a write never changes what later steps inherit: whether it reaches the file as its own
    line depends on the shell, the stream, line breaks and later overwrites, none of which the
    YAML shows. Only a step-level env: block changes the value. Any value the write may carry is
    still checked here, and one with a backslash (printf and echo -e escapes) or a character the
    shell may expand (printf formats, variables, shell_addopts_findings) fails, because what
    reaches the file differs from the text."""
    pipelines = shell_pipelines(command)
    if pipelines is None:
        return []  # tokenizing failed; scan_command already warns
    results = []
    for commands in pipelines:
        if not touches_env_file(commands):
            continue
        words = [word for command_words in commands for word in command_words]
        text = '\n'.join([bodies[int(word[1:])] for word in words if word.startswith('\0')] + [
            ' '.join(plain(word) for word in words if not word.startswith('\0') and not ANY_ENV.fullmatch(word)
                     and not re.fullmatch(REDIRECT, word))])
        for match in re.finditer(r'PYTEST_ADDOPTS=([^\n]*)', text, re.I):
            value = match.group(1)
            results.extend([unreadable_addopts(filename, value)] if '\\' in value else shell_addopts_findings(filename, value))
    return results


def plain(word):
    """A raw word without its quotes, for judging the text it may carry."""
    try:
        return ' '.join(shlex.split(word))
    except ValueError:
        return word


def scan_command(filename, command, inherited=()):
    """Check worker caps in one command string, judging each command on its own effective parallelism.

    inherited holds the PYTEST_ADDOPTS values that may already be in effect. pytest
    reads the value before the command line, so a worker flag on pytest's own
    command line replaces it, but only for that command: a later command that may
    run pytest (make, tox, a script) still sees the value. Nothing in the script
    clears or weakens a value, since an `unset` or assignment may sit in a branch
    that never runs: each later command is judged on every value inherited or
    assigned so far, and an inline assignment adds its value for its own command.
    Text that only mentions the name, such as `echo PYTEST_ADDOPTS=`, assigns
    nothing. A value written to $GITHUB_ENV is checked but changes nothing here
    or in later steps (env_file_findings).
    Returns (findings, whether every inherited value was judged against a command).
    """
    inherited = [value for value in inherited if value]
    # Shell, cmd and PowerShell line continuations.
    command, bodies = split_heredocs(posix_env(re.sub(r'(?:\\|`|\^)\r?\n', ' ', command)))
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        results = [finding('warning', 'unparsed-command', filename, 'Could not tokenize command; inspect manually')]
        if re.search('PYTEST_ADDOPTS', command, re.I):
            results.append(unreadable_addopts(filename, command))
        return results + [item for value in inherited for item in addopts_findings(filename, value)], True
    segments, current, piped = [], [], []
    for token in tokens:
        if token and all(c in ';&|\n' for c in token):
            segments.append(current)
            piped.append(token == '|')
            current = []
        else:
            current.append(token)
    segments.append(current)
    piped.append(False)
    results = env_file_findings(filename, command, bodies)
    values, seen = list(inherited), set()  # values never leave: a clear or unset is not credited
    for number, segment in enumerate(segments):
        heredocs = [bodies[int(token[1:])] for token in segment if token.startswith('\0')]
        segment = [token for token in segment if not token.startswith('\0')]
        inline, persist, words = command_word(segment)
        if inline and SHELL_EXPANSION.search(inline):
            # The shell may still change it, so no command line can be said to override it.
            results.append(unreadable_addopts(filename, inline))
        effective = values + [inline] if inline and not persist else values
        tools = [(index, 'pytest' if is_pytest(token) else 'playwright') for index, token in enumerate(words) if is_pytest(token) or is_playwright(token)]
        for position, (index, tool) in enumerate(tools):
            end = tools[position + 1][0] if position + 1 < len(tools) else len(words)
            flags = list(worker_flags(tool, words[index + 1:end]))
            results.extend(item for item in (worker_finding(filename, tool, token, value) for token, value in flags) if item)
            if tool == 'pytest':
                seen.update(effective)
                if not flags:
                    results.extend(item for value in effective for item in addopts_findings(filename, value))
        if not tools and words and effective and not neutral_command(words):
            seen.update(effective)
            results.extend(item for value in effective for item in addopts_findings(filename, value))
        # A heredoc body is input for its command, which runs it (if at all) in a child process.
        for body in heredocs:
            results.extend(scan_command(filename, body, effective)[0])
        # A value assigned by `export` or a bare assignment reaches the commands after this one.
        # A bare string piped into another command (PowerShell's "PYTEST_ADDOPTS=" | Out-File) is text.
        if inline and persist and not piped[number]:
            values.append(inline)
    # A value assigned here that no command has seen may still reach a child process or later step.
    results.extend(item for value in values[len(inherited):] if value not in seen for item in addopts_findings(filename, value))
    return results, all(value in seen for value in inherited)


def parallelism_findings(filename, command):
    """Recognize literal caps for pytest and Playwright, including PYTEST_ADDOPTS assignments."""
    return scan_command(filename, command)[0]


def env_addopts(env):
    """The literal PYTEST_ADDOPTS in an env mapping, or None. Windows variable names ignore case."""
    if isinstance(env, dict):
        for key, value in env.items():
            if str(key).upper() == 'PYTEST_ADDOPTS' and value is not None:
                return str(value)
    return None


def workflow_parallelism(filename, document):
    results = []
    workflow_env = env_addopts(document.get('env'))
    for job in (document.get('jobs') or {}).values():
        if not isinstance(job, dict):
            continue
        job_env = env_addopts(job.get('env'))
        inherited = workflow_env if job_env is None else job_env
        steps = [step for step in job.get('steps') or [] if isinstance(step, dict)]
        judged = not inherited
        for step in steps:
            own = env_addopts(step.get('env'))
            # Step env beats job and workflow env. A GITHUB_ENV write never replaces either:
            # only what the YAML itself shows is credited.
            effective = inherited if own is None else own
            inherits = own is None
            uses = step.get('uses')
            opaque = isinstance(uses, str) and not NEUTRAL_ACTION.fullmatch(uses.split('@')[0])
            # A command handed to an action through with: (retry, xvfb, ...) runs like a run step.
            commands = [value for value in strings(step.get('with')) if TEST_TOOL.search(value)] if opaque else []
            if isinstance(step.get('run'), str):
                found, resolved = scan_command(filename, step['run'], [effective])
                results.extend(found)
                if not inherits and own is not None and effective and not resolved:
                    results.extend(addopts_findings(filename, own))
                judged = judged or (inherits and resolved)
            elif opaque:
                # The action may run pytest through an input the scan cannot read, so the value
                # it inherits counts unless a with: command that names pytest overrode it.
                covered = False
                for command in commands:
                    found, resolved = scan_command(filename, command, [effective])
                    results.extend(found)
                    covered = covered or resolved
                if effective and not covered:
                    results.extend(addopts_findings(filename, effective))
                judged = judged or (inherits and bool(effective))
            elif own:
                results.extend(addopts_findings(filename, own))
        if inherited and not judged and not steps:
            results.extend(addopts_findings(filename, inherited))
    return results


def pytest_config_findings(filename, content):
    """Read addopts from pytest configuration as data."""
    base = PurePosixPath(filename).name
    values = []
    if base.endswith('.toml'):
        headers = r'tool\.pytest(?:\.ini_options)?' if base == 'pyproject.toml' else 'pytest'
        for table in re.finditer(r'(?ms)^\[' + headers + r'\][ \t]*(?:#[^\n]*)?\n(.*?)(?=^\[|\Z)', content):
            option = re.search(r'''(?ms)^addopts[ \t]*=[ \t]*(\[.*?\]|""".*?"""|\'\'\'.*?\'\'\'|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')''', table.group(1))
            if option:
                text = option.group(1)
                if text[:3] in {'"""', "'''"}:
                    values.append(text[3:-3])
                elif text[0] in '"\'':
                    values.append(text[1:-1])
                else:
                    values.append(' '.join(a or b for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'', text)))
    else:
        parser = configparser.RawConfigParser(strict=False)
        try:
            parser.read_string(content)
        except configparser.Error:
            return []  # Not a readable INI file, so pytest cannot take options from it either.
        values = [parser.get(section, 'addopts') for section in ('pytest', 'tool:pytest') if parser.has_option(section, 'addopts')]
    return [item for value in values for item in addopts_findings(filename, value)]


def script_findings(filename, content):
    # Shell and PowerShell files are command text. Python/JS launchers often
    # embed their command as a string; inspect those literals separately.
    if PurePosixPath(filename).suffix in {'.py', '.js', '.mjs', '.cjs', '.ts'}:
        results = []
        for match in re.finditer(r'''(["'`])((?:\\.|(?!\1).)*?)\1''', content, re.S):
            literal = match.group(2)
            if re.search(r'\b(?:pytest|playwright)\b', literal):
                results.extend(parallelism_findings(filename, literal))
        # Environment mappings assign the options in a separate literal.
        for match in ADDOPTS_LITERAL.finditer(content):
            results.extend(addopts_findings(filename, match.group(2)))
        return results
    return parallelism_findings(filename, content)


def lint_files(files, repository, visibility, config, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    results, documents = [], {}
    for filename, content in sorted(files.items()):
        if workflow_path(filename):
            try:
                documents[filename] = yaml.safe_load(content)
            except yaml.YAMLError:
                pass  # reported as invalid-file below
    callee_inputs = callee_runner_inputs(documents)
    documents = {}
    for filename, content in sorted(files.items()):
        try:
            if workflow_path(filename):
                document = documents[filename] = yaml.safe_load(content)
                if not isinstance(document, dict):
                    raise ValueError('Workflow must be a mapping')
                results.extend(runner_findings(filename, document, repository, visibility, config, today, callee_inputs))
                results.extend(workflow_parallelism(filename, document))
            elif PurePosixPath(filename).name == 'package.json':
                for script in json.loads(content).get('scripts', {}).values():
                    if isinstance(script, str):
                        results.extend(parallelism_findings(filename, script))
            elif PurePosixPath(filename).name.startswith('ci-local'):
                results.extend(script_findings(filename, content))
            elif PurePosixPath(filename).name in PYTEST_CONFIG:
                results.extend(pytest_config_findings(filename, content))
        except (yaml.YAMLError, ValueError, TypeError, AttributeError) as error:
            results.append(finding('error', 'invalid-file', filename, str(error)))
    if visibility == 'public':
        try:
            results.extend(exception_caller_findings({name: doc for name, doc in documents.items() if isinstance(doc, dict)}, repository, config, today))
        except (ValueError, TypeError, AttributeError) as error:
            results.append(finding('error', 'invalid-file', '.github/workflows', str(error)))
    # The same literal can be reachable from several steps or scopes.
    return [item for index, item in enumerate(results) if item not in results[:index]]


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
        # actionlint requires both .git and .github/workflows to resolve local
        # reusable calls. This empty marker contains no target git configuration.
        Path(directory, '.git').mkdir()
        for name in sorted(files):
            if not workflow_path(name):
                continue
            target = Path(directory, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[name], encoding='utf-8')
        paths = [str(Path(directory, name)) for name in selected]
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
