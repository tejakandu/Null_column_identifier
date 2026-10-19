"""Entry point: `python app.py` (also used by PyInstaller)."""

import sys

from nci.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
