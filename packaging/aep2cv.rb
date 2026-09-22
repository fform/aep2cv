class Aep2cv < Formula
  include Language::Python::Virtualenv

  desc "Convert After Effects projects to Cavalry scenes"
  homepage "https://github.com/fform/aep2cv"
  url "PLACEHOLDER: the aep2cv sdist URL from PyPI"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "python@3.13"
  # Ships pdftocairo, which converts .ai/.eps/.pdf artwork to SVG.
  depends_on "poppler" => :recommended

  resource "py-aep" do
    url "https://files.pythonhosted.org/packages/06/a1/6e52e347a1e0f41fa599e3013baead06634a157f960ed407a9e04771bbb1/py_aep-0.16.0.tar.gz"
    sha256 "283e165167c3d1ad12f50a740ecdff9b3c28632413f60e185016081ba4c8c4f5"
  end
  resource "attrs" do
    url "https://files.pythonhosted.org/packages/9a/8e/82a0fe20a541c03148528be8cac2408564a6c9a0cc7e9171802bc1d26985/attrs-26.1.0.tar.gz"
    sha256 "d03ceb89cb322a8fd706d4fb91940737b6642aa36998fe130a9bc96c985eff32"
  end
  resource "fonttools" do
    url "https://files.pythonhosted.org/packages/3e/c4/db6a7b5eb0656534c3aa2596c2c5e18830d74f1b9aa5aa8a7dff63a0b11d/fonttools-4.60.2.tar.gz"
    sha256 "d29552e6b155ebfc685b0aecf8d429cb76c14ab734c22ef5d3dea6fdf800c92c"
  end
  resource "PyYAML" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end
  resource "uharfbuzz" do
    url "https://files.pythonhosted.org/packages/66/5e/edf24fb25c46de69cf70c9d25ea4212649720ce1f55cd77a8315706478e5/uharfbuzz-0.51.7.tar.gz"
    sha256 "522de7e8e6df7d36ed0718cdd0c27089bb71b2c8c29a09257f80af1d576aee18"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "aep2cv", shell_output("#{bin}/aep2cv --help")
  end
end
