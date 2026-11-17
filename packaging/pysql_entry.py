"""PyInstaller entry point.

PyInstaller freezes a *script*, not a console-script entry point, so this thin
wrapper just calls :func:`pysql.cli.main`. Build it with ``scripts/build-binary.sh``.
"""

import sys

from pysql.cli import main

if __name__ == "__main__":
    sys.exit(main())
