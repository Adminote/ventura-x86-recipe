"""intelbrew — Homebrew-free binary package management for Intel Macs.

Keeps Homebrew, npm, node and npx working on Intel Macs whose macOS version is
newer than the last macOS Homebrew still publishes Intel bottles for, by
transparently sourcing *equivalent binary packages* from official upstream
release channels (Node.js dist, GitHub Releases, Adoptium, PyPI, conda-forge,
Maven Central...) and verifying them on landing.

Public entry points:
    * ``intelbrew.cli:main``       command line (``ibrew``)
    * ``intelbrew.run()``          shim execution (``ibrew shim``)
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
