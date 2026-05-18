# Guidance for AI Agents and Contributors

## PostmarketOS Base - DO NOT USE DEBIAN

**CRITICAL:** This project builds **PostmarketOS immutable** images for ARM64 devices (Lenovo ThinkPad X13s).

### ❌ DO NOT DO THIS:
```ini
# WRONG - Never use Debian as base!
[Distribution]
Distribution=debian
Release=trixie
```

### ✅ CORRECT:
```ini
[Distribution]
Distribution=postmarketos
```

### Why?
- Duranium is an **immutable postmarketOS** distribution, not Debian
- PostmarketOS provides device-specific support, mobile optimizations, and ARM64 tooling
- Debian Trixie was only attempted as a v20.2 mkosi workaround - **this is wrong**

### mkosi Version Requirements
- **Minimum**: mkosi with PostmarketOS support (pip3 install, not apt package)
- **Apt mkosi v20.2**: Does NOT support `Distribution=postmarketos` - do not use
- **pip3 mkosi**: Supports postmarketos - this is correct
- **GitHub Actions**: Use `pip3 install --upgrade mkosi` to get proper version

### Bluefin Integration
- Bluefin common OCI (`ghcr.io/projectbluefin/common:latest`) is layered on top via:
  - `system_files/` directory (copied by mkosi via `ExtraSearchPaths`)
  - `mkosi.finalize` script for post-build customizations
- This is **NOT** a replacement for the PostmarketOS base
- PostmarketOS + GNOME + Bluefin tools = correct stack

## Quick Reference

| Component | Source |
|-----------|--------|
| **Base OS** | PostmarketOS (via mkosi) |
| **Desktop** | GNOME Shell |
| **Tools/Branding** | Bluefin common OCI |
| **mkosi Version** | pip3 (not apt) |
| **Target Arch** | arm64 |
| **Target Device** | Lenovo ThinkPad X13s |

## If You Change mkosi.conf

1. **Always verify** `Distribution=postmarketos` is set
2. **Test locally** with: `mkosi --force build`
3. **Don't downgrade mkosi** to versions that don't support postmarketos
4. **Document any changes** to the build process
