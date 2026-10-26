"""Deterministic, bounded summaries referencing original log evidence."""

import re
from pathlib import Path

from embedded_auto.core.errors import ToolError


class LogAnalyzer:
    @staticmethod
    def analyze(path: Path, patterns: list[str] | None = None) -> dict:
        path = Path(path)
        try:
            rules = [re.compile(p, re.IGNORECASE) for p in
                     (patterns if patterns is not None else [r'\b(?:error|fatal|panic|exception|assert(?:ion)?(?: failed)?|fail(?:ed|ure)?)\b'])]
        except (re.error, TypeError) as exc:
            raise ToolError('CONFIG_INVALID', 'Invalid log regular expression: ' + str(exc)) from exc
        matches, levels = [], {'error': 0, 'warning': 0, 'info': 0}
        count = total = 0
        boot = []
        with path.open(encoding='utf-8', errors='replace') as source:
            for count, line in enumerate(source, 1):
                for level, expression in (('error', r'\b(error|fatal|panic|exception|assert|failed)\b'),
                                          ('warning', r'\b(warn|warning)\b'), ('info', r'\binfo\b')):
                    if re.search(expression, line, re.IGNORECASE):
                        levels[level] += 1
                if re.search(r'\b(boot|ready|started)\b', line, re.IGNORECASE) and len(boot) < 20:
                    boot.append({'line': count, 'text': line.rstrip()[:500]})
                for rule in rules:
                    for match in rule.finditer(line):
                        total += 1
                        if len(matches) < 100:
                            matches.append({'line': count, 'column': match.start(), 'pattern': rule.pattern,
                                            'match': match.group()[:200], 'text': line.rstrip()[:500]})
        return {'log_path': str(path), 'line_count': count, 'levels': levels,
                'match_count': total, 'matches': matches, 'truncated': total > len(matches),
                'boot_markers': boot, 'summary': f'{count} lines; {total} matches; {levels["error"]} error lines'}
