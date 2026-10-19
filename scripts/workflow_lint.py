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
# The exact environment file as one raw word, bare or double-quoted: bash's $GITHUB_ENV or
# ${GITHUB_ENV}, PowerShell's $env:GITHUB_ENV, and (only to flag it) cmd's %GITHUB_ENV%.
# A single-quoted '$GITHUB_ENV' is a literal path and $github_env another bash variable.
BASH_ENV = re.compile(r'(")?(?:\$GITHUB_ENV|\$\{GITHUB_ENV\})(?(1)")')
PWSH_ENV = re.compile(r'(")?(?i:\$env:GITHUB_ENV)(?(1)")')
ANY_ENV = re.compile(r'(")?(?:\$GITHUB_ENV|\$\{GITHUB_ENV\}|(?i:\$env:GITHUB_ENV|%GITHUB_ENV%))(?(1)")')
# Shell text split into raw words that keep their quotes, so a quoted or escaped '>>' stays an argument.
REDIRECT = r'\d*(?:>>|>&|>\||<<<|<<-|<<|<&|<>|>|<)|&>>|&>'
SHELL_SYNTAX = re.compile(r'''[ \t\r]+|(?P<comment>\#[^\n]*)|(?P<redirect>''' + REDIRECT + r''')|(?P<op>[;&|\n]+)'''
                          r'''|(?P<word>(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|<>'"\\])+)''', re.S)
# A word that expands nothing in bash or PowerShell: plain characters and quotes without $, ` or \.
LITERAL_PART = re.compile(r'''('[^'\n\r]*'|"[^"\\$`\n\r]*")|[\w@%+=:./-]+''')
PRINTF_LINE = re.compile(r'''(['"])([^'"\\%$`\n\r]*)\\n\1''')
HEREDOC = re.compile(r'''(?<!<)<<(-?)[ \t]*(['"]?)([\w.-]+)\2''')
# Shell and PowerShell blocks whose commands may not run.
OPENERS = {'if', 'while', 'until', 'for', 'foreach', 'case', 'select', 'switch', 'function', 'try', '{', '('}
CLOSERS = {'fi', 'done', 'esac', '}', ')'}
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


def addopts_findings(filename, value):
    """Judge a literal PYTEST_ADDOPTS or addopts value, which pytest puts before the command line."""
    try:
        tokens = shlex.split(value)
    except ValueError:
        return [finding('warning', 'unparsed-command', filename, 'Could not tokenize PYTEST_ADDOPTS; inspect manually')]
    results = [item for item in (worker_finding(filename, 'PYTEST_ADDOPTS', token, count) for token, count in worker_flags('pytest', tokens)) if item]
    if not results and re.search(r'\$[\w{(]', value):
        results.append(finding('warning', 'dynamic-parallelism', filename, 'PYTEST_ADDOPTS: cannot resolve options statically'))
    return results


def posix_env(command):
    """Rewrite cmd.exe and PowerShell PYTEST_ADDOPTS assignments as POSIX NAME='value'."""
    def assignment(value):
        return "PYTEST_ADDOPTS='" + value.strip().replace("'", "'\\''") + "'"
    command = re.sub(r'(?im)^[ \t]*set[ \t]+"?PYTEST_ADDOPTS=([^\r\n"]*)"?[ \t]*\r?$', lambda m: assignment(m.group(1)), command)
    return re.sub(r'''(?i)\$env:PYTEST_ADDOPTS[ \t]*=[ \t]*(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\r\n;]*))''',
                  lambda m: assignment(next(group for group in m.groups() if group is not None)), command)


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
    Each pipeline is (operator before it, [words of each command]). None when a quote never closes."""
    pipelines, words, before, position = [], [], '', 0
    commands = []
    while position < len(command):
        match = SHELL_SYNTAX.match(command, position)
        if not match:
            return None
        position = match.end()
        if match.lastgroup == 'op':
            commands.append(words)
            words = []
            if match.group() not in {'|', '|&'}:
                pipelines.append((before, commands))
                commands, before = [], match.group()
        elif match.lastgroup in {'redirect', 'word'}:
            words.append(match.group())
    pipelines.append((before, commands + [words]))
    return pipelines


def literal(word):
    """The text of a raw word that expands nothing, or None."""
    if not word or LITERAL_PART.sub('', word):
        return None
    return ''.join(match.group(1)[1:-1] if match.group(1) else match.group() for match in LITERAL_PART.finditer(word))


def printed_line(words):
    """The one line `echo TEXT` or `printf 'TEXT\\n'` prints, or None when it is not certain."""
    if len(words) == 2 and words[0] == 'echo':
        return literal(words[1])
    if len(words) == 2 and words[0] == 'printf':
        match = PRINTF_LINE.fullmatch(words[1])
        return match.group(2) if match else None
    if len(words) == 3 and words[0] == 'printf' and words[1] in {"'%s\\n'", '"%s\\n"'}:
        return literal(words[2])
    return None


def cmdlet_target(words):
    """For an Add-Content, Set-Content or Out-File call aimed at $env:GITHUB_ENV, its -Value word
    ('' when the text comes down the pipeline); None for any other call or parameter."""
    if not words or words[0].lower() not in FILE_WRITERS - {'tee'}:
        return None
    path, positional, rest = None, [], list(words[1:])
    while rest:
        word = rest.pop(0)
        name = word.lower()
        if name in {'-path', '-filepath', '-literalpath', '-value', '-encoding'} and rest:
            value = rest.pop(0)
            if name == '-value':
                positional[1:1] = [value]
            elif name != '-encoding':
                path = value
            elif literal(value) is None:
                return None
        elif name != '-append' and (name.startswith('-') or name.startswith('>')):
            return None  # -NoNewline, -WhatIf, redirections and the like change what is written
        elif name != '-append':
            positional.append(word)
    if path is None and positional:
        path = positional.pop(0)
    if path is None or not PWSH_ENV.fullmatch(path) or len(positional) > 1:
        return None
    return positional[0] if positional else ''


def env_file_write(commands):
    """The PYTEST_ADDOPTS value a pipeline certainly writes to the exact environment file, or None.

    Only these forms count, so anything else leaves the earlier value in force:
    `echo TEXT` or `printf 'TEXT\\n'` (or `printf '%s\\n' TEXT`) redirected with > or >> to
    $GITHUB_ENV, or piped into `tee [-a] "$GITHUB_ENV"`; `echo TEXT` redirected to $env:GITHUB_ENV;
    a PowerShell string or echo piped into Out-File/Add-Content/Set-Content $env:GITHUB_ENV, or
    Add-Content/Set-Content $env:GITHUB_ENV with a literal -Value. TEXT must expand nothing."""
    line = None
    if len(commands) == 1:
        words = commands[0]
        value = cmdlet_target(words)
        if value:
            line = literal(value)
        elif len(words) > 2 and words[-2] in {'>', '>>', '1>', '1>>'}:
            target = words[-1]
            if BASH_ENV.fullmatch(target) or PWSH_ENV.fullmatch(target) and words[0] == 'echo':
                line = printed_line(words[:-2])
    elif len(commands) == 2:
        source, sink = commands
        if sink[-2:] == ['>', '/dev/null']:
            sink = sink[:-2]
        if sink[:1] == ['tee'] and len(sink) in {2, 3} and sink[1:-1] in ([], ['-a'], ['--append']) and BASH_ENV.fullmatch(sink[-1]):
            line = printed_line(source)
        elif cmdlet_target(sink) == '':
            line = literal(source[0]) if len(source) == 1 and source[0][:1] in {'"', "'"} else printed_line(source)
    match = re.fullmatch(r'PYTEST_ADDOPTS=(.*)', line or '', re.S)
    return match.group(1) if match else None


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


def env_file_writes(filename, command, bodies):
    """Read the PYTEST_ADDOPTS writes to $GITHUB_ENV in a command, failing closed.

    Returns (the value last written by a recognised, unconditional write or None, findings).
    A pipeline that sends text naming PYTEST_ADDOPTS to the file in any other form, inside a
    block or after && or ||, does not change the value; it is flagged and any value it may
    write is judged."""
    pipelines = shell_pipelines(command)
    if pipelines is None:
        return None, []  # tokenizing failed; scan_command already warns
    written, results, depth = None, [], 0
    for before, commands in pipelines:
        first = commands[0][0] if commands[0] else ''
        if first in CLOSERS:
            depth -= 1
        conditional = depth != 0 or '&&' in before or '||' in before
        if first in OPENERS or first.startswith('(') or first.endswith('()') or '{' in commands[0][1:]:
            depth += 1
        if not touches_env_file(commands):
            continue
        words = [word for command_words in commands for word in command_words]
        text = '\n'.join([bodies[int(word[1:])] for word in words if word.startswith('\0')] + [
            ' '.join(plain(word) for word in words if not word.startswith('\0') and not ANY_ENV.fullmatch(word)
                     and not re.fullmatch(REDIRECT, word))])
        if not re.search('PYTEST_ADDOPTS', text, re.I):
            continue
        value = None if conditional else env_file_write(commands)
        if value is not None:
            written = value
            continue
        results.append(finding('warning', 'unverified-env-write', filename,
                               'Cannot tell what this writes to GITHUB_ENV; later steps are judged on the earlier PYTEST_ADDOPTS'))
        for match in re.finditer(r'PYTEST_ADDOPTS=([^\n]*)', text, re.I):
            results.extend(addopts_findings(filename, match.group(1)))
    return written, results


def plain(word):
    """A raw word without its quotes, for judging the text it may carry."""
    try:
        return ' '.join(shlex.split(word))
    except ValueError:
        return word


def scan_command(filename, command, addopts=None):
    """Check worker caps in one command string, judging each command on its own effective parallelism.

    addopts is the PYTEST_ADDOPTS value already in effect. pytest reads it before
    the command line, so a worker flag on pytest's own command line replaces it,
    but only for that command: a later command that may run pytest (make, tox,
    a script) still sees the value. An inline assignment belongs to its command;
    `export` and bare assignments persist. Text that only mentions the name, such
    as `echo PYTEST_ADDOPTS=`, assigns nothing. A value written to $GITHUB_ENV
    reaches the steps after this one, never the commands in it, so it is returned
    separately.
    Returns (findings, addopts in effect afterwards, whether the value passed in
    was judged against a command or replaced, the value written to GITHUB_ENV or None).
    """
    # Shell, cmd and PowerShell line continuations.
    command, bodies = split_heredocs(posix_env(re.sub(r'(?:\\|`|\^)\r?\n', ' ', command)))
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [finding('warning', 'unparsed-command', filename, 'Could not tokenize command; inspect manually')], addopts, True, None
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
    # The PYTEST_ADDOPTS value this command writes to GITHUB_ENV, read from its own shell syntax.
    written, results = env_file_writes(filename, command, bodies)
    inherited, resolved = bool(addopts), not addopts
    unjudged = False  # a value assigned by this script that no command has seen yet
    for number, segment in enumerate(segments):
        heredocs = [bodies[int(token[1:])] for token in segment if token.startswith('\0')]
        segment = [token for token in segment if not token.startswith('\0')]
        inline, persist, words = command_word(segment)
        scoped = inline is not None and not persist
        effective = inline if scoped else addopts
        tools = [(index, 'pytest' if is_pytest(token) else 'playwright') for index, token in enumerate(words) if is_pytest(token) or is_playwright(token)]
        judged = False
        for position, (index, tool) in enumerate(tools):
            end = tools[position + 1][0] if position + 1 < len(tools) else len(words)
            flags = list(worker_flags(tool, words[index + 1:end]))
            results.extend(item for item in (worker_finding(filename, tool, token, value) for token, value in flags) if item)
            if tool == 'pytest' and effective:
                judged = True
                if not flags:
                    results.extend(addopts_findings(filename, effective))
        if not tools and words and effective and not neutral_command(words):
            judged = True
            results.extend(addopts_findings(filename, effective))
        # A heredoc body is input for its command, which runs it (if at all) in a child process.
        for body in heredocs:
            results.extend(scan_command(filename, body, effective)[0])
        if judged and not scoped:
            resolved, unjudged = resolved or inherited, False
        # State changes take effect after the command that carries them. A bare string piped
        # into another command (PowerShell's "PYTEST_ADDOPTS=" | Out-File) is text, not an assignment.
        assigned = inline if persist and not piped[number] else None
        if assigned is None and words[:1] == ['unset'] and any(token.upper() == 'PYTEST_ADDOPTS' for token in words[1:]):
            assigned = ''
        if assigned is not None:
            addopts, inherited, resolved, unjudged = assigned, False, True, bool(assigned)
    if addopts and unjudged:
        results.extend(addopts_findings(filename, addopts))
    if written:
        results.extend(addopts_findings(filename, written))
    return results, addopts, resolved, written


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
        judged, persisted = not inherited, None
        for step in steps:
            own = env_addopts(step.get('env'))
            # Step env beats a value exported through GITHUB_ENV, which beats job and workflow env.
            effective = next((value for value in (own, persisted, inherited) if value is not None), None)
            inherits = own is None and persisted is None
            uses = step.get('uses')
            opaque = isinstance(uses, str) and not NEUTRAL_ACTION.fullmatch(uses.split('@')[0])
            # A command handed to an action through with: (retry, xvfb, ...) runs like a run step.
            commands = [value for value in strings(step.get('with')) if TEST_TOOL.search(value)] if opaque else []
            if isinstance(step.get('run'), str):
                found, _, resolved, written = scan_command(filename, step['run'], effective)
                results.extend(found)
                if not inherits and own is not None and effective and not resolved:
                    results.extend(addopts_findings(filename, own))
                judged = judged or (inherits and resolved)
                # A step with its own if: may not run, so what it writes cannot replace the earlier value.
                if written is not None and 'if' not in step:
                    persisted = written
            elif opaque:
                # The action may run pytest through an input the scan cannot read, so the value
                # it inherits counts unless a with: command that names pytest overrode it.
                covered = False
                for command in commands:
                    found, _, resolved, _ = scan_command(filename, command, effective)
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
