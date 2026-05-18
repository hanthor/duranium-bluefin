#!/usr/bin/python3

# SPDX-License-Identifier: GPL-3.0-or-later
#
# Portions of APKBUILD parsing and pmaports repo logic adapted from:
#   install-recommends.py
#   Copyright 2025 Alexey Minnekhanov <alexeymin@postmarketos.org>
#   SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Optional

PMAPORTS_CLONE_URL = "https://gitlab.postmarketos.org/postmarketOS/pmaports.git"
CACHE_PERIOD = 1800  # seconds

# map pmaports/Alpine arch names to mkosi Architecture= values
ARCH_MAP = {
    "aarch64": "arm64",
    "armhf": "arm",
    "armv7": "arm",
    "loongarch64": "loongarch64",
    "ppc64le": "ppc64-le",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "x86": "x86",
    "x86_64": "x86-64",
}

# Cache of {pkgname: apkbuild_path}, re-using is fine since this script is a
# one-shot thing
apkbuilds: dict[str, str] = {}


def needs_fetch(pmaports_dir: Path) -> bool:
    # packed-refs is touched by git on every fetch, so its mtime is probably
    # a free staleness indicator
    fetch_head = pmaports_dir / ".git" / "packed-refs"
    if not fetch_head.exists():
        return True
    return time.time() - fetch_head.stat().st_mtime > CACHE_PERIOD


def branch_exists_locally(pmaports_dir: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(pmaports_dir), "branch", "-r", "--list", f"origin/{branch}"],
        capture_output=True,
        check=True,
    )
    return bool(result.stdout.strip())


def prepare_pmaports(release: str, skip_fetch: bool = False) -> Path:
    # stick it at the top of the duranium repo
    # FIXME: make configurable?
    pmaports_dir = Path(__file__).parent.parent / "pmaports"

    if skip_fetch:
        if not pmaports_dir.exists():
            raise RuntimeError(f"pmaports not found at {pmaports_dir} and --skip-fetch was given")
        return pmaports_dir

    pmaports_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = "main" if release == "edge" else release

    if not pmaports_dir.exists():
        # first run, clone with only the required branch
        subprocess.run(
            ["git", "-C", str(pmaports_dir.parent), "clone",
             "--depth=1", "--no-tags", "--single-branch", "--branch", branch,
             PMAPORTS_CLONE_URL],
            check=True, capture_output=True,
        )
        return pmaports_dir

    if not branch_exists_locally(pmaports_dir, branch):
        # branch not fetched yet (e.g. repo cloned for edge, now building a stable release).
        # The explicit refspec is required to create the remote tracking ref,
        # because a bare branch name only fetches to FETCH_HEAD.
        subprocess.run(
            ["git", "-C", str(pmaports_dir), "fetch",
             "--depth=1", "--no-tags", "--update-shallow", "origin",
             f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(pmaports_dir), "switch",
             "--create", branch, f"origin/{branch}"],
            check=True, capture_output=True,
        )
    else:
        # branch already exists locally, just switch to it
        subprocess.run(
            ["git", "-C", str(pmaports_dir), "switch", branch],
            check=True, capture_output=True,
        )

    if needs_fetch(pmaports_dir):
        # repo is stale, fetch and hard reset to ensure we're in sync with upstream
        subprocess.run(
            ["git", "-C", str(pmaports_dir), "fetch", "--no-auto-gc", "origin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(pmaports_dir), "reset", "--hard", f"origin/{branch}"],
            check=True, capture_output=True,
        )

    return pmaports_dir


def build_apkbuild_index(pmaports_dir: Path) -> None:
    global apkbuilds
    if apkbuilds:
        return
    for apkbuild in glob.iglob(f"{pmaports_dir}/**/*/APKBUILD", recursive=True):
        pkgname = os.path.basename(os.path.dirname(apkbuild))
        if pkgname not in apkbuilds:
            apkbuilds[pkgname] = apkbuild


def parse_recommends(apkbuild_path: str) -> tuple[list[str], list[str]]:
    result = subprocess.run(
        ["sh", "-c", f". '{apkbuild_path}' && echo $_pmb_recommends && echo $depends"],
        capture_output=True,
        check=True,
    )
    lines = result.stdout.decode("utf-8").split("\n")
    recommends = lines[0].split() if lines[0].strip() else []
    # filter out conflict markers (e.g. !gnome-shell-mobile)
    depends = [d for d in lines[1].split() if not d.startswith("!")]
    return recommends, depends


def collect_recommends(pkgname: str, visited: set[str]) -> list[str]:
    if pkgname in visited:
        return []
    visited.add(pkgname)

    if pkgname not in apkbuilds:
        return []

    recommends, depends = parse_recommends(apkbuilds[pkgname])
    result = list(recommends)
    for pkg in recommends:
        result.extend(collect_recommends(pkg, visited))
    for pkg in depends:
        result.extend(collect_recommends(pkg, visited))
    return result


def pick_kernel_subpackage(apkbuild_path: str, device: str) -> Optional[str]:
    # This mirrors pmb's default behavior when picking a kernel for a device
    # package that supports multiple kernel options by picking the first
    # device-{device}-kernel-* subpackage.
    result = subprocess.run(
        ["sh", "-c", f". '{apkbuild_path}' && echo $subpackages"],
        capture_output=True,
        check=True,
    )
    prefix = f"device-{device}-kernel-"
    for entry in result.stdout.decode("utf-8").split():
        # kernel subpackage entries are almost always name:function
        name = entry.split(":")[0]
        if name.startswith(prefix):
            return name
    return None


def resolve(device: str, ui: str, release: str, skip_fetch: bool = False) -> dict:
    pmaports_dir = prepare_pmaports(release, skip_fetch=skip_fetch)
    build_apkbuild_index(pmaports_dir)

    # deviceinfo
    deviceinfo_path: Optional[Path] = None
    for subdir in ("main", "community", "testing"):
        candidate = pmaports_dir / "device" / subdir / f"device-{device}" / "deviceinfo"
        if candidate.exists():
            deviceinfo_path = candidate
            break
    if deviceinfo_path is None:
        raise RuntimeError(f"deviceinfo not found for device '{device}'")

    with deviceinfo_path.open("rb") as f:
        deviceinfo = tomllib.load(f)

    raw_arch = deviceinfo.get("deviceinfo_arch", "")
    if raw_arch not in ARCH_MAP:
        raise RuntimeError(f"Unknown pmaports arch '{raw_arch}', add it to ARCH_MAP")
    mkosi_arch = ARCH_MAP[raw_arch]

    # packages / pmb_recommends
    device_pkg = f"device-{device}"
    ui_pkg = f"postmarketos-ui-{ui}"

    visited: set[str] = set()
    device_apkbuild = deviceinfo_path.parent / "APKBUILD"
    packages = [device_pkg] + collect_recommends(device_pkg, visited)
    if kernel_pkg := pick_kernel_subpackage(str(device_apkbuild), device):
        packages.append(kernel_pkg)
    packages += [ui_pkg] + collect_recommends(ui_pkg, visited)

    # unl0kr-fbforcerefresh in a device's depends signals that the hardware
    # needs this force fb refresh workaround. pmaports has no mechanism to
    # express that a package belongs specifically in the initramfs image or
    # the base image so we infer it here. unl0kr-fbforcerefresh goes into the
    # initramfs (for the unlock screen), and f0rmz-fbforcerefresh goes into the
    # base image (for first boot)
    initrd_packages = []
    _, device_depends = parse_recommends(str(device_apkbuild))
    if "unl0kr-fbforcerefresh" in device_depends:
        packages.append("f0rmz-fbforcerefresh")
        initrd_packages.append("unl0kr-fbforcerefresh")

    packages = list(dict.fromkeys(packages))

    # create mkosi-compatible json config fragment
    out: dict = {"Architecture": mkosi_arch}

    sector_size = deviceinfo.get("deviceinfo_rootfs_image_sector_size")
    if sector_size is not None:
        out["SectorSize"] = int(sector_size)

    out["Packages"] = packages

    if initrd_packages:
        out["InitrdPackages"] = initrd_packages

    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve pmaports metadata into mkosi config json"
    )
    parser.add_argument("--device", required=True, help="Device name (e.g. oneplus-enchilada)")
    parser.add_argument("--ui", required=True, help="UI name (e.g. gnome)")
    parser.add_argument("--release", required=True, help="Alpine release (e.g. edge, v25.12)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip git fetch/reset, assume pmaports checkout is already up to date")
    args = parser.parse_args()

    result = resolve(args.device, args.ui, args.release, skip_fetch=args.skip_fetch)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
