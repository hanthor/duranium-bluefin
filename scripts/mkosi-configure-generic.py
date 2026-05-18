#!/usr/bin/env python3
"""Generic mkosi configure stub - passes through config unchanged for non-device builds"""
import json
import sys

config = json.load(sys.stdin)
json.dump(config, sys.stdout, indent=2)
