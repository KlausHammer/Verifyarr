#!/usr/bin/env python3
"""CLI compatibility shim — the actual code lives in the `verifyarr/` package. This file
just keeps the documented commands working unchanged:

    python3 verifyarr.py sweep --force
    python3 verifyarr.py single --video ... --subtitle ... (Bazarr's post-processing hook)
"""

from verifyarr.cli import main

if __name__ == "__main__":
    main()
