#!/usr/bin/env python3

import sys
import subprocess
import os
import argparse
import time
from typing import List
from pmaports import resolve


def validate_and_extract_components(components):
    """Validate exactly one component of each type and extract names"""
    devices = []
    uis = []

    for component in components:
        if component.startswith('device-'):
            devices.append(component)
        elif component.startswith('ui-'):
            uis.append(component)
        else:
            print(f"ERROR: Unknown component type: {component}")
            sys.exit(1)

    # Validate exactly one of each type
    if len(devices) != 1:
        print(f"ERROR: Must specify exactly one device, got {len(devices)}: {devices}")
        sys.exit(1)

    if len(uis) != 1:
        print(f"ERROR: Must specify exactly one UI, got {len(uis)}: {uis}")
        sys.exit(1)

    device = devices[0]
    ui = uis[0]

    # Extract names from component strings (remove 'device-' and 'ui-' prefixes)
    device_name = device.replace('device-', '', 1)
    ui_name = ui.replace('ui-', '', 1)

    return device_name, ui_name


def build_image(components: List[str], extra_args: List[str]):
    start_time = time.time()

    device = next(p for p in components if p.startswith('device-'))
    ui = next(p for p in components if p.startswith('ui-'))

    try:
        device_name, ui_name = validate_and_extract_components(components)

        # Extract release from extra_args
        release = "edge"
        for arg in extra_args:
            if arg.startswith('--release='):
                release = arg.split('=', 1)[1]
                break

        image_id = f"{device_name}_{ui_name}_{release}"

        print(f"Device: {device_name}, UI: {ui_name}")
        print(f"ImageID: {image_id}")

        # Make sure the pmaports cache has the right branch checked out. The mkosi
        # sandbox has no network access, so the configure script uses --skip-fetch
        resolved = resolve(device_name, ui_name, release)

        if not os.path.exists("mkosi.version"):
            subprocess.run([os.environ.get('MKOSI', 'mkosi'), "bump"], check=True)

        # Call mkosi
        mkosi_cmd = [
            os.environ.get('MKOSI', 'mkosi'),
            "build",
            "--force",
            f"--architecture={resolved['Architecture']}",
            f"--image-id={image_id}",
            f"--environment=PMOS_DEVICE={device_name}",
            f"--environment=PMOS_VARIANT={ui_name}",
        ] + extra_args

        print(f"Executing: {' '.join(mkosi_cmd)}")

        subprocess.run(mkosi_cmd, check=True)

        duration = time.time() - start_time
        print(f"Built device: {device}, ui: {ui}, release: {release} in {duration}s")
        return

    except Exception as e:
        print(e)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Build a postmarketOS Duranium image using mkosi')
    parser.add_argument('components', nargs='*', help='Device and UI components')
    # Parse known args so we can pass through extra mkosi args
    args, extra_args = parser.parse_known_args()

    if len(args.components) < 2:
        print("Usage: build-image.py <device> <ui> [mkosi-args...]")
        print("Example: build-image.py device-pine64-pinephone ui-plasma-mobile --release=edge --profile=compressed")
        sys.exit(1)

    build_image(args.components, extra_args)

if __name__ == '__main__':
    main()
