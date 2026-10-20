#!/usr/bin/env python3
"""Audit every repository in an agent-ops rulesets snapshot, without executing it."""
import argparse
import json
from pathlib import Path

import yaml

import workflow_lint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rulesets', type=Path, required=True)
    parser.add_argument('--rulesets-revision', required=True)
    parser.add_argument('--actionlint', default='actionlint')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rulesets = yaml.safe_load(args.rulesets.read_text(encoding='utf-8'))
    reports = []
    for name in rulesets['repos']:
        repository = rulesets['owner'] + '/' + name
        print('Auditing ' + repository, flush=True)
        try:
            report = workflow_lint.inspect_remote(repository, args.actionlint)
        except Exception as error:  # Preserve incomplete audits as explicit failures.
            report = {'repository': repository, 'audit_error': str(error)}
        reports.append(report)
        args.output.write_text(json.dumps({'rulesets_revision': args.rulesets_revision, 'reports': reports}, indent=2) + '\n', encoding='utf-8')
    return int(any('audit_error' in report for report in reports))


if __name__ == '__main__':
    raise SystemExit(main())
