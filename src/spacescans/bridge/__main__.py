"""`python -m spacescans.bridge <command> --request req.json --result res.json`."""
import sys

from spacescans.bridge.main import main

if __name__ == "__main__":
    sys.exit(main())
