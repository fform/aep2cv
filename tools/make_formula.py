"""Generate a Homebrew formula for aep2cv, resources and all.

Homebrew installs a Python CLI into its own virtualenv, and every dependency
has to be pinned in the formula as a `resource` block. Hand-writing those is
the usual source of drift, so this builds them from PyPI metadata.

Homebrew's own `brew update-python-resources Formula/aep2cv.rb` does the same
job once the formula exists; this bootstraps the first one.
"""
import importlib.metadata as metadata
import json, urllib.request

PACKAGE = "aep2cv"
# Pinned to the versions installed here, so the formula ships the same set the
# converter is actually tested against.
DEPS = ["py-aep", "attrs", "fonttools", "PyYAML", "uharfbuzz"]


def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def sdist(name, version=None):
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=30) as fh:
        data = json.load(fh)
    version = version or data["info"]["version"]
    for entry in data["releases"][version]:
        if entry["packagetype"] == "sdist":
            return data["info"]["name"], version, entry["url"], entry["digests"]["sha256"]
    raise SystemExit(f"no sdist published for {name} {version}")


def main():
    resources = []
    for dep in DEPS:
        name, version, url, digest = sdist(dep, installed_version(dep))
        resources.append(f'''  resource "{name}" do
    url "{url}"
    sha256 "{digest}"
  end
''')
    print(f'''class Aep2cv < Formula
  include Language::Python::Virtualenv

  desc "Convert After Effects projects to Cavalry scenes"
  homepage "https://github.com/fform/aep2cv"
  url "PLACEHOLDER: the aep2cv sdist URL from PyPI"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "python@3.13"
  # Ships pdftocairo, which converts .ai/.eps/.pdf artwork to SVG.
  depends_on "poppler" => :recommended

{"".join(resources)}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "aep2cv", shell_output("#{{bin}}/aep2cv --help")
  end
end''')


main()
