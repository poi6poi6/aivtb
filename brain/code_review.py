"""Host-owned source snapshots and reviewable, conflict-checked code changes."""
import difflib
import hashlib
import json
import os
import re
import shlex
from pathlib import Path

DIRS = {'brain', 'body', 'tests', 'tools', 'docs', 'vendor'}
EXTS = {'.py', '.js', '.mjs', '.ts', '.html', '.css', '.md', '.json', '.txt', '.ps1', '.cmd'}
SKIP = {'__pycache__', 'node_modules', '.git', '.venv', 'data', '.agents', '.codex'}


def _literal_report_command(command):
    """Recognize only one data-writing/output command with literal arguments."""
    if not isinstance(command, str) or len(command) > 4000:
        return False
    literal = r"""(?:'(?:[^']|'')*'|"[^"$`\r\n]*"|[^\s'"`$;&|<>(){}\[\]#*,?=]+)"""
    return re.fullmatch(
        r'(?:Set-Content|Add-Content|Out-File|Write-Output|echo|write)'
        r'(?:[ \t]+' + literal + r')+[ \t]*', command.strip(), re.I) is not None


def is_test_command(command):
    """Keep opaque runner wrappers as tests, excluding proven literal reports."""
    return (isinstance(command, str) and not _literal_report_command(command) and
            bool(re.search(r'(?:-m\s+(?:unittest|pytest)\b|\bnode\s+["\']?(?:\.?[/\\])?tests[/\\]|'
                           r'\bnpm\s+(?:run\s+)?test\b)', command)))


def test_scope(command):
    """Identify the whole direct invocation, not just its first test target.

    Quoting is decoded without treating Windows path separators as escapes.
    Shell wrappers/compound commands cannot provide trustworthy scope here.
    Unknown runner arguments remain part of the key so a narrower rerun can
    never silently replace a broader failure. Only display flags are ignored.
    """
    if not isinstance(command, str) or len(command) > 4000:
        return None
    text = command.strip()
    if text.startswith('& '):
        text = text[2:].lstrip()
    # This common PowerShell suffix only forwards the direct runner's result;
    # recognize its scope so an exact successful rerun can clear a failure.
    text = re.sub(r';\s*exit\s+\$LASTEXITCODE\s*;?\s*$', '', text, flags=re.I)
    # A single literal status message does not change the tested scope, but it
    # can mask the runner's exit status. Keep checking the runner's own summary.
    # No interpolation, escaping, redirection, pipes or additional commands.
    text, status_suffix = re.subn(
        r';[ \t]*(?:Write-Output|echo)[ \t]+'
        r'(?:[A-Za-z0-9_. -]+|"[A-Za-z0-9_. -]+"|\'[A-Za-z0-9_. -]+\')[ \t]*$',
        '', text, flags=re.I)
    if any(char in text for char in ('\n', '\r', ';', '&', '|', '>', '<', '`', '$')):
        return None
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split, lexer.commenters, lexer.escape = True, '', ''
    try:
        words = list(lexer)
    except ValueError:
        return None
    if not words:
        return None
    executable = words.pop(0).replace('\\', '/').rsplit('/', 1)[-1].lower()
    runtime = []
    if re.fullmatch(r'(?:python(?:\d+(?:\.\d+)?)?|py)(?:\.exe)?', executable):
        while words and words[0] != '-m':
            option = words.pop(0)
            if option in {'-X', '-W'} and words:
                value = words.pop(0)
                if (option, value) != ('-X', 'utf8'):
                    runtime.extend((option, value))
            elif option in {'-B', '-u'}:
                continue
            elif option in {'-E', '-I', '-s', '-S', '-O', '-OO'} or re.fullmatch(r'-\d+(?:\.\d+)?', option):
                runtime.append(option)
            else:
                return None
        if len(words) < 2 or words[0] != '-m' or words[1] not in {'unittest', 'pytest'}:
            return None
        runner, words = words[1], words[2:]
    elif executable in {'node', 'node.exe'}:
        if not words or not re.match(r'^(?:\./)?tests/', words[0].replace('\\', '/')):
            return None
        runner = 'node'
    elif executable in {'npm', 'npm.cmd'}:
        if words[:2] == ['run', 'test']:
            words = words[1:]
        if not words or words[0] != 'test':
            return None
        runner, words = 'npm', words[1:]
    else:
        return None
    # Node/custom npm scripts may print PASS and then crash without a final
    # failure summary. They must retain the runner's actual exit status.
    if status_suffix and runner not in {'unittest', 'pytest'}:
        return None
    # Do not discard an argument value merely because it resembles a display
    # flag (for example pytest -k "-v"). Keep all selection/options verbatim.
    display = {'-q', '--quiet', '-v', '--verbose'} if runner in {'unittest', 'pytest'} else set()
    normalized = []
    positional_only = False
    for word in words:
        if word == '--':
            positional_only = True
        if not positional_only and word in display and (not normalized or not normalized[-1].startswith('-')):
            continue
        normalized.append(word)
    return runner, tuple(runtime), tuple(normalized)


def tests_executed(output, runner):
    """Require a positive executed-test summary, not merely a zero exit code."""
    if not isinstance(output, str):
        return False
    # Runner summaries may be coloured. Removing only ANSI control sequences
    # leaves ordinary quoted text inert because all summary matches are anchored.
    output = re.sub(r'\x1b\[[0-9;]*m', '', output).replace('\r\n', '\n').replace('\r', '\n')
    if runner in {'unittest', 'npm'}:
        counts = re.findall(r'(?m)^Ran (\d+) tests? in [^\r\n]+$', output)
        results = re.findall(r'(?m)^OK(?: \(([^\r\n]*)\))?[ \t]*$', output)
        if counts:
            skipped = [int(match.group(1)) if (match := re.search(r'\bskipped=(\d+)\b', result)) else 0
                       for result in results]
            return (len(counts) == len(results) and
                    not re.search(r'(?m)^FAILED\b', output) and
                    all(int(count) > skip for count, skip in zip(counts, skipped)))
    if runner in {'pytest', 'npm'}:
        summaries = re.findall(r'(?m)^[= ]*(\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)[^\r\n]*? in \d+(?:\.\d+)?s)(?: \([\d:]+\))?[= ]*$', output)
        if summaries:
            for summary in summaries:
                counts = dict((name, int(count)) for count, name in
                              re.findall(r'(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b', summary))
                if (sum(counts.get(name, 0) for name in ('passed', 'xfailed', 'xpassed')) == 0 or
                        any(counts.get(name, 0) for name in ('failed', 'error', 'errors'))):
                    return False
            return True
    if runner in {'node', 'npm'}:
        return bool(re.search(r'(?m)^PASS[: ][^\r\n]+$', output)) and not re.search(r'(?m)^FAIL(?:ED)?[: (]', output)
    return False


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_cwd_matches(value, workspace):
    """Missing/relative legacy evidence cannot prove which source was tested."""
    if not isinstance(value, str) or not value or '\x00' in value:
        return False
    try:
        path = Path(value)
        return path.is_absolute() and path.resolve() == Path(workspace).resolve()
    except (OSError, ValueError):
        return False


def safe(root, name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name:
        raise ValueError('非法源码路径')
    target = root / p
    for part in [target, *target.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('源码不能经过链接目录')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('源码路径越界')
    return target


def sources(root):
    found = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        rel = Path(base).relative_to(root)
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.') and (rel.parts or d in DIRS)]
        for name in files:
            p = Path(base) / name
            vendor_metadata = (rel.parts and rel.parts[0] == 'vendor'
                               and (p.suffix.lower() == '.toml' or name == 'LICENSE'))
            if name.startswith('.') or (p.suffix.lower() not in EXTS and not vendor_metadata):
                continue
            key = p.relative_to(root).as_posix()
            p = safe(root, key)
            data = p.read_bytes()
            if len(data) > 2_000_000:
                raise ValueError('源码文件超过 2 MB：' + key)
            data.decode('utf-8-sig')
            found[key] = data
    if sum(map(len, found.values())) > 30_000_000:
        raise ValueError('源码副本超过 30 MB')
    return found


class CodeReview:
    def version(self):
        return {name: digest(data) for name, data in sources(self.copy).items()}

    def __init__(self, root, folder, job_id):
        self.root = Path(root).resolve()
        self.task = Path(folder).resolve()
        self.copy = self.task / 'workspace'
        self.host = self.root / 'data/code-reviews' / job_id

    def prepare(self):
        original = sources(self.root)
        self.host.mkdir(parents=True, exist_ok=False)
        self.copy.mkdir()
        for name, data in original.items():
            for base in (self.host / 'baseline', self.copy):
                p = safe(base, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        return self.copy

    def inspect(self, commands):
        before = sources(self.host / 'baseline')
        after = sources(self.copy)
        changes, patch, errors = [], [], []
        for name in sorted(before.keys() | after.keys()):
            old, new = before.get(name), after.get(name)
            if old == new:
                continue
            changes.append({'path': name, 'before': digest(old) if old is not None else None,
                            'after': digest(new) if new is not None else None,
                            'operation': 'add' if old is None else 'delete' if new is None else 'modify'})
            patch.extend(difflib.unified_diff((old or b'').decode('utf-8-sig').splitlines(True),
                         (new or b'').decode('utf-8-sig').splitlines(True), 'a/' + name, 'b/' + name))
            if new is not None and name.endswith('.py'):
                try:
                    compile(new, name, 'exec')
                except (SyntaxError, ValueError) as exc:
                    errors.append(str(exc))
        if len(changes) > 100:
            raise ValueError('单次改动超过 100 个文件，请拆分任务')
        # A rerun replaces only the same complete scope. A successful subset
        # must not erase failures elsewhere in an earlier multi-target run.
        tests_by_scope = {}
        unscoped_failures = []
        for command in commands:
            if not command.get('test') or _literal_report_command(command.get('command')):
                continue
            scope = test_scope(command.get('command'))
            if scope is not None:
                tests_by_scope[scope] = command
            else:
                # Shell wrappers may hide a failing runner behind exit code 0.
                # Without a known scope no subsequent subset can prove that
                # all tests were fixed; use a fresh reviewed revision instead.
                unscoped_failures.append(command)
        version = {name: digest(data) for name, data in after.items()}
        passed = not unscoped_failures and bool(tests_by_scope) and all(c.get('exit_code') == 0 and
            test_cwd_matches(c.get('cwd'), self.copy) and
            c.get('source_before') == version and c.get('source_after') == version and
            tests_executed(c.get('output'), scope[0]) for scope, c in tests_by_scope.items())
        report = {'changes': changes, 'diff': ''.join(patch), 'commands': commands,
                  'source_version': version,
                  'baseline_version': {name: digest(data) for name, data in before.items()},
                  'syntax_errors': errors, 'tests_passed': passed,
                  'can_apply': bool(changes) and passed and not errors}
        # Persist host-owned review and exact approved bytes outside executor writable roots.
        staging = self.host / 'staged'
        staging.mkdir(exist_ok=True)
        for c in changes:
            if c['after'] is not None:
                p = safe(staging, c['path']); p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(after[c['path']])
        (self.host / 'review.json').write_text(json.dumps(report, ensure_ascii=False), 'utf-8')
        return report

    def seed_parent(self, folder, job_id):
        parent = CodeReview(self.root, folder, job_id)
        if not (parent.host / 'review.json').exists():
            return
        for change in parent.read()['changes']:
            path = safe(self.copy, change['path'])
            current = digest(path.read_bytes()) if path.exists() else None
            if current == change['after']:
                continue
            if current != change['before']:
                raise ValueError('后续任务与当前源码冲突：' + change['path'])
            if change['after'] is None:
                path.unlink(missing_ok=True)
            else:
                data = safe(parent.host/'staged', change['path']).read_bytes()
                if digest(data) != change['after']:
                    raise ValueError('上次修订文件校验失败')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def read(self):
        return json.loads((self.host / 'review.json').read_text('utf-8'))

    def apply(self, token):
        report = self.read()
        if token != digest(json.dumps(report, sort_keys=True).encode()) or not report['can_apply']:
            raise ValueError('改动版本失效或检查未通过，请重新查看')
        if (self.host / 'applied.json').exists():
            raise ValueError('该改动已经应用')
        if report.get('baseline_version') != {name: digest(data) for name, data in sources(self.root).items()}:
            raise ValueError('项目已变化，测试依赖版本失效，请重新创建修订并测试')
        # Check every file before changing any file, including edits made since the snapshot.
        planned = []
        for c in report['changes']:
            p = safe(self.root, c['path'])
            old = p.read_bytes() if p.exists() else None
            if (digest(old) if old is not None else None) != c['before']:
                raise ValueError('项目已变化，未应用任何文件：' + c['path'])
            data = safe(self.host / 'staged', c['path']).read_bytes() if c['after'] else None
            if data is not None and digest(data) != c['after']:
                raise ValueError('待应用文件校验失败')
            planned.append((p, old, data))
        changed = []
        try:
            for p, old, data in planned:
                changed.append((p, old))
                if data is None:
                    p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
        except OSError:
            for p, old in reversed(changed):
                if old is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(old)
            raise
        (self.host / 'applied.json').write_text(json.dumps({'files': len(changed)}), 'utf-8')
        return len(changed)

    def public(self):
        report = self.read()
        current = {name:digest(data) for name,data in sources(self.root).items()}
        valid = 'source_version' in report and report.get('baseline_version') == current
        return {**report, 'can_apply': report['can_apply'] and valid,
                'version_valid': valid, 'token': digest(json.dumps(report, sort_keys=True).encode())}

    def rollback(self):
        if not (self.host/'applied.json').exists() or (self.host/'rolled-back.json').exists():
            raise ValueError('没有可回退的已应用版本')
        planned=[]
        for c in self.read()['changes']:
            path=safe(self.root,c['path'])
            current=path.read_bytes() if path.exists() else None
            if (digest(current) if current is not None else None) != c['after']:
                raise ValueError('检测到后续修改，未回退任何文件：'+c['path'])
            previous=safe(self.host/'baseline',c['path']).read_bytes() if c['before'] else None
            if previous is not None and digest(previous)!=c['before']:
                raise ValueError('原始备份校验失败')
            planned.append((path,current,previous))
        changed=[]
        try:
            for path,current,previous in planned:
                changed.append((path,current))
                if previous is None: path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(previous)
        except OSError:
            for path,current in reversed(changed):
                if current is None: path.unlink(missing_ok=True)
                else: path.write_bytes(current)
            raise
        (self.host/'rolled-back.json').write_text(json.dumps({'files':len(changed)}),'utf-8')
        return len(changed)
