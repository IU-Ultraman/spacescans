"""`python -m spacescans` — the same CLI as the `spacescans` console script."""
import sys

from spacescans.cli import main

if __name__ == "__main__":
    sys.exit(main())
