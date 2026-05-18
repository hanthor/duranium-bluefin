#!/usr/bin/python3

# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import time
import unittest.mock
from pathlib import Path

import pytest

# pmaports.py lives one directory up from this test file
sys.path.insert(0, str(Path(__file__).parent.parent))
import pmaports as pm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_apkbuild_index():
    """Clear the module-level apkbuild cache before every test."""
    pm.apkbuilds.clear()
    yield
    pm.apkbuilds.clear()


def make_apkbuild(pkg_dir: Path, *, pkgname: str,
                  depends: str = "", recommends: str = "", subpackages: str = "") -> None:
    """Write a minimal but shell-sourceable APKBUILD into pkg_dir."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "APKBUILD").write_text(
        f'pkgname="{pkgname}"\n'
        f'depends="{depends}"\n'
        f'_pmb_recommends="{recommends}"\n'
        f'subpackages="{subpackages}"\n'
    )


def make_deviceinfo(device_dir: Path, *, arch: str = "aarch64", sector_size: str = None) -> None:
    device_dir.mkdir(parents=True, exist_ok=True)
    content = f'deviceinfo_arch = "{arch}"\n'
    if sector_size is not None:
        content += f'deviceinfo_rootfs_image_sector_size = "{sector_size}"\n'
    (device_dir / "deviceinfo").write_text(content)


def make_fake_pmaports(tmp_path: Path, *, device: str, ui: str,
                        device_arch: str = "aarch64",
                        device_depends: str = "",
                        device_recommends: str = "",
                        device_subpackages: str = "",
                        ui_recommends: str = "",
                        sector_size: str = None) -> Path:
    """
    Build a minimal fake pmaports tree and return its root.

    Directory layout mirrors what build_apkbuild_index() expects:
      <root>/device/main/device-<device>/APKBUILD
      <root>/device/main/device-<device>/deviceinfo
      <root>/main/postmarketos-ui-<ui>/APKBUILD
    """
    device_dir = tmp_path / "device" / "main" / f"device-{device}"
    make_deviceinfo(device_dir, arch=device_arch, sector_size=sector_size)
    make_apkbuild(device_dir, pkgname=f"device-{device}",
                  depends=device_depends, recommends=device_recommends,
                  subpackages=device_subpackages)

    ui_dir = tmp_path / "main" / f"postmarketos-ui-{ui}"
    make_apkbuild(ui_dir, pkgname=f"postmarketos-ui-{ui}", recommends=ui_recommends)

    return tmp_path


# ---------------------------------------------------------------------------
# needs_fetch
# ---------------------------------------------------------------------------

def test_needs_fetch_missing_packed_refs(tmp_path):
    # no .git/packed-refs at all -> always fetch
    assert pm.needs_fetch(tmp_path) is True


def test_needs_fetch_fresh(tmp_path):
    packed_refs = tmp_path / ".git" / "packed-refs"
    packed_refs.parent.mkdir()
    packed_refs.touch()
    assert pm.needs_fetch(tmp_path) is False


def test_needs_fetch_stale(tmp_path):
    packed_refs = tmp_path / ".git" / "packed-refs"
    packed_refs.parent.mkdir()
    packed_refs.touch()
    old_time = time.time() - pm.CACHE_PERIOD - 1
    os.utime(packed_refs, (old_time, old_time))
    assert pm.needs_fetch(tmp_path) is True


# ---------------------------------------------------------------------------
# parse_recommends
# ---------------------------------------------------------------------------

def test_parse_recommends_basic(tmp_path):
    make_apkbuild(tmp_path, pkgname="test", depends="foo bar", recommends="baz qux")
    recommends, depends = pm.parse_recommends(str(tmp_path / "APKBUILD"))
    assert recommends == ["baz", "qux"]
    assert depends == ["foo", "bar"]


def test_parse_recommends_empty(tmp_path):
    make_apkbuild(tmp_path, pkgname="test")
    recommends, depends = pm.parse_recommends(str(tmp_path / "APKBUILD"))
    assert recommends == []
    assert depends == []


def test_parse_recommends_strips_conflict_markers(tmp_path):
    # packages prefixed with ! are conflict markers, not real deps
    make_apkbuild(tmp_path, pkgname="test", depends="foo !bar baz")
    _, depends = pm.parse_recommends(str(tmp_path / "APKBUILD"))
    assert depends == ["foo", "baz"]
    assert "!bar" not in depends


# ---------------------------------------------------------------------------
# pick_kernel_subpackage
# ---------------------------------------------------------------------------

def test_pick_kernel_subpackage_returns_first(tmp_path):
    subpkgs = "device-testphone-kernel-mainline:func device-testphone-kernel-downstream:func"
    make_apkbuild(tmp_path, pkgname="device-testphone", subpackages=subpkgs)
    result = pm.pick_kernel_subpackage(str(tmp_path / "APKBUILD"), "testphone")
    assert result == "device-testphone-kernel-mainline"


def test_pick_kernel_subpackage_none(tmp_path):
    make_apkbuild(tmp_path, pkgname="device-testphone")
    result = pm.pick_kernel_subpackage(str(tmp_path / "APKBUILD"), "testphone")
    assert result is None


# ---------------------------------------------------------------------------
# collect_recommends
# ---------------------------------------------------------------------------

def _inject_apkbuilds(tmp_path: Path, packages: dict[str, dict]) -> None:
    """
    Build fake APKBUILDs and inject them into the module-level index directly,
    bypassing the filesystem glob in build_apkbuild_index().
    """
    for pkgname, kwargs in packages.items():
        pkg_dir = tmp_path / pkgname
        make_apkbuild(pkg_dir, pkgname=pkgname, **kwargs)
        pm.apkbuilds[pkgname] = str(pkg_dir / "APKBUILD")


def test_collect_recommends_transitive(tmp_path):
    # pkg-a recommends pkg-b which recommends pkg-c
    _inject_apkbuilds(tmp_path, {
        "pkg-a": {"recommends": "pkg-b"},
        "pkg-b": {"recommends": "pkg-c"},
        "pkg-c": {},
    })
    result = pm.collect_recommends("pkg-a", set())
    assert "pkg-b" in result
    assert "pkg-c" in result


def test_collect_recommends_cycle(tmp_path):
    # must not recurse forever if packages recommend each other
    _inject_apkbuilds(tmp_path, {
        "pkg-a": {"recommends": "pkg-b"},
        "pkg-b": {"recommends": "pkg-a"},
    })
    result = pm.collect_recommends("pkg-a", set())
    assert "pkg-b" in result


def test_collect_recommends_unknown_package(tmp_path):
    # referencing a package not in the index should not crash
    _inject_apkbuilds(tmp_path, {
        "pkg-a": {"recommends": "nonexistent-pkg"},
    })
    result = pm.collect_recommends("pkg-a", set())
    assert "nonexistent-pkg" in result


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_basic(mock_prepare, tmp_path):
    pmaports_dir = make_fake_pmaports(
        tmp_path, device="testphone", ui="testui",
        device_arch="aarch64",
        device_recommends="some-driver",
        device_subpackages="device-testphone-kernel-mainline:func",
    )
    mock_prepare.return_value = pmaports_dir

    result = pm.resolve("testphone", "testui", "edge")

    assert result["Architecture"] == "arm64"
    assert "device-testphone" in result["Packages"]
    assert "device-testphone-kernel-mainline" in result["Packages"]
    assert "postmarketos-ui-testui" in result["Packages"]
    assert "some-driver" in result["Packages"]
    assert "SectorSize" not in result
    assert "InitrdPackages" not in result


@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_sector_size(mock_prepare, tmp_path):
    pmaports_dir = make_fake_pmaports(
        tmp_path, device="testphone", ui="testui", sector_size="4096"
    )
    mock_prepare.return_value = pmaports_dir

    result = pm.resolve("testphone", "testui", "edge")
    assert result["SectorSize"] == 4096


@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_fbforcerefresh(mock_prepare, tmp_path):
    # device with unl0kr-fbforcerefresh in depends -> both fb packages should appear
    pmaports_dir = make_fake_pmaports(
        tmp_path, device="testphone", ui="testui",
        device_depends="unl0kr-fbforcerefresh",
    )
    mock_prepare.return_value = pmaports_dir

    result = pm.resolve("testphone", "testui", "edge")
    assert "f0rmz-fbforcerefresh" in result["Packages"]
    assert result["InitrdPackages"] == ["unl0kr-fbforcerefresh"]


@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_no_duplicate_packages(mock_prepare, tmp_path):
    # a package appearing in both device and ui recommends should appear only once
    pmaports_dir = make_fake_pmaports(
        tmp_path, device="testphone", ui="testui",
        device_recommends="shared-pkg",
        ui_recommends="shared-pkg",
    )
    mock_prepare.return_value = pmaports_dir

    result = pm.resolve("testphone", "testui", "edge")
    assert result["Packages"].count("shared-pkg") == 1


@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_unknown_arch(mock_prepare, tmp_path):
    pmaports_dir = make_fake_pmaports(
        tmp_path, device="testphone", ui="testui", device_arch="mips"
    )
    mock_prepare.return_value = pmaports_dir

    with pytest.raises(RuntimeError, match="Unknown pmaports arch"):
        pm.resolve("testphone", "testui", "edge")


@unittest.mock.patch("pmaports.prepare_pmaports")
def test_resolve_missing_device(mock_prepare, tmp_path):
    # pmaports tree with no device-testphone directory at all
    mock_prepare.return_value = tmp_path

    with pytest.raises(RuntimeError, match="deviceinfo not found"):
        pm.resolve("testphone", "testui", "edge")
