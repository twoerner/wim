#! /usr/bin/env python3
## Copyright (C) 2025  Trevor Woerner <twoerner@gmail.com>
## vim: sw=4 ts=4 sts=4 expandtab

import re
import sys

from .CLI import main

if __name__ == "__main__":
    sys.argv[0] = re.sub(r"(-script\.pyw|\.exe|\.pyz)?$", "", sys.argv[0])
    sys.exit(main())
