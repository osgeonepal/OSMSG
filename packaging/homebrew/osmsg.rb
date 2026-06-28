class Osmsg < Formula
  include Language::Python::Virtualenv

  desc "OpenStreetMap Stats Generator: per-user node/way/relation edit counts"
  homepage "https://github.com/osgeonepal/osmsg"
  url "https://pypi.org/packages/source/o/osmsg/osmsg-1.1.2.tar.gz"
  sha256 "8406e438dee0670af0f7379ce8d9c71fd6e5ecc29e1f6ad691045ebde64f9aac"
  license "MIT"

  depends_on "python@3.12"

  # Tap formula: build an isolated venv and pip-install osmsg with its dependencies (all publish
  # wheels: osmium, duckdb, pyarrow, shapely). Simpler and more maintainable than vendoring every
  # transitive dep as a `resource`; for a homebrew-core submission, generate those with
  # `brew update-python-resources osmsg` and switch to `virtualenv_install_with_resources`.
  def install
    venv = virtualenv_create(libexec, "python3.12")
    venv.pip_install_and_link buildpath
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/osmsg --version")
  end
end
