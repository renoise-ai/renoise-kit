"""What differs between macOS, Linux and Windows: text encodings, path syntax, command-line length, fontconfig."""
import json
import re
import shutil
import subprocess

import pytest

from rn import ff, fonts
from rn.cli import main
from rn.ff import probe


def ffmpeg_major() -> int:
    first = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True).stdout.split('\n')[0]
    match = re.search(r'version n?(\d+)\.', first)
    return int(match.group(1)) if match else 99  # a git build: assume current


def test_edl_with_bom_cjk_and_awkward_folder(media, tmp_path):
    # Notepad writes a BOM, Chinese is not in any Windows ANSI code page, and the demuxer list must quote the folder.
    folder = tmp_path / "it's 片段"
    folder.mkdir()
    shutil.copyfile(media / 'ramp.mp4', folder / 'ramp.mp4')
    edl = folder / 'edit.json'
    edl.write_text(json.dumps({'output': {'width': 320, 'height': 240, 'fps': 24},
                               'tracks': [{'clips': [{'id': 'a', 'src': 'ramp.mp4', 'duration': 0.5, 'meta': {'copy': '新的标语'}},
                                                     {'id': 'b', 'src': 'ramp.mp4', 'duration': 0.5}]}]}, ensure_ascii=False),
                   encoding='utf-8-sig')
    out = folder / '成片.mp4'
    assert main(['assemble', str(edl), '-o', str(out)]) == 0
    assert probe(out, exact=True)['video']['frames'] == 24
    assert main(['edit', str(edl), 'set', 'a', 'meta.copy=另一句']) == 0
    assert json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips'][0]['meta']['copy'] == '另一句'


@pytest.mark.skipif(ffmpeg_major() < 7, reason='"-/option file" needs ffmpeg 7')
def test_long_filter_graph_goes_through_a_file(media, tmp_path):
    chain = ','.join(['null'] * (ff.LONG_FILTER // 4))
    assert len(chain) > ff.LONG_FILTER
    out = tmp_path / 'long.mp4'
    ff.run(['ffmpeg', '-i', str(media / 'ramp.mp4'), '-vf', chain, '-an', '-y', str(out)])
    assert probe(out, exact=True)['video']['frames'] == 60


def test_fonts_without_fontconfig(monkeypatch):
    pytest.importorskip('fontTools')
    if not fonts.font_files():
        pytest.skip('no fonts in the system font folders')
    real = shutil.which
    monkeypatch.setattr(fonts.shutil, 'which', lambda name, *a, **k: None if name == 'fc-list' else real(name, *a, **k))
    result = fonts.fonts_command(family=None, script=None, licenses=False)
    assert result['count'] > 0
    row = result['fonts'][0]
    assert row['weights'] and all(w % 100 == 0 for w in row['weights']) and row['files']
    assert any('latin' in r['scripts'] for r in result['fonts'])
