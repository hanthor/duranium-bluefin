#!/usr/bin/env python3
import json
import glob
import os
import subprocess
import sys


def get_combos():
    raw = os.environ.get("COMBOS")
    if raw is None:
        print("error: COMBOS environment variable is not set", file=sys.stderr)
        sys.exit(1)

    try:
        combos = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"error: failed to parse COMBOS: {e}", file=sys.stderr)
        sys.exit(1)

    return combos


def run_mkosi_summary(args):
    cmd = ["mkosi", "--json", "summary"] + args

    print(f">> running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("error: mkosi summary failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"error: failed to parse mkosi summary output: {e}", file=sys.stderr)
        sys.exit(1)


def run_pmaports(device, ui, release):
    pmaports_py = os.path.join(os.path.dirname(__file__), "..", "scripts", "pmaports.py")
    cmd = [pmaports_py, "--device", device, "--ui", ui, "--release", release]

    print(f">> running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("error: pmaports.py failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"error: failed to parse pmaports.py output: {e}", file=sys.stderr)
        sys.exit(1)


def index_repo(repo_dir, arch=None):
    """Create an APKINDEX for all .apk files in repo_dir.

    Organizes packages into an arch subdirectory as apk expects.
    """
    if arch is None:
        result = subprocess.run(["apk", "--print-arch"], capture_output=True, text=True)
        arch = result.stdout.strip()

    arch_dir = os.path.join(repo_dir, arch)
    os.makedirs(arch_dir, exist_ok=True)

    # Move apks into arch subdir if they're in the top level
    for apk_file in glob.glob(os.path.join(repo_dir, "*.apk")):
        os.rename(apk_file, os.path.join(arch_dir, os.path.basename(apk_file)))

    apks = glob.glob(os.path.join(arch_dir, "*.apk"))
    if not apks:
        return

    # NOTE: allow-untrusted because these packages may have been built by CI
    # in a previous job, using a throwaway signing key
    apks = [os.path.basename(f) for f in glob.glob(os.path.join(arch_dir, "*.apk"))]
    cmd = ["apk", "index",
           "--allow-untrusted",
           "--rewrite-arch", arch,
           "-o", "APKINDEX.tar.gz"] + apks

    print(f">> indexing {len(apks)} packages in {repo_dir}")
    result = subprocess.run(cmd, cwd=arch_dir)
    if result.returncode != 0:
        print("error: apk index failed", file=sys.stderr)
        sys.exit(1)


def resolve_install_if(packages, extra_repos=None):
    # NOTE: this expects a fully initialized apk root at ./simroot!
    cmd = ["apk", "add", "--simulate", "--root", "simroot"]

    for repo in (extra_repos or []):
        cmd += ["--repository", repo, "--allow-untrusted"]

    cmd += sorted(packages)

    print(f">> resolving 'install_if' for package set ({len(packages)} packages)")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("error: apk add --simulate failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        print("Is there an initialized apk root at './simroot' ?")
        sys.exit(1)

    resolved = set()
    # FIXME: Parsing package names from apk output is a total hack. We
    # should not rely on the stability of apk's output.
    for line in result.stderr.splitlines() + result.stdout.splitlines():
        if ") Installing " in line:
            parts = line.split("Installing ", 1)
            if len(parts) == 2:
                resolved.add(parts[1].split(" (")[0])

    if not resolved:
        print("error: failed to parse any packages from apk simulate output",
              file=sys.stderr)
        sys.exit(1)

    return resolved


def fetch_packages(packages, output_dir, extra_repos=None):
    os.makedirs(output_dir, exist_ok=True)

    cmd = ["apk", "fetch", "--root", "simroot", "--output", output_dir]

    for repo in (extra_repos or []):
        cmd += ["--repository", repo, "--allow-untrusted"]

    cmd += sorted(packages)

    print(f">> fetching {len(packages)} packages into directory '{output_dir}'")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("error: apk fetch failed", file=sys.stderr)
        sys.exit(1)


def main():
    combos = get_combos()

    custom_repo_dir = "custom-repo"
    has_custom_repo = os.path.isdir(custom_repo_dir)
    arch = os.environ.get("ARCH")

    if has_custom_repo:
        index_repo(custom_repo_dir, arch=arch)

    extra_repos = [custom_repo_dir] if has_custom_repo else []

    resolved = set()
    for combo in combos.get("image", []):
        device = combo["device"]
        ui = combo["ui"]
        release = combo["release"]

        # mkosi summary gets static pkg lists from .conf files
        summary = run_mkosi_summary([
            f"--environment=PMOS_DEVICE={device}",
            f"--environment=PMOS_VARIANT={ui}",
            f"--release={release}",
        ])

        # pmaports.py gets dynamic pkg lists from pmaports + duranium.toml overrides
        pmaports_data = run_pmaports(device, ui, release)

        # Resolve base/main packages (dynamic from pmaports + static from summary)
        # separately from initrd packages, to avoid provider conflicts between
        # FDE and non-FDE unlocker packages.
        base_packages = set(pmaports_data.get("Packages", []))
        initrd_packages = set(pmaports_data.get("InitrdPackages", []))

        extension_images = []
        for image in summary.get("Images", []):
            image_packages = set(image.get("Packages", []))
            if not image_packages:
                continue
            if image.get("Image") == "default-initrd":
                initrd_packages.update(image_packages)
            elif image.get("Format") == "directory" and image.get("Image") != "base":
                # Tier 1 extension directory images (e.g. dev-tools).
                # Packages are resolved unioned with base, since extensions
                # are built against the full base image.
                extension_images.append(image_packages)
            else:
                base_packages.update(image_packages)

        resolved.update(resolve_install_if(base_packages, extra_repos=extra_repos))
        resolved.update(resolve_install_if(initrd_packages, extra_repos=extra_repos))
        for ext_packages in extension_images:
            resolved.update(resolve_install_if(
                ext_packages, extra_repos=extra_repos,
            ))

    if not resolved:
        print("error: no packages resolved", file=sys.stderr)
        sys.exit(1)

    print(f">> packages resolved: {len(resolved)}")

    fetch_packages(resolved, output_dir="packages", extra_repos=extra_repos)

    index_repo("packages", arch=arch)

    print(">> done")


if __name__ == "__main__":
    main()
