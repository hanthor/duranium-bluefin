#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import subprocess
import sys


def main() -> int:
    for var in ("PMOS_DEVICE", "PMOS_VARIANT", "RELEASE"):
        if not os.environ.get(var):
            print(f"Error: {var} is not set", file=sys.stderr)
            return 1

    device = os.environ["PMOS_DEVICE"]
    variant = os.environ["PMOS_VARIANT"]
    release = os.environ["RELEASE"]
    srcdir = os.environ["SRCDIR"]

    config = json.load(sys.stdin)

    result = subprocess.run(
        [f"{srcdir}/scripts/pmaports.py",
          # Allow downloading pmaports inside mkosi.tools sandbox (it has network access)
          "--device", device,
          "--ui", variant,
          "--release", release],
        stdout=subprocess.PIPE,
        check=True,
    )
    pmaports = json.loads(result.stdout)

    image = config.get("Image")

    if image == "main":
        # main needs arch/sector size from deviceinfo
        for key in ("Architecture", "SectorSize"):
            if pmaports.get(key):
                config[key] = pmaports[key]
    elif image == "base":
        # base gets device+UI packages resolved from pmaports
        existing = config.get("Packages", [])
        dynamic = pmaports.get("Packages", [])
        config["Packages"] = list(dict.fromkeys(existing + dynamic))
    elif image == "default-initrd":
        # pmaports emits InitrdPackages to distinguish base-image pkgs from
        # initramfs pkgs. for the initrd image, these need to go in Packages.
        existing = config.get("Packages", [])
        dynamic = pmaports.get("InitrdPackages", [])
        config["Packages"] = list(dict.fromkeys(existing + dynamic))
    else:
        print(f"Error: unexpected image name '{image}'", file=sys.stderr)
        return 1

    json.dump(config, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
