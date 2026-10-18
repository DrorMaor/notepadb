# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""Thin entry point so PyInstaller has a top-level script to build from
(the package itself uses relative imports, which don't work if a module
inside it is executed directly as __main__)."""
from notepadb.app import main

if __name__ == "__main__":
    main()
