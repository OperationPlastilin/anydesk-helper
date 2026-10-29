"""Точка входа для сборки AnyDeskHelper.exe (PyInstaller)."""

import sys

from anydesk_helper.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
