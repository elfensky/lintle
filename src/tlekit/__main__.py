"""Allow ``python -m tlekit`` to run the CLI."""

import sys

from tlekit.cli import main

if __name__ == "__main__":
    sys.exit(main())
