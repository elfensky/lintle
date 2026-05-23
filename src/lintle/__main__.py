"""Allow ``python -m lintle`` to run the CLI."""

import sys

from lintle.cli import main

if __name__ == "__main__":
    sys.exit(main())
