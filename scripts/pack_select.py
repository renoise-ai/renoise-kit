"""Expand the sound-effect selection rules into explicit entries in scripts/pack_sources.json (run once after choosing).

    uv run python scripts/pack_select.py SOURCES_DIR

Each rule: (category, opengameart slug, file names (fnmatch, sorted), name prefix, tags, max seconds). Files are numbered
per category in rule order. Not shipped.
"""
from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

K_UI, K_DIG, K_SCI, K_CAS = 'interface-sounds', '63-digital-sound-effects-lasers-phasers-space-etc', 'sci-fi-sounds', '54-casino-sound-effects-cards-dice-chips'
BFH, RS50, SF50, SF60 = '75-cc0-breaking-falling-hit-sfx', '50-cc0-retro-synth-sfx', '50-cc0-sci-fi-sfx', '60-cc0-sci-fi-sfx'
UI2, KEYS = 'ui-sound-effects-button-clicks-user-feedback-notifications', 'keyboard-soundpack-1-typing-and-single-keystrokes'

RULES = [
    ('whoosh', 'swishes-sound-pack', ['swish-*.wav'], 'swish', ['swish', 'fast', 'transition', 'swipe'], 2),
    ('whoosh', 'air-whoosh', ['whoosh2_0.wav'], 'air', ['air', 'long', 'transition', 'pass-by'], 3),
    ('swipe', K_DIG, ['highDown.mp3', 'highUp.mp3', 'phaseJump[1-5].mp3', 'phaserDown[1-3].mp3', 'phaserUp[1-3].mp3'], 'digital',
     ['digital', 'data', 'transition', 'tech', 'slide'], 2),
    ('swipe', K_UI, ['open_00[1-2].ogg', 'close_00[1-2].ogg', 'scroll_00[1-3].ogg'], 'ui', ['ui', 'slide', 'panel', 'transition'], 2),
    ('hit', BFH, ['bfh1_hit_*.ogg'], 'thud', ['thud', 'impact', 'drop', 'beat'], 2.5),
    ('hit', 'impact', ['*ImpactMeat0[1-2].ogg', '*ImpactStone.ogg', '*ImpactWood.ogg'], 'impact', ['impact', 'body', 'heavy'], 2.5),
    ('punch', 'punch', ['*Punch0[1-5].ogg'], 'punch', ['punch', 'fight', 'comedy', 'impact'], 2),
    ('metal', BFH, ['bfh1_metal_hit_*.ogg'], 'hit', ['metal', 'clang', 'impact'], 2.5),
    ('metal', K_SCI, ['impactMetal_00[0-4].ogg'], 'clank', ['metal', 'clank', 'mechanical'], 2.5),
    ('metal', 'metal-impact-sounds', ['bing1.wav', 'bong1.wav', 'clink1_0.wav', 'thud2.wav'], 'ring', ['metal', 'ring', 'bell', 'clink'], 3),
    ('glass', BFH, ['bfh1_glass_breaking_0[1-5].ogg', 'bfh1_glass_hit_0[1-2].ogg'], 'break', ['glass', 'shatter', 'crack', 'break'], 3),
    ('break', BFH, ['bfh1_wood_breaking_0[1-2].ogg', 'bfh1_rock_breaking_0[1-2].ogg', 'bfh1_breaking_0[1-2].ogg'], 'crash',
     ['break', 'crash', 'wood', 'rock', 'destroy'], 3),
    ('boom', K_SCI, ['explosionCrunch_00[0-4].ogg', 'lowFrequency_explosion_00[0-1].ogg'], 'explosion',
     ['explosion', 'boom', 'bass', 'drop', 'impact'], 4),
    ('boom', RS50, ['retro_explosion_0[1-3].ogg'], 'retro', ['explosion', 'retro', '8-bit', 'game'], 3),
    ('riser', K_DIG, ['powerUp[2-3].mp3'], 'digital', ['riser', 'build', 'tension', 'up'], 3),
    ('riser', RS50, ['power_up_03.ogg', 'synth_misc_04.ogg', 'synth_misc_12.ogg', 'synth_misc_16.ogg'], 'synth', ['riser', 'synth', 'build', 'sweep'], 4),
    ('riser', SF50, ['teleport_0[1-2].ogg', 'misc_03.ogg'], 'scifi', ['riser', 'teleport', 'sci-fi', 'reveal'], 4),
    ('riser', SF60, ['sfx_04a.ogg', 'sfx_21a.ogg'], 'sweep', ['riser', 'sweep', 'transition'], 4),
    ('power-down', 'energy-drain', ['*PowerDrain.ogg'], 'drain', ['power down', 'fail', 'drain', 'sad'], 3),
    ('power-down', 'machine-shutting-down', ['MachinePowerOff.ogg'], 'machine', ['power down', 'machine', 'shut down'], 4),
    ('power-down', K_DIG, ['zapThreeToneDown.mp3', 'lowDown.mp3', 'phaserDown[4-9].mp3'], 'digital', ['power down', 'down', 'fail', 'digital'], 2),
    ('power-down', K_UI, ['minimize_00[1-3].ogg'], 'ui', ['power down', 'close', 'shrink', 'ui'], 2),
    ('power-down', RS50, ['retro_die_0[1-3].ogg'], 'retro', ['power down', 'game over', 'retro', 'fail'], 3),
    ('power-up', K_DIG, ['powerUp1.mp3', 'powerUp4.mp3', 'powerUp6.mp3', 'powerUp10.mp3', 'powerUp12.mp3', 'zapThreeToneUp.mp3'], 'digital',
     ['power up', 'success', 'level up', 'digital'], 2.5),
    ('power-up', K_UI, ['maximize_00[1-3].ogg'], 'ui', ['power up', 'open', 'grow', 'ui'], 2),
    ('power-up', RS50, ['power_up_0[1-2].ogg', 'power_up_0[4-6].ogg'], 'retro', ['power up', 'retro', 'game', 'bonus'], 3),
    ('glitch', K_UI, ['glitch_00[1-4].ogg', 'scratch_00[1-3].ogg'], 'ui', ['glitch', 'error', 'digital', 'scratch'], 2),
    ('glitch', SF60, ['sfx_02a.ogg', 'sfx_02c.ogg', 'sfx_07a.ogg', 'sfx_07c.ogg', 'sfx_09a.ogg', 'sfx_13a.ogg', 'sfx_20a.ogg', 'sfx_22a.ogg'],
     'digital', ['glitch', 'digital', 'cyber', 'distortion'], 3),
    ('glitch', SF50, ['weird_0[1-5].ogg'], 'weird', ['glitch', 'weird', 'alien', 'distortion'], 3),
    ('glitch', K_DIG, ['spaceTrash[1-3].mp3'], 'trash', ['glitch', 'noise', 'digital'], 2),
    ('click', K_UI, ['click_00[1-5].ogg', 'tick_00[124].ogg', 'toggle_00[1-3].ogg', 'switch_00[1-3].ogg', 'select_00[1-4].ogg'], 'ui',
     ['click', 'ui', 'tap', 'button'], 1),
    ('click', UI2, ['click_[1-4].ogg'], 'soft', ['click', 'soft', 'ui', 'tap'], 1),
    ('pop', 'pop-effect-sounds', ['p_[1-5].ogg'], 'pop', ['pop', 'appear', 'bubble', 'cute'], 1.5),
    ('pop', 'bubbles-pop', ['pop.ogg'], 'bubble', ['pop', 'bubble', 'cute'], 1.5),
    ('pop', K_UI, ['drop_00[1-4].ogg', 'pluck_00[1-2].ogg'], 'drop', ['pop', 'drop', 'pluck', 'appear'], 1.5),
    ('ding', UI2, ['chimes.ogg', 'Ding.ogg', 'ding_deep.ogg'], 'bell', ['ding', 'notification', 'success', 'bell'], 4),
    ('ding', K_UI, ['confirmation_00[1-4].ogg', 'question_00[1-2].ogg', 'bong_001.ogg'], 'ui', ['ding', 'confirm', 'notification', 'ui'], 3),
    ('ding', 'alertnotification-sound', ['alert.ogg'], 'alert', ['ding', 'alert', 'notification', 'message'], 3),
    ('ding', K_DIG, ['twoTone[1-2].mp3', 'threeTone[1-2].mp3'], 'tone', ['ding', 'tone', 'notification', 'digital'], 2),
    ('error', K_UI, ['error_00[1-5].ogg'], 'ui', ['error', 'wrong', 'fail', 'buzz'], 2),
    ('error', UI2, ['negative_sound.ogg', 'negative_sound2.ogg', 'dum.ogg', 'alarm.ogg'], 'buzz', ['error', 'wrong', 'alarm', 'negative'], 3),
    ('type', KEYS, ['keypress-0[0-1][0-9].wav'], 'key', ['typing', 'keyboard', 'key', 'text'], 1),
    ('type', KEYS, ['generated-003_slow.wav', 'generated-005_fast.wav', 'human_vel-00[1-2].wav'], 'run', ['typing', 'keyboard', 'sequence'], 5),
    ('camera', 'camera', ['photo.ogg'], 'shutter', ['camera', 'shutter', 'photo', 'flash', 'snapshot'], 2),
    ('camera', K_UI, ['glass_00[1-3].ogg'], 'flash', ['flash', 'shine', 'sparkle', 'glass'], 2),
    ('data', SF50, ['terminal_0[1-8].ogg'], 'terminal', ['data', 'terminal', 'computer', 'beep', 'scan'], 2),
    ('data', K_DIG, ['tone1.mp3', 'pepSound[1-4].mp3'], 'beep', ['data', 'beep', 'scan', 'digital'], 2),
    ('data', SF60, ['sfx_05a.ogg', 'sfx_08a.ogg', 'sfx_12c.ogg'], 'scan', ['data', 'scan', 'processing', 'hud'], 3),
    ('static', 'static', ['ScatterNoise1.mp3'], 'scatter', ['static', 'noise', 'tv', 'radio'], 3),
    ('static', 'frequency-static-sound-effects', ['static1.wav'], 'radio', ['static', 'radio', 'noise', 'tune'], 3),
    ('static', K_SCI, ['computerNoise_00[0-3].ogg', 'forceField_00[0-1].ogg'], 'electric', ['static', 'electric', 'hum', 'noise'], 3),
    ('coin', '12-coin-sound-effects', ['coin.[1-8].ogg'], 'coin', ['coin', 'money', 'cash', 'reward', 'sale'], 2),
    ('coin', RS50, ['retro_coin_0[1-2].ogg'], 'retro', ['coin', 'retro', 'game', 'reward'], 2),
    ('coin', K_CAS, ['chips-stack-[1-3].ogg', 'chip-lay-1.ogg'], 'chips', ['coin', 'chips', 'money', 'stack'], 2),
    ('laser', K_SCI, ['laserSmall_00[0-2].ogg', 'laserRetro_00[0-1].ogg'], 'scifi', ['laser', 'shoot', 'game', 'sci-fi'], 2),
    ('laser', K_DIG, ['laser[1-3].mp3'], 'digital', ['laser', 'zap', 'game'], 2),
    ('card', K_CAS, ['card-place-[1-2].ogg', 'card-slide-[1-2].ogg', 'card-shuffle.ogg', 'card-fan-1.ogg', 'dice-throw-1.ogg'], 'card',
     ['card', 'paper', 'slide', 'flip', 'dice'], 3),
]


def main() -> None:
    sources = Path(sys.argv[1]) / 'sfx'
    sfx: dict[str, list] = {}
    for category, slug, patterns, prefix, tags, longest in RULES:
        files = sorted({f.name for f in (sources / slug).rglob('*') if f.is_file() and any(fnmatch.fnmatch(f.name, p) for p in patterns)},
                       key=lambda n: (len(n), n))
        if not files:
            raise SystemExit(f'{slug} {patterns}: nothing matches')
        entries = sfx.setdefault(category, [])
        start = sum(1 for e in entries if e['name'].startswith(prefix + '-')) + 1
        for k, name in enumerate(files, start):
            entries.append({'from': slug, 'file': name, 'name': f'{prefix}-{k}', 'tags': tags, 'max': longest})
    path = Path(__file__).with_name('pack_sources.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    data['sfx'] = sfx
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(sum(len(v) for v in sfx.values()), 'sounds in', len(sfx), 'categories:', {k: len(v) for k, v in sfx.items()})


if __name__ == '__main__':
    main()
