# Packaging

## Release

```sh
python -m build                  # builds the wheel and sdist into dist/
twine upload dist/*              # publish to PyPI
```

That alone covers every platform: `pipx install aep2cv`, `uv tool install
aep2cv`, or plain `pip install aep2cv`.

## Homebrew

`aep2cv.rb` is a `Language::Python::Virtualenv` formula, which installs the
tool into its own virtualenv so it never touches the user's Python. Every
dependency is pinned as a `resource` block.

Use a **tap**, not homebrew-core: core has notability requirements and puts
releases behind their review queue. A tap is one repo named
`homebrew-aep2cv` containing `Formula/aep2cv.rb`, and installs with:

```sh
brew tap fform/aep2cv && brew install aep2cv
```

### Cutting a new version

1. Publish to PyPI.
2. Update `url` and `sha256` in the formula to the new sdist.
3. Refresh the dependency pins, either with Homebrew's own tool:
   ```sh
   brew update-python-resources Formula/aep2cv.rb
   ```
   or by regenerating the whole formula from the versions installed locally:
   ```sh
   python tools/make_formula.py > packaging/aep2cv.rb
   ```

### One wrinkle

`uharfbuzz` is a compiled extension, and Homebrew builds resources from
sdists, so installing compiles it. It arrives as a hard dependency of `py-aep`
but is only needed to *write* text back into a project - the converter only
reads. If build time becomes a problem, the fix is upstream: ask py-aep to
make `uharfbuzz` an optional extra. PyPI installs are unaffected, since pip
takes the prebuilt wheel.
