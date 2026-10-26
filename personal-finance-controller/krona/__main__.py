"""Run KRONA: python -m krona"""

import sys

from .config import ConfigError, from_env
from .runtime import run


def main() -> int:
    try:
        settings = from_env()
    except ConfigError as exc:
        print(f"KRONA configuration error: {exc}", file=sys.stderr)
        return 1
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
