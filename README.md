# rn — Renoise media kit

One CLI for the mechanical, error-prone half of editing AI video: frame-exact probing and frames, viewable contact
sheets, shot boundaries, audio as facts (loudness, beats, word timestamps), HTML-to-PNG-sequence rendering, an EDL
assembler, delivery finishing and QA. It measures and guarantees the mechanics; every creative decision stays yours.
The same `rn` runs in the Renoise media sandbox and on your machine.

```sh
brew install ffmpeg-full                               # ffmpeg 7+ with libass (Homebrew's plain ffmpeg has none)
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"   # keg-only; Linux: your distro's ffmpeg
uv tool install 'renoise-kit[all]'                     # or pick extras: listen, transcribe, faces, capture, captions, fonts, fx
playwright install chromium                            # only for rn capture

rn --help
rn sheet take.mp4 --fps 4                              # labelled contact sheet
rn assemble edit.json -o out.mp4 --preview             # half-size draft, fast
rn compare out.mp4 reference.mp4 --cuts                # side by side, cut timing compared
rn qa out.mp4 --edl edit.json                          # delivery checks, exit 1 on FAIL
```

`rn <command> --help` is the documentation. A command whose optional dependency is missing says which extra to install.

Design, EDL schema, output standard and roadmap: [SPEC.md](SPEC.md).

## Develop

```sh
uv run pytest -q                                       # needs ffmpeg with libass; playwright install chromium for capture tests
git tag v$(sed -n "s/^__version__ = '\(.*\)'/\1/p" rn/__init__.py) && git push --tags   # release.yml publishes to PyPI
```
