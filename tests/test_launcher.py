"""Exercise the Windows launcher with inert scripts in an isolated copy."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe')
SCRIPTS = {
    'run': 'start-all.ps1', 'local-test': 'start-local-test.ps1',
    'tasks-start': 'start-agent-tasks.ps1', 'tasks-stop': 'stop-agent-tasks.ps1',
    'minecraft-start': 'start-minecraft.ps1', 'minecraft-stop': 'stop-minecraft.ps1',
    'tts': 'start-indextts2.ps1', 'friend-stop': 'stop-friend-test.ps1',
    'voice-settings': 'tts-settings.ps1', 'fish-check': 'tts-settings.ps1',
}
STUB = r"""param([int]$Port = 18080, [switch]$NoBrowser, [switch]$ValidateOnly, [switch]$CheckService)
$fixtureRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$scriptName = Split-Path -Leaf $PSCommandPath
$record = @{script=$scriptName; port=$Port; no_browser=[bool]$NoBrowser;
    validate_only=[bool]$ValidateOnly; check_service=[bool]$CheckService; cwd=[string](Get-Location); pid=$PID}
Add-Content -LiteralPath (Join-Path $fixtureRoot 'calls.jsonl') -Encoding UTF8 -Value ($record | ConvertTo-Json -Compress)
if ($env:AI_VTUBER_FIXTURE_FAIL -eq $scriptName) { exit 37 }
exit 0
"""


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell required')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '中文 空格 & (启动测试)'
        self.scripts = self.root / 'tools' / 'launcher'
        self.scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / '启动.cmd', self.root / '启动.cmd')
        shutil.copyfile(ROOT / 'tools/launcher/launch.ps1', self.scripts / 'launch.ps1')
        for name in SCRIPTS.values():
            (self.scripts / name).write_text(STUB, 'utf-8-sig')

    def run_launcher(self, *options, input=None, fail=None, cmd=False):
        env = os.environ.copy()
        env.pop('AI_VTUBER_FIXTURE_FAIL', None)
        if fail:
            env['AI_VTUBER_FIXTURE_FAIL'] = fail
        if cmd:
            # /S /C needs an outer quote pair in addition to the batch path's
            # quotes. Avoid list2cmdline stripping that shell-specific layer.
            command = ('"' + os.environ.get('COMSPEC', 'cmd.exe') + '" /d /s /c ""'
                       + str(self.root / '启动.cmd') + '" '
                       + subprocess.list2cmdline(options) + '"')
        else:
            command = [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                       '-File', str(self.scripts / 'launch.ps1'), *options]
        # Production launchers are replaced before every invocation: no service,
        # model, credentials, port, process tree or real data is touched.
        return subprocess.run(command, cwd=self.temp.name, env=env, input=input,
                              text=True, encoding='utf-8', errors='replace',
                              capture_output=True, timeout=30)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text('utf-8-sig').splitlines()] if path.exists() else []

    def prepare_real_launcher(self, *, backend='mock'):
        # Keep the actual control flow and OS mutex; replace only the Python
        # executable dependency with an inert script. HTTP, listener-table,
        # browser and child-process boundaries are test doubles, never live services.
        source = (ROOT / 'tools/launcher/start-all.ps1').read_text('utf-8-sig')
        needle = '$mainPython = Join-Path $root ".venv\\Scripts\\python.exe"'
        self.assertEqual(source.count(needle), 1)
        source = source.replace(needle, '$mainPython = Join-Path $root ".venv\\Scripts\\main-fixture.ps1"')
        listeners = '[Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()'
        self.assertEqual(source.count(listeners), 1)
        source = source.replace(listeners, '(Get-FixtureTcpListeners)')
        source = source.replace('& subst.exe', 'Invoke-FixtureSubst')
        (self.scripts / 'start-all.ps1').write_text(source, 'utf-8-sig')
        (self.root / 'tools/configure_tts.py').write_text('# replaced by inert Python fixture', 'utf-8')
        (self.scripts / 'launcher-cleanup.ps1').write_text('throw "must not execute"', 'utf-8-sig')
        config = self.root / 'config'
        config.mkdir()
        (config / 'settings.yaml').write_text(
            f'tts:\n  backend: {backend}\nwebui:\n  host: 127.0.0.1\n  port: 18099\n', 'utf-8')
        python_dir = self.root / '.venv/Scripts'
        python_dir.mkdir(parents=True)
        (python_dir / 'main-fixture.ps1').write_text(r'''param([string]$m)
if ($m -match 'configure_tts.py$') {
    Add-Content -LiteralPath (Join-Path $env:AI_VTUBER_FIXTURE_ROOT 'effects.jsonl') -Encoding UTF8 -Value '{"kind":"fish_health"}'
    if ($env:AI_VTUBER_FIXTURE_FISH_FAIL -eq '1') { exit 1 }
    exit 0
}
Add-Content -LiteralPath (Join-Path $env:AI_VTUBER_FIXTURE_ROOT 'effects.jsonl') -Encoding UTF8 -Value (@{
    kind='main'; tts_state=$env:AI_VTUBER_TTS_STARTUP_STATE; tts_pid=$env:AI_VTUBER_TTS_OWNED_PID
} | ConvertTo-Json -Compress)
if ($env:AI_VTUBER_FIXTURE_HOLD -eq '1') {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (-not (Test-Path -LiteralPath (Join-Path $env:AI_VTUBER_FIXTURE_ROOT 'release'))) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'fixture release timed out' }
        Start-Sleep -Milliseconds 30
    }
}
exit 0
''', 'utf-8-sig')
        wrapper = self.root / 'invoke-real.ps1'
        wrapper.write_text(r'''param([switch]$NoBrowser, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
function Record-Effect($record) {
    Add-Content -LiteralPath (Join-Path $env:AI_VTUBER_FIXTURE_ROOT 'effects.jsonl') -Encoding UTF8 -Value ($record | ConvertTo-Json -Compress)
}
function Invoke-WebRequest($Uri, [switch]$UseBasicParsing, $TimeoutSec, $MaximumRedirection) {
    Record-Effect @{kind='http'; uri=$Uri}
    if ($Uri -match '/health$') {
        if ($env:AI_VTUBER_FIXTURE_TTS_HEALTH -eq 'ready') {
            return [pscustomobject]@{StatusCode=200; Content='{"status":"ok","backend":"indextts2"}'}
        }
        throw [Net.WebException]::new('fixture TTS loading', [Net.WebExceptionStatus]::ConnectFailure)
    }
    if ($env:AI_VTUBER_FIXTURE_HEALTH -eq 'absent') {
        throw [Net.WebException]::new('fixture offline', [Net.WebExceptionStatus]::ConnectFailure)
    }
    if ($env:AI_VTUBER_FIXTURE_HEALTH -eq 'timeout') {
        throw [Net.WebException]::new('fixture timeout', [Net.WebExceptionStatus]::Timeout)
    }
    return [pscustomobject]@{StatusCode=200; Content=(Get-Content -LiteralPath (Join-Path $env:AI_VTUBER_FIXTURE_ROOT 'health.json') -Raw)}
}
function Get-FixtureTcpListeners {
    if ($env:AI_VTUBER_FIXTURE_LISTENER -eq '1') { return [pscustomobject]@{Port=18099} }
}
function Start-Process($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru,
                       $WorkingDirectory, $RedirectStandardOutput, $RedirectStandardError) {
    Record-Effect @{kind='process'; file=$FilePath; arguments=$ArgumentList; window=$WindowStyle}
    if ($PassThru) {
        if ($FilePath -eq 'V:\IndexTTS2\.venv\Scripts\python.exe') {
            return [pscustomobject]@{Id=999999; StartTime=[DateTime]'2026-01-01'; HasExited=$false}
        }
        return [pscustomobject]@{Id=0}
    }
}
function Get-Process($Id, $ErrorAction) {
    if ($Id -eq 999999) {
        $started = if ($env:AI_VTUBER_FIXTURE_TTS_REUSED -eq '1') { [DateTime]'2026-01-02' } else { [DateTime]'2026-01-01' }
        return [pscustomobject]@{Id=999999; StartTime=$started}
    }
    return Microsoft.PowerShell.Management\Get-Process -Id $Id -ErrorAction SilentlyContinue
}
function Stop-Process($Id, $ErrorAction) { Record-Effect @{kind='stop'; id=$Id} }
function Wait-Process($Id, $Timeout, $ErrorAction) { }
function Invoke-FixtureSubst($Name, $Target) {
    Record-Effect @{kind='subst'; drive=$Name; target=$Target}
    $env:AI_VTUBER_FIXTURE_SUBST = if ($Target -eq '/D') { '' } else { $Target }
    $global:LASTEXITCODE = 0
}
# Isolate validation's read-only drive inspection from the developer's V:.
Add-Type -TypeDefinition @'
public static class AiVtuberSubstNative {
    public static uint QueryDosDevice(string name, System.Text.StringBuilder buffer, int size) {
        string target = System.Environment.GetEnvironmentVariable("AI_VTUBER_FIXTURE_SUBST");
        if (string.IsNullOrEmpty(target)) return 0;
        buffer.Append(@"\??\" + target);
        return (uint)buffer.Length;
    }
}
'@
function Test-Path($LiteralPath) {
    if ($LiteralPath.StartsWith('V:\')) { return [bool]$env:AI_VTUBER_FIXTURE_SUBST }
    return Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath
}
& (Join-Path $PSScriptRoot 'tools\launcher\start-all.ps1') -NoBrowser:$NoBrowser -ValidateOnly:$ValidateOnly
exit $LASTEXITCODE
''', 'utf-8-sig')
        self.write_health()
        return wrapper

    def write_health(self, **changes):
        health = {'scope': 'main', 'ready': False, 'boot_id': '0123456789abcdef',
                  'code': {name: 'f' * 64 for name in
                           ('brain/main.py', 'brain/webui.py', 'brain/static/index.html')}}
        health.update(changes)
        (self.root / 'health.json').write_text(json.dumps(health), 'utf-8')

    def real_command(self, *options, health='main', hold=False, listener=True):
        env = os.environ.copy()
        env.update(AI_VTUBER_FIXTURE_ROOT=str(self.root), AI_VTUBER_FIXTURE_HEALTH=health,
                   AI_VTUBER_FIXTURE_HOLD='1' if hold else '0',
                   AI_VTUBER_FIXTURE_LISTENER='1' if listener else '0',
                   AI_VTUBER_FIXTURE_SUBST='', AI_VTUBER_FIXTURE_TTS_HEALTH='', AI_VTUBER_FIXTURE_TTS_REUSED='')
        return dict(args=[POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                          str(self.root / 'invoke-real.ps1'), *options],
                    cwd=self.temp.name, env=env, text=True, encoding='utf-8', errors='replace',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def effects(self):
        path = self.root / 'effects.jsonl'
        return [json.loads(line) for line in path.read_text('utf-8-sig').splitlines()] if path.exists() else []

    def test_every_service_action_routes_from_unrelated_cwd(self):
        for action, name in SCRIPTS.items():
            with self.subTest(action=action):
                result = self.run_launcher('-Action', action)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls()[-1]['script'], name)

    def test_cmd_handles_cjk_spaces_and_forwards_options_and_exit_code(self):
        result = self.run_launcher('-Action', 'local-test', '-Port', '18091',
                                   '-NoBrowser', '-ValidateOnly', cmd=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.calls()[-1]
        self.assertEqual(record['port'], 18091)
        self.assertTrue(record['no_browser'])
        self.assertTrue(record['validate_only'])
        failed = self.run_launcher('-Action', 'run', cmd=True, fail='start-all.ps1')
        self.assertEqual(failed.returncode, 37, failed.stdout + failed.stderr)

    def test_check_validates_both_even_if_first_fails(self):
        result = self.run_launcher('-Action', 'check', '-Port', '18092', fail='start-all.ps1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([row['script'] for row in self.calls()], ['start-all.ps1', 'start-local-test.ps1'])
        self.assertTrue(all(row['validate_only'] for row in self.calls()))
        self.assertEqual(self.calls()[-1]['port'], 18092)

    def test_menu_recovers_from_invalid_selection_and_child_failure(self):
        result = self.run_launcher(input='invalid\n1\n2\n0\n', fail='start-all.ps1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([row['script'] for row in calls], ['start-all.ps1', 'start-local-test.ps1'])
        self.assertNotEqual(calls[0]['pid'], calls[1]['pid'], 'each launcher needs its own lifetime')

    def test_advanced_menu_and_eof_do_not_launch_extra_services(self):
        result = self.run_launcher(input='6\n1\n0\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row['script'] for row in self.calls()], ['start-agent-tasks.ps1'])
        empty = self.run_launcher(input='')
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_voice_settings_and_fish_check_have_separate_explicit_actions(self):
        result = self.run_launcher(input='7\n6\n7\n0\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([row['script'] for row in self.calls()], ['tts-settings.ps1', 'tts-settings.ps1'])
        self.assertEqual([row['check_service'] for row in self.calls()], [False, True])

    def test_validation_flag_cannot_trigger_stop_and_bad_port_cannot_launch(self):
        for options in [('-Action', 'tasks-stop', '-ValidateOnly'),
                        ('-Action', 'local-test', '-Port', '65536'),
                        ('-Action', 'unknown')]:
            with self.subTest(options=options):
                result = self.run_launcher(*options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_missing_component_fails_with_nonzero_exit(self):
        (self.scripts / 'start-all.ps1').unlink()
        result = self.run_launcher('-Action', 'run')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_real_validation_failure_does_not_create_or_clear_error_marker(self):
        # Missing Python stops validation before any native mapping inspection;
        # the actual migrated launcher must not touch its live-start log state.
        shutil.copyfile(ROOT / 'tools/launcher/start-all.ps1', self.scripts / 'start-all.ps1')
        log_directory = self.root / 'data/launcher'
        result = self.run_launcher('-Action', 'run', '-ValidateOnly')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(log_directory.exists())
        log_directory.mkdir(parents=True)
        marker = log_directory / 'last-start-error.txt'
        marker.write_text('preserve previous startup failure', 'utf-8')
        result = self.run_launcher('-Action', 'run', '-ValidateOnly')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(marker.read_text('utf-8'), 'preserve previous startup failure')

    def test_real_launcher_reuses_identified_main_even_when_not_ready(self):
        self.prepare_real_launcher(backend='indextts2')
        # No IndexTTS Python exists: reuse must happen before any TTS setup.
        for options in [('-NoBrowser',), ()]:
            with self.subTest(options=options):
                result = subprocess.run(**self.real_command(*options), timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Reusing the existing AI VTuber', result.stdout)
        processes = [event for event in self.effects() if event['kind'] == 'process']
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0]['file'], 'http://127.0.0.1:18099')
        self.assertFalse(any(event['kind'] == 'main' for event in self.effects()))
        self.assertFalse((self.root / 'data').exists())

    def test_real_launcher_rejects_unidentified_http_service(self):
        self.prepare_real_launcher()
        for changes in ({'scope': 'local-test'}, {'code': {}}, {'boot_id': 'generic-service'}):
            with self.subTest(changes=changes):
                self.write_health(**changes)
                result = subprocess.run(**self.real_command('-NoBrowser'), timeout=15)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('does not identify', result.stdout)
        self.assertTrue(all(event['kind'] == 'http' for event in self.effects()))

    def test_fish_launcher_reuses_external_service_without_index_environment(self):
        self.prepare_real_launcher(backend='fish_speech')
        validation = subprocess.run(**self.real_command('-ValidateOnly'), timeout=15)
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        self.assertEqual(self.effects(), [], 'validation does not request a model or health endpoint')
        result = subprocess.run(**self.real_command('-NoBrowser', health='absent'), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Reusing external Fish Speech', result.stdout)
        effects = self.effects()
        self.assertEqual([e['kind'] for e in effects], ['http', 'process', 'main'])
        self.assertEqual(len([e for e in effects if e['kind'] == 'process']), 1, 'only cleanup helper is started')

    def test_fish_health_is_independent_and_does_not_block_the_text_core(self):
        self.prepare_real_launcher(backend='fish_speech')
        kwargs = self.real_command('-NoBrowser', health='absent')
        kwargs['env']['AI_VTUBER_FIXTURE_FISH_FAIL'] = '1'
        result = subprocess.run(**kwargs, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([e['kind'] for e in self.effects()], ['http', 'process', 'main'])

    def test_index_model_loading_does_not_block_main_and_owned_child_is_cleaned(self):
        self.prepare_real_launcher(backend='indextts2')
        python = self.root / 'IndexTTS2/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_text('never executed: Start-Process is a fixture', 'utf-8')
        result = subprocess.run(**self.real_command('-NoBrowser', health='absent'), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        effects = self.effects()
        main = next(e for e in effects if e['kind'] == 'main')
        self.assertEqual(main['tts_state'], 'loading')
        self.assertEqual(main['tts_pid'], '999999')
        self.assertEqual(sum(e['kind'] == 'http' and e['uri'].endswith('/health') for e in effects), 1)
        processes = [e for e in effects if e['kind'] == 'process']
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(e['window'] == 'Hidden' for e in processes))
        self.assertIn('-TtsIdentity', processes[1]['arguments'])
        self.assertEqual([e['id'] for e in effects if e['kind'] == 'stop'], [999999])
        self.assertEqual([e['target'] for e in effects if e['kind'] == 'subst'], [str(self.root), '/D'])

    def test_missing_index_dependency_degrades_voice_but_starts_main(self):
        self.prepare_real_launcher(backend='indextts2')
        result = subprocess.run(**self.real_command('-NoBrowser', health='absent'), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(next(e for e in self.effects() if e['kind'] == 'main')['tts_state'], 'unavailable')
        self.assertFalse(any(e['kind'] in {'stop', 'subst'} for e in self.effects()))
        self.assertFalse((self.root / 'data/launcher/last-start-error.txt').exists())

    def test_reused_tts_pid_is_never_stopped(self):
        self.prepare_real_launcher(backend='indextts2')
        python = self.root / 'IndexTTS2/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_text('inert fixture', 'utf-8')
        kwargs = self.real_command('-NoBrowser', health='absent')
        kwargs['env']['AI_VTUBER_FIXTURE_TTS_REUSED'] = '1'
        result = subprocess.run(**kwargs, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(e['kind'] == 'stop' for e in self.effects()))

    def test_existing_index_service_and_mapping_remain_unowned(self):
        self.prepare_real_launcher(backend='indextts2')
        python = self.root / 'IndexTTS2/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_text('inert fixture', 'utf-8')
        kwargs = self.real_command('-NoBrowser', health='absent')
        kwargs['env'].update(AI_VTUBER_FIXTURE_TTS_HEALTH='ready', AI_VTUBER_FIXTURE_SUBST=str(self.root))
        result = subprocess.run(**kwargs, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        effects = self.effects()
        self.assertEqual(next(e for e in effects if e['kind'] == 'main')['tts_state'], 'ready')
        self.assertEqual(sum(e['kind'] == 'process' for e in effects), 1, 'only cleanup helper is started')
        self.assertFalse(any(e['kind'] in {'stop', 'subst'} for e in effects))

    def test_unrelated_mapping_is_preserved_and_only_voice_degrades(self):
        self.prepare_real_launcher(backend='indextts2')
        python = self.root / 'IndexTTS2/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_text('inert fixture', 'utf-8')
        kwargs = self.real_command('-NoBrowser', health='absent')
        kwargs['env']['AI_VTUBER_FIXTURE_SUBST'] = str(self.root / 'unrelated')
        result = subprocess.run(**kwargs, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        effects = self.effects()
        self.assertEqual(next(e for e in effects if e['kind'] == 'main')['tts_state'], 'unavailable')
        self.assertFalse(any(e['kind'] in {'stop', 'subst'} for e in effects))

    def test_real_launcher_serializes_startup_and_validation_ignores_lock(self):
        self.prepare_real_launcher()
        first = subprocess.Popen(**self.real_command('-NoBrowser', health='absent', hold=True))
        try:
            deadline = time.monotonic() + 12
            while not any(event['kind'] == 'main' for event in self.effects()):
                if first.poll() is not None:
                    out, err = first.communicate()
                    self.fail(f'fixture exited before main: {out}{err}')
                self.assertLess(time.monotonic(), deadline, 'fixture main did not start')
                time.sleep(.05)
            before = self.effects()
            marker = self.root / 'data/launcher/last-start-error.txt'
            marker.parent.mkdir(parents=True)
            marker.write_text('preserve marker', 'utf-8')
            validation = subprocess.run(**self.real_command('-ValidateOnly'), timeout=15)
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
            self.assertIn('Validation OK', validation.stdout)
            self.assertEqual(self.effects(), before, 'validation must not probe or launch')
            self.assertEqual(marker.read_text('utf-8'), 'preserve marker')

            duplicate = subprocess.run(**self.real_command('-NoBrowser', health='absent'), timeout=15)
            self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
            self.assertIn('already starting or running', duplicate.stdout)
            self.assertEqual(sum(event['kind'] == 'main' for event in self.effects()), 1)
            self.assertEqual(sum(event['kind'] == 'process' for event in self.effects()), 1)
            self.assertEqual(marker.read_text('utf-8'), 'preserve marker')
        finally:
            (self.root / 'release').touch()
            out, err = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, out + err)
        # A normal exit releases the mutex so a later intentional launch works.
        again = subprocess.run(**self.real_command('-NoBrowser', health='absent'), timeout=15)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(sum(event['kind'] == 'main' for event in self.effects()), 2)

    def test_real_launcher_does_not_launch_when_health_times_out(self):
        self.prepare_real_launcher()
        result = subprocess.run(**self.real_command('-NoBrowser', health='timeout'), timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Unable to check', result.stdout)
        self.assertEqual([event['kind'] for event in self.effects()], ['http'])

    def test_real_launcher_can_start_when_loopback_times_out_but_has_no_listener(self):
        self.prepare_real_launcher()
        result = subprocess.run(**self.real_command('-NoBrowser', health='timeout', listener=False), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event['kind'] for event in self.effects()], ['http', 'process', 'main'])

    def test_remote_timeout_cannot_be_treated_as_an_empty_local_port(self):
        self.prepare_real_launcher()
        config = self.root / 'config/settings.yaml'
        config.write_text(config.read_text('utf-8').replace('127.0.0.1', '192.0.2.10'), 'utf-8')
        result = subprocess.run(**self.real_command('-NoBrowser', health='timeout', listener=False), timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([event['kind'] for event in self.effects()], ['http'])

    def test_all_powershell_sources_parse_in_windows_51_and_chinese_has_bom(self):
        sources = sorted((ROOT / 'tools/launcher').glob('*.ps1'))
        for source in sources:
            content = source.read_bytes()
            if any(char > 127 for char in content):
                self.assertTrue(content.startswith(b'\xef\xbb\xbf'), source.name)
        parser = self.root / 'parse.ps1'
        parser.write_text(r"""param([string]$Folder)
Get-ChildItem -LiteralPath $Folder -Filter '*.ps1' | ForEach-Object {
    $tokens = $null; $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { $errors | Out-String | Write-Host; exit 1 }
}
exit 0
""", 'utf-8-sig')
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(parser), '-Folder', str(ROOT / 'tools/launcher')],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
