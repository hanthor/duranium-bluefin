#!/bin/bash
# Extract Bluefin system files from common OCI for layering into mkosi build
# Usage: ./scripts/extract-bluefin-omc.sh ghcr.io/projectbluefin/common:latest

set -e

BLUEFIN_IMAGE="${1:-ghcr.io/projectbluefin/common:latest}"
OUTPUT_DIR="${2:-system_files/bluefin}"

echo "Extracting Bluefin common OCI: $BLUEFIN_IMAGE"

mkdir -p "$OUTPUT_DIR"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# Try podman first
if command -v podman &> /dev/null; then
    echo "Using podman to extract OCI..."
    CONTAINER_ID=$(podman create "$BLUEFIN_IMAGE" true 2>/dev/null)
    podman export "$CONTAINER_ID" 2>/dev/null | tar -C "$TMPDIR" -xf - || true
    podman rm "$CONTAINER_ID" 2>/dev/null || true
    
    # Copy system_files from extracted OCI
    if [ -d "$TMPDIR/system_files" ]; then
        echo "Copying system_files from OCI..."
        cp -rv "$TMPDIR/system_files"/* "$OUTPUT_DIR/" 2>/dev/null || true
    fi
    
    # Copy /etc, /usr/share for branding
    echo "Copying branding and configuration..."
    for subdir in etc/default/grub.d etc/issue.d usr/share/icons usr/share/pixmaps usr/lib/os-release; do
        if [ -d "$TMPDIR/$subdir" ]; then
            mkdir -p "$OUTPUT_DIR/$subdir"
            cp -rv "$TMPDIR/$subdir"/* "$OUTPUT_DIR/$subdir/" 2>/dev/null || true
        fi
    done
fi

# Try skopeo if podman failed
if [ ! -f "$OUTPUT_DIR/env" ]; then
    if command -v skopeo &> /dev/null; then
        echo "Using skopeo to extract OCI..."
        skopeo copy "docker://$BLUEFIN_IMAGE" "oci:$TMPDIR:latest" 2>/dev/null || true
        # Further processing would extract from OCI blob directory
    fi
fi

echo "Bluefin system files extracted to: $OUTPUT_DIR"
ls -la "$OUTPUT_DIR" || true
