"""v0.7.0: rn doctor (what this machine can do with rn, offline) and rn schema (the edit.json schema shipped in the package)."""
import importlib.resources
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

from rn import __version__, assets, doctor
from rn.cli import main

KEYS = ['name', 'group', 'core', 'status', 'version', 'path', 'detail', 'commands', 'fix']
CORE = {'python', 'ffmpeg', 'ffprobe', 'encoders', 'numpy', 'pillow'}


def rn(*args) -> int:
    return main([str(a) for a in args])


def by_name(result: dict) -> dict:
    return {check['name']: check for check in result['checks']}


def test_doctor_reports_the_core_and_exits_0(tmp_path, capsys):
    report = tmp_path / 'doctor.json'
    assert rn('doctor', '--json', report) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert set(result) == {'ok', 'rn', 'python', 'platform', 'checks', 'missing', 'warnings', 'settings', 'seconds'}
    assert result['ok'] is True and result['rn'] == __version__
    checks = by_name(result)
    assert {c['name'] for c in result['checks'] if c['core']} == CORE
    assert all(checks[name]['status'] != 'missing' for name in CORE)
    for check in result['checks']:
        assert list(check) == KEYS
        assert check['status'] in ('ok', 'warn', 'missing')
        assert (check['fix'] is None) == (check['status'] == 'ok')
        assert check['commands'] and all(isinstance(c, str) for c in check['commands'])
    assert checks['rn']['status'] == 'ok' and checks['rn']['version'] == __version__  # package data in place
    for name in ('subtitles', 'xfade', 'sidechaincompress', 'ebur128', 'alimiter'):
        assert f'filter {name}' in checks
    assert checks['filter xfade']['commands'] == ['rn assemble (transitions)']
    assert {'opencv', 'librosa', 'faster-whisper', 'playwright', 'chromium', 'tesseract', 'caption fonts', 'yunet model'} <= set(checks)
    assert result['missing'] == [c['name'] for c in result['checks'] if c['status'] == 'missing']
    assert result['settings']['assets_manifest'] == assets._url()
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith('ok       python: ') and out[-1].startswith('doctor: core ok;')
    assert any(line.startswith('assets   manifest ') for line in out)


@pytest.mark.parametrize('module, check, core', [('cv2', 'opencv', False), ('librosa', 'librosa', False), ('numpy', 'numpy', True)])
def test_doctor_reports_a_missing_module(monkeypatch, module, check, core):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *a, **k: None if name == module else real(name, *a, **k))
    result = doctor.doctor_command()
    found = by_name(result)[check]
    assert found['status'] == 'missing' and found['version'] is None and check in result['missing']
    assert found['detail'] == f'not installed (module {module})'
    # An extra only narrows what works; a required module is a broken install.
    assert result['ok'] is not core and found['core'] is core
    if check == 'opencv':
        assert found['commands'] == ['rn faces', 'rn shots', 'rn compare --shots'] and 'extra "faces"' in found['fix']


def test_doctor_without_tools_keeps_its_checks_and_fails_on_the_core(monkeypatch, capsys):
    names = [c['name'] for c in doctor.doctor_command()['checks']]
    monkeypatch.setattr(shutil, 'which', lambda name, *a, **k: None)
    assert rn('doctor') == 1  # runs although ffmpeg is not on PATH, and says so
    out = capsys.readouterr().out
    assert 'missing  ffmpeg: not found on PATH' in out and 'missing  ffmpeg filters: not checked: no working ffmpeg' in out
    assert out.strip().splitlines()[-1].startswith('doctor: core broken (exit 1);')
    result = doctor.doctor_command()
    assert [c['name'] for c in result['checks']] == names  # the same checks whatever the machine has
    checks = by_name(result)
    assert checks['tesseract']['status'] == 'missing' and checks['tesseract']['commands'] == ['rn text']
    assert checks['caption fonts']['status'] == 'warn'  # not checkable without fc-list
    assert checks['encoders']['status'] == 'missing' and not result['ok']


def test_doctor_names_a_missing_package_file(monkeypatch):
    monkeypatch.setitem(doctor.PACKAGE_FILES, 'gone.json', ['rn gone'])
    result = doctor.doctor_command()
    check = by_name(result)['rn']
    assert check['status'] == 'missing' and check['commands'] == ['rn gone'] and 'gone.json' in check['detail']
    assert result['ok']  # the commands that read other files still work


def test_every_filter_doctor_checks_is_named_in_rn():
    package = Path(doctor.__file__).parent
    sources = '\n'.join(p.read_text(encoding='utf-8') for p in package.glob('*.py') if p.name != 'doctor.py')
    for command, names in doctor.FILTER_USES:
        assert command.startswith('rn ')
        for name in names.split():
            assert re.search(rf'(?<![\w-]){name}(?=[=,\[\'"])', sources), f'{name} ({command}) no longer appears in rn/'


def test_schema_prints_the_shipped_file(monkeypatch, capsys):
    shipped = importlib.resources.files('rn') / 'edit.schema.json'
    monkeypatch.setattr(shutil, 'which', lambda name, *a, **k: None)  # rn schema needs no ffmpeg
    assert rn('schema') == 0
    out = capsys.readouterr().out
    assert out == shipped.read_text(encoding='utf-8')
    assert json.loads(out)['title'] == 'edit.json version 2'
    assert rn('schema', '--path') == 0
    assert Path(capsys.readouterr().out.strip()) == Path(str(shipped))


def test_schema_json_writes_the_schema(tmp_path, capsys):
    out = tmp_path / 'schema.json'
    assert rn('schema', '--json', out) == 0
    capsys.readouterr()
    shipped = json.loads((importlib.resources.files('rn') / 'edit.schema.json').read_text(encoding='utf-8'))
    assert json.loads(out.read_text(encoding='utf-8')) == shipped
