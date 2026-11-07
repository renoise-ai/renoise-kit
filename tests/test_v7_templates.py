"""rn template: built-in parameterised motion templates rendered with rn capture (needs Playwright + Chromium)."""
import json

import numpy as np
import pytest
from PIL import Image

from rn.ff import RnError
from rn.template import _names, template_command

pytest.importorskip('playwright')


def _render_or_skip(tmp_path, name, **kw):
    try:
        return template_command('render', name, out=str(tmp_path / name), width=180, height=320, fps=10, duration=kw.pop('duration', 1.0),
                                shards=1, **kw)
    except RnError as error:
        if 'Executable' in str(error) or 'install' in str(error):
            pytest.skip(f'no Chromium for Playwright: {error}')
        raise


def test_list_and_show_carry_every_param_with_a_default_and_a_doc():
    names = _names()
    assert {'headline', 'lower-third', 'callout', 'counter', 'bars', 'steps', 'text-behind'} <= set(names)
    for spec in template_command('list', None)['templates']:
        assert spec['what'] and {'font', 'font_file', 'in', 'out'} <= set(spec['params'])
        assert all('default' in p and p['doc'] for p in spec['params'].values())
    assert template_command('show', 'counter')['path'].endswith('counter.html')


@pytest.mark.parametrize('name', ['headline', 'lower-third', 'callout', 'counter', 'bars', 'steps', 'text-behind'])
def test_every_template_draws_on_a_transparent_frame(tmp_path, name):
    result = _render_or_skip(tmp_path, name)
    assert result['frames'] == 10 and result['template'] == name
    last_entrance = np.asarray(Image.open(tmp_path / name / '00008.png'))
    assert last_entrance.shape == (320, 180, 4)
    alpha = last_entrance[..., 3]
    assert 0.001 < (alpha > 0).mean() < 0.9  # something drawn, background left transparent


def test_params_reach_the_page_and_unknown_ones_are_refused(tmp_path):
    plain = _render_or_skip(tmp_path, 'counter', duration=1.5)
    assert plain['params']['to'] == 12000
    big = template_command('render', 'counter', out=str(tmp_path / 'big'), width=180, height=320, fps=10, duration=1.5, shards=1,
                           params=json.dumps({'to': 7, 'size': 0.4, 'accent': '#ff0000', 'label': ''}))
    frame = np.asarray(Image.open(tmp_path / 'big' / '00013.png')).astype(int)
    red = (frame[..., 3] > 200) & (frame[..., 0] > 200) & (frame[..., 1] < 60)
    assert big['params']['size'] == 0.4 and red.sum() > 200
    with pytest.raises(RnError, match='unknown params'):
        template_command('render', 'counter', out=str(tmp_path / 'x'), params='{"too": 1}')
    with pytest.raises(RnError, match='no template'):
        template_command('show', 'nope')
