"""edit.json version 2: placement and look in "style" under CSS names, keys left / top in px, version 1 upgraded on load,
and schema/edit.schema.json agreeing with rn on the shared fixtures other hosts test against."""
import json
from pathlib import Path

import pytest

from rn.edl import load
from rn.editops import upgrade_op
from rn.ff import RnError

SCHEMA_DIR = Path(__file__).resolve().parents[1] / 'schema'
FIXTURES = SCHEMA_DIR / 'fixtures'
# Why rn rejects each invalid fixture: part of its error message.
REJECTED_FOR = {
    'blend-hue': 'mix-blend-mode', 'fit-stretch': 'object-fit', 'key-x': 'keyable here', 'length-space': 'style.left',
    'main-width': "['width'] not supported here", 'odd-width': 'must be even', 'opacity-range': 'between 0 and 1',
    'two-sources': 'exactly one of', 'until-on-main': 'main-track clips play back to back', 'v1-field': 'x is now style.left',
    'version-3': 'version: 2',
}


def schema_validator():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((SCHEMA_DIR / 'edit.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize('path', sorted((FIXTURES / 'valid').glob('*.json')), ids=lambda p: p.stem)
def test_valid_fixtures_pass_the_schema_and_rn(path):
    schema_validator().validate(json.loads(path.read_text(encoding='utf-8')))
    load(path)


@pytest.mark.parametrize('path', sorted((FIXTURES / 'invalid').glob('*.json')), ids=lambda p: p.stem)
def test_invalid_fixtures_fail_the_schema_and_rn(path):
    assert not schema_validator().is_valid(json.loads(path.read_text(encoding='utf-8')))
    with pytest.raises(RnError, match=REJECTED_FOR[path.stem].replace('[', r'\[').replace(']', r'\]')):
        load(path)


V1 = {'output': {'width': 1080, 'height': 1920, 'fps': 30}, 'tracks': [
    {'clips': [{'color': '#203040', 'duration': 2, 'fit': 'contain',
                'keys': {'scale': [[0, 1], [2, 1.2, 'out']], 'x': [[0, 0], [2, 0.05]], 'y': [[0, -0.1]]}}]},
    {'clips': [{'color': '#ffffff', 'at': 0.5, 'duration': 1, 'x': 90, 'y': 1400, 'w': 900, 'h': 200, 'opacity': 0.8, 'blend': 'add',
                'keys': {'y': [[0, 1400], [1, 1300, 'out3']]}}]}]}
V2 = {'version': 2, 'output': {'width': 1080, 'height': 1920, 'fps': 30}, 'tracks': [
    {'clips': [{'color': '#203040', 'duration': 2, 'style': {'object-fit': 'contain', 'top': -192},
                'keys': {'scale': [[0, 1], [2, 1.2, 'out']], 'left': [[0, 0], [2, 54]]}}]},
    {'clips': [{'color': '#ffffff', 'at': 0.5, 'duration': 1,
                'style': {'left': '8.333333%', 'top': 1400, 'width': '900px', 'height': 200, 'opacity': 0.8, 'mix-blend-mode': 'plus-lighter'},
                'keys': {'top': [[0, 1400], [1, 1300, 'out3']]}}]}]}


def laid_out(edit):
    """What rn renders from: fit, placement, look and keys (rounded: px <-> fractions of the frame round-trip)."""
    keys = lambda c: {p: [(t, round(v, 9), e) for t, v, e in pts] for p, pts in (c.keys or {}).items()}  # noqa: E731
    return [(c.fit, c.x, c.y, c.w, c.h, c.opacity, c.blend, keys(c), c.timeline_start, c.frames) for c in edit.base + edit.overlays]


def write(tmp_path, name, raw):
    path = tmp_path / name
    path.write_text(json.dumps(raw), encoding='utf-8')
    return path


def test_version_1_and_version_2_lay_out_the_same(tmp_path):
    v1, v2 = load(write(tmp_path, 'v1.json', V1)), load(write(tmp_path, 'v2.json', V2))
    assert laid_out(v1) == laid_out(v2)
    main, overlay = v2.base[0], v2.overlays[0]
    assert main.keys['x'][-1][1] == pytest.approx(0.05) and main.keys['y'] == [(0.0, -0.1, 'linear')]  # px -> fraction of the frame
    assert (overlay.x, overlay.w, overlay.blend) == (90, 900, 'addition')


def test_upgrade_rewrites_the_file_and_keeps_the_timeline(tmp_path):
    path = write(tmp_path, 'e.json', V1)
    before = laid_out(load(path))
    upgrade_op(path)
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert raw['version'] == 2 and raw['tracks'][1]['clips'][0]['style']['mix-blend-mode'] == 'plus-lighter'
    assert 'x' not in raw['tracks'][1]['clips'][0] and 'left' in raw['tracks'][0]['clips'][0]['keys']
    assert laid_out(load(path)) == before
    with pytest.raises(RnError, match='already version 2'):
        upgrade_op(path)


def test_a_style_value_is_a_held_key_and_cannot_also_be_keyed(tmp_path):
    raw = json.loads(json.dumps(V2))
    raw['tracks'][1]['clips'][0]['style'] |= {'rotate': '5deg', 'scale': 1.5}
    overlay = load(write(tmp_path, 'held.json', raw)).overlays[0]
    assert overlay.keys['rotate'] == [(0.0, 5.0, 'linear')] and overlay.keys['scale'] == [(0.0, 1.5, 'linear')]
    raw['tracks'][1]['clips'][0]['keys']['scale'] = [[0, 1], [1, 2]]
    with pytest.raises(RnError, match='style.scale or keys.scale'):
        load(write(tmp_path, 'both.json', raw))
