# Overview

This document outlines the design of Duranium, an immutable, image-based variant of postmarketOS following the concepts from systemd's "Fitting Everything Together" approach. Duranium requires systemd.

Duranium uses mkosi for image building with systemd tooling for A/B updates (systemd-sysupdate), partition management (systemd-repart), and slot boot selection and automatic fallback on failure (systemd-boot). EFI is chosen as the boot interface because it provides a simple, easy way to configure the OS for booting and integrates seamlessly with the systemd tooling being used. UKI (Unified Kernel Images) enable future secure boot capabilities and simplify image-based updates by bundling kernel, initramfs, cmdline, and DTBs as versioned artifacts. For ARM devices lacking native EFI support (e.g. Android phones), u-boot's implemetation of EFI is expected to bridge this gap.

# Features

* **Immutable, verified /usr**: All OS resources live in a read-only /usr partition, verified at boot with dm-verity. Updates replace the entire partition image atomically.

* **A/B updates with automatic rollback**: Two /usr slots allow atomic updates via systemd-sysupdate. If a new image fails to boot, systemd-boot automatically falls back to the previous slot.

* **Factory reset**: Wipe the rootfs and re-enter first boot setup. Triggered from an authenticated session via an EFI variable.

* **Encrypted by default**: Rootfs is always created on a LUKS volume. Users can set a custom passphrase during first boot or later.

* **First-boot provisioning**: On first boot, a setup wizard handles user account creation and optional FDE passphrase configuration.

* **System extensions**: Optional add-ons that extend `/usr` at runtime via systemd-sysext. Extensions are versioned, dm-verity protected, and locked to the OS image version they were built against. They are updated via sysupdate alongside the main OS image.

# System Architecture

## Boot Overview

This design uses Type 2 booting with UKI, as defined in the UAPI Boot Loader Spec.

Type 1 Boot, where the kernel, initramfs, dtbs are separate files in the ESP, make image creation with mkosi and image-based updates with sysupdate very difficult. More specifically:

* **No ESP Update Mechanism**: mkosi uses kernel version paths (`/postmarketOS/6.15.8-0-stable/`) not IMAGE_VERSION. This means that boot artifacts (kernel) are not coupled with usr partition versions for sysupdate. In other words, it means that /usr/lib/modules may not match the booted kernel, and this is bad.

* **Verity Hash Timing**: `usrhash=` must be passed on the kernel cmdline, the value of this is only available **after** the usr partition is created and finished. For type 2, mkosi automatically creates and injects this into the kernel cmdline in the UKI, but for type 1 there is no point where this can be done. `mkosi.finalize` runs before verity calculation, and `mkosi.postoutput` runs after the ESP partition is finalized/exported, so there's no scriptable point where the usrhash can be injected into the cmdline manually. Using mkosi's kernel-install mechanism isn't possible either since it writes to paths using kernel versions, and not IMAGE_VERSION. Given the previously mentioned issue, when sysupdate updates the usr partition, the loader config in the ESP will have a stale `usrhash=` value. Stale `usrhash=` values in the kernel cmdline break dm-verity.

* **Complex sysupdate.transfer config**: Trying to create an ESP layout with versioned Type 1 artifacts and a sysupdate config that can manage them all (so they can get updated) is difficult and fragile.

* **No SecureBoot Support**: While SecureBoot is not a requirement for an immutable pmOS, choosing a boot implementation that doesn't really support it will make supporting it more difficult in the future.

### Why UKI

UKI resolves/avoids all Type 1 limitations when using mkosi for image creation and sysupdate for image-based updates:

* **Automatic Verity**: mkosi injects `usrhash=` into UKI cmdline during build automatically, no need to patch mkosi to do this

* **Proper Versioning**: UKI files use versioning compatible with other sysupdated artifacts, with boot counting (e.g. `oneplus-enchilada_gnome-mobile_edge_26012901+3-0.efi`), the same versioning is used to couple UKI with the correct usr partition for module loading.

* **Easy sysupdate.transfer config**: Single .efi file contains kernel + initramfs + cmdline + DTB(s), updated atomically by sysupdate, and the sysupdate.transfer configuration to handle this is very simple and straightforward.

* **SecureBoot support**: Entire UKI signed as single unit

### Supported Device Boot Scenarios

DTB loading is handled by embedding devicetrees as `.dtbauto` sections in the UKI, where systemd-stub selects the correct one at boot by matching the `compatible` string from the EFI configuration table. mkosi handles the DTB embedding during image build.

* **u-boot + explicit DTB**: DTB from deviceinfo is embedded in UKI `.dtbauto` and copied to `/dtbs/` in the ESP for u-boot

* **u-boot + auto-detect**: u-boot provides DTB from internal logic, so embed all `/dtbs/` in UKI

* **WoA + explicit DTB**: DTB from deviceinfo is embedded in the UKI

* **WoA + auto-detect**: embed all `/dtbs/` in UKI

* **ACPI devices**: No DTB sections in UKI, normal ACPI boot

**Note about embedding many devicetrees in a UKI**: Embedding all ~1.6K dtbs shipped in the postmarketos-linux-next kernel and booting it on a Thinkpad X13s resulted in no perceived delay in booting while the stub detected/loaded the correct dtb for this device from the large selection of embedded dtbs.

## Versioning

A lot consideration was taken to choose a versioning scheme for images, because we do not want to accidentally configure sysupdate to flash incompatible or unexpected images to devices during update, and there are limitations to how long partitions labels can be. The GPT spec gives a limit of 34 characters, but there have been cases where some bootloaders only support much less (e.g. 24 characters.)

os-release is used to set a variety of parameters for images:

* `IMAGE_ID`: Contains the device name, e.g. `qemu-aarch64`, `apple-mac-aarch64`, `generic-x86_64`, `lenovo-21bx`

* `VARIANT_ID`: Contains the UI, e.g. `gnome`, `plasma-mobile`, `console`, `cosmic`

* `VERSION_ID`: Contains the release, e.g. `edge`, `v25.06`

* `IMAGE_VERSION`: Contains a build date code and increment, e.g. `25110402`

* `CONFEXT_LEVEL`: Set to `IMAGE_VERSION`. Used by systemd-confext to verify that confext images match the running OS version before merging.

* `SYSEXT_LEVEL`: Set to `IMAGE_VERSION`. Used by systemd-sysext to verify that sysext images match the running OS version before merging.

* **mkosi**: Uses ImageId (from build-image.py) for output filenames, but `IMAGE_ID` in `os-release` and `initrd-release`, used by repart for partition labels, sysupdate for matching, and factory reset, are overwritten in `mkosi.finalize` to only specify the device name.

* **sysupdate**: Uses these to identify when updates are available. Sysupdate uses GPT partition names to determine which partition to preserve and which one to install an image update to. Sysupdate also requires that newer image updates have an version that's higher than the currently active image. Sysupdate uses all of these variables from os-release to search for updates. Modifying `VARIANT_ID` and/or `VERSION_ID` in `/etc/os-release` after image installation and running sysupdate allows one to switch to a different UI or release, respectively.

* **repart**: mkosi calls repart when creating a full disk image for provisioning a system, and embeds the `IMAGE_ID` and `IMAGE_VERSION` in the active slot partition name.

* **GPT Partition Name**: This field is limited to a maximum of 34 characters, so to guarantee this will fix and ensure it's static, the partition name is prefixed with `duranium_IMAGE_ID`. An additional 3 characters is reserved for indicating the partition type as a suffix. The partition names need to be unique on the disk.

Given all of these requirements, the following format is used for partition labels:

`duranium_{IMAGE_VERSION}_{partition type suffix}` = duranium(8) + _(1) + version(8) + _(1) + suffix(3) = 21 chars

A real world example of a partition label in this format might look like: `duranium_25110402_vty` for the /usr verity partition on a pine64 pinephone.


## Partition Layout

**Initial shipped image:**

1. ESP (EFI System Partition) with systemd-boot, UKI, DTBs (for u-boot compatibility, separate from UKI-embedded DTBs)

2. /usr partition (version A) - immutable, labeled with image version

3. Verity partition for /usr (version A)

**Created on first boot by systemd-repart:**

1. /usr partition (version B) - initially empty, labeled `_empty`

2. Verity partition for /usr (version B)

4. Root filesystem - encrypted with LUKS (blank passphrase by default)

### ESP Layout for UKI

```
/boot/
├── efi/                                               # installed once, not managed by A/B updates
│   ├── boot/
│   │   └── bootaa64.efi                               # systemd-boot
│   └── systemd/
├── dtbs/                                              # Device detection DTBs (optional, unversioned)
│   ├── qcom/
│   │   ├── sc8280xp-lenovo-thinkpad-x13s.dtb
│   │   └── ...
│   └── ...
└── EFI/Linux/
    ├── lenovo-21bx_phosh_edge_25071501.efi            # UKI (dtb(s) in dtbauto sections)
    └── lenovo-21bx_phosh_edge_25071801.efi+3-0.efi    # Next version with boot counting
```

## Image Building

Images are built from a base subimage containing packages common to all Duranium installations. Device and UI packages are resolved at build time from pmaports, the postmarketOS package repository. A wrapper script (`build-image.py`) is used to invoke mkosi, users specify a device, UI, and release, and the wrapper handles passing these to mkosi as `PMOS_DEVICE`, `PMOS_VARIANT`, and `--release=`. mkosi makes these available to `mkosi.configure` scripts, which perform dynamic package and configuration resolution for each image.


### Resolving Device and UI Packages

`pmaports.py` (under `scripts/`) is the central tool for resolving device and UI packages/config from pmaports. It clones and maintains a `pmaports/` checkout at the repo root, which mkosi can access as a "BuildSource". Given a device, UI, and release, it:

* Checks out the correct pmaports branch

* Parses `deviceinfo` to extract architecture and sector size

* Recursively resolves `pmb_recommends` for device and UI packages. This logic is somewhat duplicated from `postmarketos-install-recommends`, but is much more flexible and it means avoiding a dependency on this tool

* For multi-kernel devices, selects the default kernel subpackage (the first `device-{device}-kernel-*` entry in the APKBUILD's subpackages list)

* Detects `unl0kr-fbforcerefresh` in a device's depends and adds `f0rmz-fbforcerefresh` to base image packages and `unl0kr-fbforcerefresh` to initrd packages

* Outputs a mkosi-compatible JSON config fragment

`mkosi.configure` scripts run `pmaports.py --skip-fetch` since the mkosi sandbox has no network access. The three `mkosi.configure` entries (top-level, `mkosi.images/base/`, `mkosi.initrd.conf/`) are symlinks to a single `scripts/mkosi-configure.py` that uses the `Image` field from the stdin config to emit image-specific config to mkosi.

### Build Process

1. `build-image.py` is invoked with a device, UI, and release (e.g. `build-image.py device-oneplus-enchilada ui-gnome --release=edge`)

2. `build-image.py` pre-runs `pmaports.py` to ensure the `pmaports/` checkout exists and has the correct branch. This is required because `mkosi.configure` scripts run without network access.

3. `build-image.py` constructs the `ImageId` (`{device}_{ui}_{release}`), then invokes mkosi with `--image-id=`, `--environment=PMOS_DEVICE=`, `--environment=PMOS_VARIANT=`, and `--release=`.

4. mkosi builds the `base` subimage first. `mkosi.images/base/mkosi.configure` runs `pmaports.py --skip-fetch` to resolve device and UI packages, and sets `Packages=` mkosi config dynamically.

5. mkosi builds the default-initrd subimage. `mkosi.initrd.conf/mkosi.configure` runs `pmaports.py --skip-fetch` and sets the initrd's `Packages=` to pmaports' `InitrdPackages` list (device-specific initrd packages like `unl0kr-fbforcerefresh`). The reason for this is that `InitrdPackages=` set on parent images via configure scripts does not propagate to the default-initrd, only statically-declared `InitrdPackages=` in `mkosi.conf` seem to make it in.

6. mkosi builds the main image on top of `base`. The top-level `mkosi.configure` runs `pmaports.py --skip-fetch` to set `Architecture=` and `SectorSize=`.

7. `mkosi.finalize` modifies `os-release` to set `VARIANT_ID=$PMOS_VARIANT`, `IMAGE_ID=$PMOS_DEVICE`, `CONFEXT_LEVEL=$IMAGE_VERSION`, and `SYSEXT_LEVEL=$IMAGE_VERSION`. It also builds the base confext image from /etc.

8. mkosi exports the main image as a full disk image. Partition images (usr, verity, ESP) are split out as separate files. If `--profile=compressed` is used, the artifacts are compressed and a SHA256SUMS manifest is generated.

### Image and Config Layout

```
mkosi.version
qemu-aarch64_console_edge/
├── qemu-aarch64_console_edge_25081111.efi
├── qemu-aarch64_console_edge_25081111.zst
├── qemu-aarch64_console_edge_25081111.usr-arm64-verity.4c62010a14dda6d767e3108092367651.raw.zst
├── qemu-aarch64_console_edge_25081111.usr-arm64.77415c80aa85f09c68ab25fba2481fa2.raw.zst
├── qemu-aarch64_console_edge_25081111.dev-tools-sysext-arm64.raw.zst
├── qemu-aarch64_console_edge_25081111.dev-tools-confext-arm64.raw.zst
├── qemu-aarch64_console_edge_25082001.efi
├── qemu-aarch64_console_edge_25082001.zst
├── qemu-aarch64_console_edge_25082001.usr-arm64-verity.5d8faa5c7560e499080bd6993ed67359.raw.zst
├── qemu-aarch64_console_edge_25082001.usr-arm64.60c62c8db2a1c111ad9d53fe69a74074.raw.zst
├── qemu-aarch64_console_edge_25082001.dev-tools-sysext-arm64.raw.zst
├── qemu-aarch64_console_edge_25082001.dev-tools-confext-arm64.raw.zst
├── SHA256SUMS
├── SHA256SUMS.gpg
pine64-pinephonepro_phosh_edge/
├── pine64-pinephonepro_phosh_edge_25081111.efi
├── pine64-pinephonepro_phosh_edge_25081111.zst
├── pine64-pinephonepro_phosh_edge_25081111.usr-arm64-verity.e07910a06a086c83ba41827aa00b26ed.raw.zst
├── pine64-pinephonepro_phosh_edge_25081111.usr-arm64.34c5f9b2cd3e1504604d186a190cbaaf.raw.zst
├── pine64-pinephonepro_phosh_edge_25081111.dev-tools-sysext-arm64.raw.zst
├── pine64-pinephonepro_phosh_edge_25081111.dev-tools-confext-arm64.raw.zst
├── SHA256SUMS
├── SHA256SUMS.gpg
```

### Deploying on HTTP server

The postmarketOS infra is currently hosting images at https://duranium.postmarketos.org. The information below is included in case this needs to change in the future. For now, images are being built and pushed there automatically by gitlab CI.

sysupdate is configured to query/fetch image updates from a remote HTTP server. Images should be laid out on the server under directories named after the image's `ImageId`, and each `ImageId` directory should contain a file `SHA256SUMS` that serves as a manifest of available images for sysupdate along with a checksum of the image files. This manifest should be signed (`SHA256SUMS.gpg`), and the public key included in images created by mkosi so that they can be verified at runtime.

Image files (except the UKI) will be compressed to save space on the server and reduce download size.

An example layout might look something like this:

```
qemu-aarch64_console_edge/
├── qemu-aarch64_console_edge_26040801.efi
├── qemu-aarch64_console_edge_26040801.usr-arm64-verity.raw.zst
├── qemu-aarch64_console_edge_26040801.usr-arm64.raw.zst
├── qemu-aarch64_console_edge_26040801.dev-tools-sysext-arm64.raw.zst
├── qemu-aarch64_console_edge_26040801.dev-tools-confext-arm64.raw.zst
├── SHA256SUMS
├── SHA256SUMS.gpg
pine64-pinephonepro_phosh_edge/
├── pine64-pinephonepro_phosh_edge_26040801.efi
├── pine64-pinephonepro_phosh_edge_26040801.usr-arm64-verity.raw.zst
├── pine64-pinephonepro_phosh_edge_26040801.usr-arm64.raw.zst
├── pine64-pinephonepro_phosh_edge_26040801.dev-tools-sysext-arm64.raw.zst
├── pine64-pinephonepro_phosh_edge_26040801.dev-tools-confext-arm64.raw.zst
├── SHA256SUMS
├── SHA256SUMS.gpg
```

A mkosi profile, `compressed`, will automatically compress the usr+verity partitions and generate a SHA256SUMs file with these artifacts listed in it that can be appended to an existing manifest on the HTTP server when the new artifacts are deployed to it.

### Pipeline and Versioning

Gitlab CI is used for building images that are deployed on the HTTP server. All image builds within a single CI pipeline share the same `IMAGE_VERSION`. A global `mkosi.version` file on the server tracks the current version. After package builds succeed, a `determine-version` job fetches this file, runs `mkosi bump`, and writes it back immediately to claim the version. A CI resource group serializes this job across concurrent pipelines to prevent collisions. The resulting version is passed as an artifact to all downstream build jobs.

All images within a pipeline install from the same package snapshot. A `snapshot-repos` job runs before image builds. It calls `pmaports.py` for every combo to get the dynamic package list (device/UI packages and recursively resolved `pmb_recommends`), and invokes `mkosi summary` without profiles (using `--environment=` args) to get static package lists from `*.conf` files. Base, initrd, and extension subimage packages are each resolved separately via `apk add --simulate` to avoid provider conflicts between packages in each subimage. Extension packages are resolved unioned with the base package set, since extensions are built against the full base image. Build jobs use this snapshot via `--local-mirror` and `--cache-only=always`.

## Booting

As mentioned previously, EFI is required for booting Duranium.

* **DTB devices**: UKI contains multiple `.dtbauto` sections with all required DTBs embedded. U-boot looks for dtbs in well known paths in the ESP (e.g. `/dtbs`) so in addition to embedding dtbs, dtb files will be maintained in this path too.

* **ACPI devices**: Standard UKI without DTB sections, relies on firmware-provided ACPI

### Pre-kernel Boot Flow

1. **systemd-boot**: selects boot entries by sorting UKI files by version and boot count status. Entries without counters (successful boots) are preferred, followed by entries with tries remaining (+N suffix), then entries with zero tries left (marked bad).

2. **DTB Matching**: systemd-stub reads `compatible` from EFI table, finds matching `.dtbauto` section in UKI, replaces temporary DTB with version-matched one. This is not applicable for devices that support ACPI.

3. **Kernel Launch**: Boot proceeds with kernel boot

### Initramfs

The initramfs is built by mkosi and runs systemd as init. This replaced an earlier POC approach that modified the pmOS mkinitfs-generated initramfs, which was complicated and buggy. Switching to the mkosi initramfs solved several outstanding issues, particularly around disk detection (systemd in the initramfs sets up disks/partitions and the state persists seamlessly into the rootfs after switch-root).

All boot logic (first boot, normal boot, factory reset) is implemented as systemd units in the initramfs. systemd handles /usr partition + dm-verity setup automatically via `usrhash=` in the kernel cmdline. For Android devices with nested subpartitions, a systemd unit runs early in boot to scan and initialize them (using the same logic from the pmOS initramfs).

`mkosi.finalize` patches `initrd-release` in the initramfs with the correct `IMAGE_ID` and related variables, which is necessary for factory reset to work, since systemd-repart compares the IMAGE_ID in the EFI variable with the value in `/etc/initrd-release` to make sure it's resetting the correct OS.

**Normal boot flow:**

1. Subpartitions scanned/initialized (if applicable)

2. systemd detects LUKS partition for root. unl0kr is configured as a password agent for systemd to unlock it.

3. systemd automatically handles /usr partition + dm-verity setup

4. confexts are merged into /etc

5. switchroot

**First boot flow:**

1. systemd-repart in the initramfs creates missing partitions (B-slot usr + verity, and rootfs). Rootfs is always created on a LUKS volume with a default blank passphrase.

2. Subpartitions scanned/initialized (if applicable), then systemd switches root

3. confexts are merged into /etc

4. f0rmz runs in the rootfs, triggered by a sentinel file under `/etc`. It creates the user account and optionally sets a custom LUKS passphrase (replacing the blank passphrase set by repart). f0rmz was chosen over UI-specific first boot tools (gnome-initial-setup, Plasma setup) because it allows prompting for and setting a FDE passphrase, and is UI-agnostic. f0rmz is based on the buffybox/unl0kr codebase and is still incomplete.

**Factory reset flow:**

1. Triggered from the rootfs, e.g. `systemctl start systemd-factory-reset-request && reboot`. This sets the `FactoryResetRequest` EFI variable with OS data from `/etc/os-release`.

2. On next boot, systemd-repart in the initramfs detects the EFI variable, deletes the rootfs, and recreates it

3. Boot proceeds as first boot

## Populating /etc

Since many packages in Alpine Linux install configuration to /etc rather than /usr, the base image's /etc content is shipped as a directory-based confext image. systemd-confext merges this into /etc via overlayfs, providing both immutable defaults and transparent mutability.

At build time, `mkosi.finalize` relocates /etc into a confext at `/usr/lib/confexts/duranium-base-config/` with an extension-release file. The extension-release contains `ID=` and `CONFEXT_LEVEL=` fields that systemd-confext checks against the host os-release before merging, preventing version-mismatched confexts from being applied. Machine-specific files (e.g. machine-id, hostname) are excluded from the confext.

The confext overlay on /etc has two layers:

* **Lower layer** (read-only): `/usr/lib/confexts/duranium-base-config/`, the confext image containing default config files from packages

* **Upper layer** (writable): `/var/lib/extensions.mutable/etc/`, automatically created by systemd-confext. Duranium configures confext with `Mutable=yes` to enable this.

When a user edits a file in /etc, overlayfs creates a mutable copy in the upper layer. The user's version takes priority and persists across reboots, while unmodified files continue to reflect the read-only confext content and receive updates automatically. The upper layer can be inspected directly to see exactly which system configuration files have been modified, which is useful for debugging.

Sysexts whose packages install files to /etc require a paired confext image. These are built using a three-tier subimage pattern: a directory image installs packages against the base and captures the full filesystem diff, then separate sysext and confext subimages package only `/usr` and `/etc` content respectively. The paired confext is delivered to `/var/lib/confexts/` via sysupdate and merged alongside the base confext. See the System Extensions section for details on the build process and update mechanism.

## System Extensions

Duranium supports system extensions (sysexts) via systemd-sysext. Sysexts extend `/usr` at runtime by overlaying additional files on top of the immutable base, without modifying the base image itself. Installed sysexts live in `/var/lib/extensions/` and are merged on boot by `systemd-sysext.service`. Extensions are purely additive and strictly read-only. They are intended for optional functionality, and built from the same package repositories and at the same point in time as the base OS.

### Versioning

Alpine edge has no soname stability guarantees, packages are updated on an ongoing basis. For example, a sysext built against one week's set of libraries can break against the next week's. Sysexts must therefore be tightly coupled to the base image version. The same tight coupling model is used for stable releases for consistency.

To do this, os-release in the OS image includes `SYSEXT_LEVEL` set to `IMAGE_VERSION`. Each sysext carries an extension-release file with matching `SYSEXT_LEVEL`, `ID`, and architecture fields. At merge time, systemd-sysext validates these and refuses to activate mismatched extensions. Multiple versions can coexist in `/var/lib/extensions/` because systemd-sysext silently skips images whose `SYSEXT_LEVEL` does not match.

### Building

Sysexts are built as subimages alongside each device+UI combo's main image, using the full base package tree as their foundation. This is required because Alpine's post-install triggers produce aggregated artifacts (e.g. compiled GSettings schemas, icon caches) that are not composable across overlayfs layers. A sysext built against a minimal base would very likely produce incomplete or conflicting versions of these artifacts.

Extensions follow a three-tier subimage pattern. The first tier is a directory image that installs packages against the base image with `Overlay=yes`, capturing the full filesystem diff (both `/usr` and `/etc`). The second tier packages only the `/usr` content from the directory image as a sysext. If the extension's packages install files to `/etc`, a third tier packages the `/etc` content as a confext (see Populating /etc). Tiers 2 and 3 carry no packages of their own, all package resolution happens in tier 1.

All extension subimages share common configuration files under `mkosi.images/`. A shared finalize script appends `SYSEXT_LEVEL` or `CONFEXT_LEVEL` to the appropriate extension-release file. `SYSEXT_SCOPE` and `CONFEXT_SCOPE` are set to `system` via environment variables that mkosi reads when generating the extension-release.

### Updates

Sysext and confext transfers are defined alongside the existing OS transfers in `/usr/lib/sysupdate.d/`. They share the same `@v` version identifier, so a single `sysupdate update` invocation downloads version-locked OS artifacts and extension artifacts together.

Extensions are gated by sysupdate features. Each extension has a `.feature` file that defaults to disabled. Users enable extensions via `updatectl enable <feature>`. A sysext and its paired confext share the same feature name, so enabling a feature activates downloads for both images.

On the target side, sysupdate writes sysext images to `/var/lib/extensions/` and confext images to `/var/lib/confexts/`. `InstancesMax=2` retains two versions (current and previous), matching the A/B model used for `/usr` and UKI. The previous version is harmlessly ignored by systemd-sysext/confext until sysupdate cleans it up, or will be merged if the user boots the other slot.

## Factory Reset

Some other immutable OS designs using systemd tooling (e.g. ParticleOS) use a kernel command line parameter to trigger a factory reset condition. This is accomplished by building a UKI with a profile to add this parameter and named something like "Factory Reset", and the bootloader (systemd-boot) exposes this option in the boot menu. Having this as a boot menu option could lead to an accidental (or malicious) factory reset of the device, since it doesn't require any authentication to select this boot option and could be done unintentionally with a misplaced click/button press. There's also some risk that this option might be auto-selected by the bootloader! In the best case this is inconvenient if the user has good backups, but a more likely worst case is unrecoverable loss of data.

To help avoid this situation, this design relies on systemd's factory reset infrastructure to set an EFI variable that systemd-repart detects on the next boot. The variable is set from within an authenticated OS session (e.g. via `systemd-factory-reset-request.service`), and could be wrapped behind a GUI application in userspace to make it user-friendly. SecureBoot further limits the scope where this variable could be set.

This uses systemd's factory reset infrastructure (requires systemd >=258) <https://www.freedesktop.org/software/systemd/man/devel/systemd-factory-reset.html>

**Flow:**

1. User triggers factory reset from authenticated OS session (e.g. `systemctl start systemd-factory-reset-request`, or a GUI that calls into it)

2. The `FactoryResetRequest` EFI variable is set, which the initramfs/repart will detect on the next boot to trigger the reset process.

3. System is rebooted

4. See Initramfs section above for the factory reset boot flow

## References

* [Fitting Everything Together](https://0pointer.net/blog/fitting-everything-together.html)

* [ParticleOS](https://github.com/particle-iot/particle-os)
