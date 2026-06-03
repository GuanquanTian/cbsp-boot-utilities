# IQ-X7181 UEFI Capsule Generation

## Overview

`build_capsule_iqx7181.sh` is a one-stop shell script that automates the full
capsule generation pipeline for the **IQ-X7181** platform (SPINOR flash, UEFI
DTBs stored in `uefi_dtbs.elf`/`uefi_dtbs.xz`).

The script wraps the Python-based `qcom-capsule-tool` suite and drives every
stage in order:

| Step | Description |
|------|-------------|
| Python venv | Creates/reuses an isolated venv, installs `requests pyelftools pylibfdt` |
| Certificate chain | Auto-generates a test chain **or** uses existing OEM certs |
| QcFMPRoot.inc | Converts `QcFMPRoot.cer` (DER) → hex `.inc` for cert patching |
| uefi\_dtbs patch | Injects the new root cert into every DTB inside `uefi_dtbs.elf` |
| uefi\_dtbs compress | Compresses the patched ELF → `uefi_dtbs.xz` (configurable name) |
| edk2 setup | Clones edk2 (shallow) and builds `GenFfs` / `GenFv` |
| SYSFW\_VERSION.bin | Generates the firmware version binary |
| FvUpdate.xml | Copies your `FvUpdate.xml` into the build directory |
| firmware.fv | Packages all payload binaries into a Firmware Volume |
| config.json | Writes the JSON parameters for `GenerateCapsule.py` |
| capsule (.cap) | Calls `GenerateCapsule.py` to produce the final capsule file |

---

## Prerequisites

```
bash, xz, openssl, git, python3 (≥ 3.10)
```

Python packages are installed automatically into a managed venv and `pip` runs
on every invocation (fast if already cached); pass `--no-venv` to skip entirely.
GitHub network access (for edk2 clone) is only needed the first time — edk2
setup runs automatically when `GenFv` is absent and is skipped on subsequent
runs.

---

## Directory Layout

```
uefi_capsule_generation/
├── build_capsule_iqx7181.sh   # Main script (this document)
├── FvUpdate.xml               # Firmware-entry configuration (edit per build)
├── Images/
│   ├── dtb.bin                # Device-tree binary to flash to dtb_a/b
│   └── uefi_dtbs.xz           # (or uefi_dtbs.elf) UEFI DTB container
├── Certificates/              # Generated or OEM-supplied cert chain
│   ├── QcFMPCert.pem          # Leaf cert + unencrypted private key (PEM)
│   ├── QcFMPRoot.pub.pem      # Root CA public cert (PEM)
│   ├── QcFMPSub.pub.pem       # Sub CA public cert (PEM)
│   └── QcFMPRoot.cer          # Root CA cert (DER, input to bin-to-hex)
├── edk2/                      # Cloned + built by `setup` (auto-created)
├── src/qcom_capsule_tool/     # Python tool modules
└── build/iqx7181/             # All build artifacts (auto-created)
    ├── uefi_dtbs_patched.elf  # Intermediate: patched ELF
    ├── uefi_dtbs.xz           # Compressed patched ELF (capsule payload)
    ├── firmware.fv
    ├── config.json
    └── capsule_iqx7181.cap    # Final output
```

---

## Quick Start

edk2 setup runs automatically on the first invocation (when `GenFv` is absent)
and is skipped automatically on subsequent runs — no flag needed either way.

```bash
./build_capsule_iqx7181.sh --fw-ver 0.0.2.0
```

Test certificates are generated in `Certificates/` if none are present.

### Specifying the lowest supported version

```bash
./build_capsule_iqx7181.sh --fw-ver 0.0.2.0 --lfw-ver 0.0.1.0
```

### Force or suppress edk2 setup explicitly

```bash
./build_capsule_iqx7181.sh --setup    --fw-ver 0.0.2.0   # always run setup
./build_capsule_iqx7181.sh --no-setup --fw-ver 0.0.2.0   # never run setup
```

---

## All Parameters

### Command-line flags

| Flag | Default | Description |
|------|---------|-------------|
| `--fw-ver X.X.X.X` | `0.0.2.0` | Firmware version written into the capsule |
| `--lfw-ver X.X.X.X` | `0.0.0.0` | Lowest firmware version the device will accept |
| `--output name.cap` | `capsule_iqx7181.cap` | Output capsule filename (written to `BUILD_DIR`) |
| `--images <dir>` | `./Images` | Directory containing `dtb.bin`, `uefi_dtbs.xz`, etc. |
| `--dtbs-xz-out <file>` | `BUILD_DIR/uefi_dtbs.xz` | Full path of the compressed patched uefi_dtbs |
| `--fvupdate <file>` | `./FvUpdate.xml` | Path to your `FvUpdate.xml` |
| `--build-dir <dir>` | `./build/iqx7181` | Directory for all intermediate and final build artifacts |
| `--cert-dir <dir>` | `./Certificates` | Directory containing the cert chain (see [Certificates](#certificates)) |
| `--edk2-path <dir>` | `./edk2` (auto-detected after `setup`) | Path to an already-built edk2 tree; skips cloning/building |
| `--setup` | — | Force edk2 setup even if `GenFv` already exists |
| `--no-setup` | — | Skip edk2 clone/build unconditionally |
| `--no-gen-certs` | — | Require real OEM certs; abort if chain is missing |
| `--no-venv` | — | Use the system/active Python instead of creating a managed venv |
| `--venv-path <dir>` | `./.venv` | Path for the Python virtual environment |
| `-h` / `--help` | — | Print usage and exit |

### Environment variables

Every flag above has a matching environment variable (uppercase, underscores).
Environment variables are evaluated **before** flags, so a flag on the command
line always wins.

| Variable | Corresponding flag |
|----------|--------------------|
| `FW_VERSION` | `--fw-ver` |
| `LOWEST_FW_VER` | `--lfw-ver` |
| `FMP_GUID` | _(no flag)_ IQ-X7181 ESRT GUID |
| `CAPSULE_NAME` | `--output` |
| `BUILD_DIR` | `--build-dir` |
| `EDK2_DIR` | `--edk2-path` |
| `FVUPDATE_XML` | `--fvupdate` |
| `IMAGES_DIR` | `--images` |
| `UEFI_DTBS_ELF` | Path to the input `uefi_dtbs.elf` (overrides `IMAGES_DIR/uefi_dtbs.elf`) |
| `UEFI_DTBS_XZ` | Path to the input `uefi_dtbs.xz` (overrides `IMAGES_DIR/uefi_dtbs.xz`) |
| `UEFI_DTBS_OUT` | Path for the intermediate patched ELF (`BUILD_DIR/uefi_dtbs_patched.elf`) |
| `UEFI_DTBS_XZ_OUT` | `--dtbs-xz-out` |
| `CERT_DIR` | `--cert-dir` |
| `CERT_PEM` | Path to leaf cert (overrides `CERT_DIR/QcFMPCert.pem`) |
| `CERT_ROOT_PEM` | Path to root public cert (overrides `CERT_DIR/QcFMPRoot.pub.pem`) |
| `CERT_SUB_PEM` | Path to sub public cert (overrides `CERT_DIR/QcFMPSub.pub.pem`) |
| `CERT_ROOT_INC` | Path to root `.inc` hex file (overrides `CERT_DIR/QcFMPRoot.inc`) |
| `GENERATE_TEST_CERTS` | `1` = auto-generate test chain; `0` = same as `--no-gen-certs` |
| `CERT_PASSWORD` | Password for test cert key files (default: `testpassword`) |
| `SETUP_VENV` | `1` = create venv (default); `0` = same as `--no-venv` |
| `VENV_DIR` | `--venv-path` |

**IQ-X7181 ESRT GUID (fixed):** `0F6D58FC-2258-4D27-9E23-D77219B0897C`

---

## FvUpdate.xml

`FvUpdate.xml` defines which binary files are packed into the Firmware Volume
and which partitions they target on the device.

### Key fields

| Field | Description |
|-------|-------------|
| `<InputBinary>` | Filename of the payload binary |
| `<InputPath>` | Directory to search for the binary (relative to `BUILD_DIR` when `fv-create` runs) |
| `<Operation>` | `UPDATE` or `IGNORE` |
| `<DiskType>` | `SPINOR` for IQ-X7181 |
| `<PartitionName>` | Target partition name (case-sensitive) |
| `<PartitionTypeGUID>` | Partition GUID (see `FirmwarePartitions.md`) |

### Complete example — updating `dtb.bin` + `uefi_dtbs.xz`

> The shipped `FvUpdate.xml` contains only the `dtb.bin` entry.
> Add the `uefi_dtbs.xz` `<FwEntry>` below when you also need to update the UEFI DTB partition.

```xml
<FVItems>
  <Metadata>
    <BreakingChangeNumber>0</BreakingChangeNumber>
    <FlashType>NORUFS</FlashType>
  </Metadata>

  <!-- dtb.bin is sourced from Images/ directory -->
  <FwEntry>
    <InputBinary>dtb.bin</InputBinary>
    <InputPath>Images</InputPath>
    <Operation>UPDATE</Operation>
    <UpdateType>UPDATE_PARTITION</UpdateType>
    <BackupType>BACKUP_PARTITION</BackupType>
    <Dest>
      <DiskType>SPINOR</DiskType>
      <PartitionName>dtb_a</PartitionName>
      <PartitionTypeGUID>{2A1A52FC-AA0B-401C-A808-5EA0F91068F8}</PartitionTypeGUID>
    </Dest>
    <Backup>
      <DiskType>SPINOR</DiskType>
      <PartitionName>dtb_b</PartitionName>
      <PartitionTypeGUID>{A166F11A-2B39-4FAA-B7E7-F8AA080D0587}</PartitionTypeGUID>
    </Backup>
  </FwEntry>

  <!--
    uefi_dtbs.xz is the cert-patched output produced by the script.
    InputPath must be "." because fv-create runs from BUILD_DIR and the
    patched file is written to BUILD_DIR/uefi_dtbs.xz (UEFI_DTBS_XZ_OUT).
  -->
  <FwEntry>
    <InputBinary>uefi_dtbs.xz</InputBinary>
    <InputPath>.</InputPath>
    <Operation>UPDATE</Operation>
    <UpdateType>UPDATE_PARTITION</UpdateType>
    <BackupType>BACKUP_PARTITION</BackupType>
    <Dest>
      <DiskType>SPINOR</DiskType>
      <PartitionName>uefi_a</PartitionName>
      <PartitionTypeGUID>{400FFDCD-22E0-47E7-9A23-F16ED9382388}</PartitionTypeGUID>
    </Dest>
    <Backup>
      <DiskType>SPINOR</DiskType>
      <PartitionName>uefi_b</PartitionName>
      <PartitionTypeGUID>{9F234B5B-0EFB-4313-8E4C-0AF1F605536B}</PartitionTypeGUID>
    </Backup>
  </FwEntry>

</FVItems>
```

> **Note on `InputPath`:**
> - Binaries from `Images/` → use `InputPath = Images`
> - The patched `uefi_dtbs.xz` produced by the script → use `InputPath = .`
>   (it lives in `BUILD_DIR`, which is the working directory when `fv-create` runs)

---

## Certificates

### Test certificates (auto-generated, default)

On first run, if `Certificates/QcFMPCert.pem` is absent and
`GENERATE_TEST_CERTS=1`, a full three-tier test chain is generated under
`Certificates/`:

```
QcFMPRoot.key / .crt / .cer / .pub.pem   ← Root CA
QcFMPSub.key  / .crt / .pub.pem          ← Intermediate CA
QcFMPCert.key / .crt / .pem              ← Leaf signing cert
QcFMPRoot.inc                            ← Hex array for DTB patching
```

Test certs are re-used on subsequent runs. The cert password defaults to
`testpassword`; override with `CERT_PASSWORD=<pw>`.

### Production (OEM) certificates

1. Place your OEM cert files in `Certificates/` (or a custom `CERT_DIR`).
2. Pass `--no-gen-certs` to prevent any auto-generation.
3. Required files:

   | File | Description |
   |------|-------------|
   | `QcFMPCert.pem` | Leaf cert + unencrypted private key, concatenated (PEM) |
   | `QcFMPRoot.pub.pem` | Root CA public cert (PEM) |
   | `QcFMPSub.pub.pem` | Sub CA public cert (PEM) |
   | `QcFMPRoot.cer` | Root CA cert (DER) — used to generate `QcFMPRoot.inc` |

---

## uefi\_dtbs Patching

IQ-X7181 stores the capsule root certificate in the UEFI device tree
(`uefi_dtbs.elf`) rather than `xbl_config.elf`. The script handles this
automatically:

1. If both `uefi_dtbs.elf` and `uefi_dtbs.xz` exist → abort with error
2. If `Images/uefi_dtbs.elf` exists → use directly
3. If `Images/uefi_dtbs.xz` exists → decompress to `BUILD_DIR/uefi_dtbs.elf`
4. If neither exists → skip patch step (warning only; build continues)
5. Scan every ELF segment for concatenated DTBs (magic `0xD00DFEED`)
6. For each DTB containing `QcCapsuleRootCert`, auto-detect the node path
   (supports both regular `/sw/uefi/uefiplat` and overlay fragment layouts)
7. Patch the property value from `QcFMPRoot.inc`; update the embedded SHA-384
8. Write patched ELF to `BUILD_DIR/uefi_dtbs_patched.elf`
9. Compress to `UEFI_DTBS_XZ_OUT` (default: `BUILD_DIR/uefi_dtbs.xz`)

To put a fresh build product directly into the pipeline:

```bash
cp /path/to/built/uefi_dtbs.xz Images/uefi_dtbs.xz
./build_capsule_iqx7181.sh --no-setup --fw-ver 1.0.3.0
```

To control the compressed output filename:

```bash
./build_capsule_iqx7181.sh --no-setup --fw-ver 1.0.3.0 \
    --dtbs-xz-out /tmp/my_uefi_dtbs.xz
```

---

## Build Outputs

All artifacts are written to `BUILD_DIR` (default `build/iqx7181/`):

| File | Description |
|------|-------------|
| `uefi_dtbs_patched.elf` | Patched (cert-injected) ELF |
| `uefi_dtbs.xz` | Compressed patched ELF — payload for capsule |
| `SYSFW_VERSION.bin` | Firmware version structure |
| `FvUpdate.xml` | Copy of your input XML |
| `firmware.fv` | Firmware Volume containing all payloads |
| `config.json` | Parameters for `GenerateCapsule.py` |
| `capsule_iqx7181.cap` | **Final capsule file** |

---

## Advanced Usage

### Use an existing edk2 build

```bash
./build_capsule_iqx7181.sh --no-setup \
    --edk2-path /opt/edk2 \
    --fw-ver 1.0.0.0
```

### Custom Images and build directories

```bash
./build_capsule_iqx7181.sh --no-setup \
    --images /mnt/nfs/board_images \
    --build-dir /tmp/capsule_build \
    --fw-ver 2.0.0.0 \
    --output release_v2.cap
```

### Production build with OEM certs

```bash
./build_capsule_iqx7181.sh --no-setup --no-gen-certs \
    --cert-dir /secure/oem_certs \
    --fw-ver 3.0.0.0
```

### Override via environment variables

```bash
FW_VERSION=1.5.0.0 LOWEST_FW_VER=1.0.0.0 \
IMAGES_DIR=/nfs/images \
GENERATE_TEST_CERTS=0 \
    ./build_capsule_iqx7181.sh --no-setup
```

---

## qcom-capsule-tool Subcommands

The script drives these subcommands internally. They can also be called
directly for debugging or individual steps.

| Subcommand | Description |
|------------|-------------|
| `patch-uefi-dtbs <elf> <cert.inc> <out.elf>` | Patch `QcCapsuleRootCert` in all DTBs of a uefi_dtbs ELF |
| `bin-to-hex <input.cer> <output.inc>` | Convert DER cert to hex `.inc` array |
| `sysfw-version-create -Gen -FwVer X -LFwVer Y -O out.bin` | Generate `SYSFW_VERSION.bin` |
| `fv-create <out.fv> -FvType SYS_FW <xml> <ver.bin> <img_dir> --edk2-path <dir>` | Build Firmware Volume |
| `update-json -j cfg.json -f SYS_FW -b ver.bin -pf fw.fv -p cert.pem -x root.pem -oc sub.pem -g GUID` | Write `config.json` |
| `setup` | Clone edk2 (shallow) and build `GenFfs` / `GenFv` |
| `parse-config <xbl_config.elf> dump --out-dir <dir>` | Extract DTBs from xbl_config.elf |
| `set-dtb-property <dtb> <node> <prop> @list:<inc> <out>` | Patch a DTB property |

---

## Partition Reference (IQ-X7181 SPINOR)

| Partition | GUID |
|-----------|------|
| `dtb_a` | `2A1A52FC-AA0B-401C-A808-5EA0F91068F8` |
| `dtb_b` | `A166F11A-2B39-4FAA-B7E7-F8AA080D0587` |
| `uefi_a` | `400FFDCD-22E0-47E7-9A23-F16ED9382388` |
| `uefi_b` | `9F234B5B-0EFB-4313-8E4C-0AF1F605536B` |
| `xbl_a` | `DEA0BA2C-CBDD-4805-B4F9-F428251C3E98` |
| `xbl_b` | `7A3DF1A3-A31A-454D-BD78-DF259ED486BE` |
| `tz_a` | `A053AA7F-40B8-4B1C-BA08-2F68AC71A4F4` |
| `tz_b` | `C832EA16-8B0D-4398-A67B-EBB30EF98E7E` |

---

## License

BSD-3-Clause-Clear — Copyright (c) 2026 Qualcomm Innovation Center, Inc.
