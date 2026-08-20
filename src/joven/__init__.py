"""joven — EPUB translation helper."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("joven-ebook-annotator")
except PackageNotFoundError:  # a source tree with no install; tests never rely on this
    __version__ = "0.0.0+unknown"
