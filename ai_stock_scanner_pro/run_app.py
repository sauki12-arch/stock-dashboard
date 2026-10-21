"""PyInstaller entry point (also usable as `python run_app.py`)."""
import sys

from aiscanner.cli import main

if __name__ == "__main__":
    sys.exit(main())
