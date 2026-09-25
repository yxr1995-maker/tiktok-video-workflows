#!/usr/bin/env python3
"""Convenience root wrapper for scripts/videoctl.py"""
import sys
from pathlib import Path

# Add scripts directory to path and execute videoctl.main()
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import videoctl

if __name__ == "__main__":
    sys.exit(videoctl.main())
