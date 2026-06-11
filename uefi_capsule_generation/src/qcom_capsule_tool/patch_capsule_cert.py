#!/usr/bin/env python3
# Copyright (c) Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
patch-capsule-cert: Patch QcCapsuleRootCert in a uefi_dtbs or xbl_config ELF.

Auto-detects the ELF type by scanning program-header payloads:
  - uefi_dtbs  : one or more ELF segments contain raw DTBs (FDT magic 0xd00dfeed).
                 The certificate is stored as a DTB property and is replaced in
                 every DTB that carries it.
  - xbl_config : PH#1 contains a valid XBLConfig metadata blob (4-byte ASCII
                 type tag + version + entry count).  The certificate is stored
                 as a DTB property inside one of the named DTB payload segments.
                 Find the DTB segment containing QcCapsuleRootCert, patch the
                 property, then call replace_ph() to write back and update
                 p_filesz/p_memsz, xblconfig item_size, and SHA-384.

Both paths accept a plain DER (.cer) certificate file.  Internally:
  - uefi_dtbs path converts the .cer to a temporary .inc hex file (same format
    produced by ``bin-to-hex``) and calls patch_uefi_dtbs().
  - xbl_config path patches the DTB property and calls xblconfig_parser.replace_ph().

Usage:
    qcom-capsule-tool patch-capsule-cert <input.elf> <cert.cer> <output.elf> \\
        [--prop-name QcCapsuleRootCert]
"""

import argparse
import os
import struct
import sys
import tempfile

from elftools.elf.elffile import ELFFile

from qcom_capsule_tool.BinToHex import bin_to_hex

# -- ELF-type detection ---

DTB_MAGIC = 0xD00DFEED
_DEFAULT_NAME = "QcCapsuleRootCert"


def _has_dtb_segment(elf: ELFFile) -> bool:
    """Return True if any ELF segment contains DTB magic (0xd00dfeed)."""
    for seg in elf.iter_segments():
        data = seg.data()
        for i in range(0, len(data) - 3, 4):
            if struct.unpack(">I", data[i : i + 4])[0] == DTB_MAGIC:
                return True
    return False


def _has_xblconfig_metadata(elf: ELFFile) -> bool:
    """
    Return True if PH#1 looks like a valid XBLConfig metadata blob.

    Checks that the first 4 bytes are printable ASCII (the xcfg_type field)
    and that the header parses without error - no hardcoded magic value so
    this works regardless of the specific type tag ('XCFG', 'CFGL', etc.).
    """
    from qcom_capsule_tool.xblconfig_parser import parse_meta_header

    segs = list(elf.iter_segments())
    if len(segs) < 2:
        return False
    data = segs[1].data()
    if len(data) < 12:
        return False
    if not all(0x20 <= b < 0x7F for b in data[:4]):
        return False
    try:
        parse_meta_header(data, 0)
        return True
    except Exception:
        return False


ELF_TYPE_UEFI_DTBS = "uefi_dtbs"
ELF_TYPE_XBL_CONFIG = "xbl_config"


def detect_elf_type(elf_path: str) -> str:
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        if _has_xblconfig_metadata(elf):
            return ELF_TYPE_XBL_CONFIG
        if _has_dtb_segment(elf):
            return ELF_TYPE_UEFI_DTBS
    raise ValueError(
        f"Cannot determine ELF type for '{elf_path}': "
        "no XBLConfig metadata header and no DTB segments found."
    )


# -- xbl_config: find DTB segment -> patch property -> replace_ph ---


def _patch_xbl_config(
    elf_path: str,
    cert_cer_path: str,
    output_path: str,
    prop_name: str,
    meta_ph_index: int,
) -> None:
    """
    Patch *prop_name* in xbl_config ELFs where the certificate is stored as a
    DTB property inside one of the named payload segments:

      1. Find which named segment contains a DTB with *prop_name*.
      2. Patch the DTB property with the new certificate.
      3. Call replace_ph() to write the patched segment back into the ELF,
         updating p_filesz/p_memsz, xblconfig item_size, and SHA-384.
    """
    import io

    from qcom_capsule_tool.dtb_utils import find_cert_node, patch_dtb, scan_dtbs
    from qcom_capsule_tool.xblconfig_parser import parse_metadata_from_ph, replace_ph

    with open(elf_path, "rb") as f:
        raw = f.read()
    elf = ELFFile(io.BytesIO(raw))
    segs = list(elf.iter_segments())
    _, items, _, _ = parse_metadata_from_ph(elf, meta_ph_index)

    inc_fd, inc_path = tempfile.mkstemp(suffix=".inc")
    os.close(inc_fd)
    try:
        bin_to_hex(cert_cer_path, inc_path)

        patched = skipped = errors = 0
        items_snapshot = list(items)
        for idx, item in enumerate(items_snapshot):
            ph_index = idx + 2
            if ph_index >= len(segs):
                continue

            seg_data = segs[ph_index].data()
            dtbs = scan_dtbs(seg_data)
            if not dtbs:
                continue

            for dtb_off, dtb_sz in dtbs:
                dtb_bytes = seg_data[dtb_off : dtb_off + dtb_sz]
                node_path = find_cert_node(dtb_bytes, prop_name)
                if node_path is None:
                    skipped += 1
                    continue

                print(
                    f"[+] xbl_config: found '{prop_name}' in "
                    f"'{item.config_name}' (PH#{ph_index}) at {node_path}"
                )

                try:
                    patched_dtb = patch_dtb(dtb_bytes, node_path, inc_path, prop_name)
                except Exception as exc:
                    print(f"[!] xbl_config: error patching '{item.config_name}': {exc}")
                    errors += 1
                    continue

                # Reconstruct the full segment bytes with the patched DTB spliced in.
                if len(dtbs) == 1 and dtb_off == 0:
                    new_seg_bytes = patched_dtb
                else:
                    new_seg = bytearray(seg_data)
                    new_seg[dtb_off : dtb_off + dtb_sz] = patched_dtb
                    new_seg_bytes = bytes(new_seg)

                tmp_fd, tmp_seg_path = tempfile.mkstemp(suffix=".dtb")
                os.close(tmp_fd)
                try:
                    with open(tmp_seg_path, "wb") as f:
                        f.write(new_seg_bytes)

                    # replace_ph: splice back + update p_filesz + item_size + SHA-384.
                    replace_ph(
                        elf_path=elf_path,
                        target_ph_index=ph_index,
                        new_file=tmp_seg_path,
                        output_file=output_path,
                        meta_ph_index=meta_ph_index,
                    )
                    # Subsequent iterations work on the already-patched output.
                    elf_path = output_path
                    with open(elf_path, "rb") as f:
                        raw = f.read()
                    elf = ELFFile(io.BytesIO(raw))
                    segs = list(elf.iter_segments())
                    patched += 1
                finally:
                    try:
                        os.unlink(tmp_seg_path)
                    except OSError:
                        pass

        print(f"[+] xbl_config: patched={patched}  skipped={skipped}  errors={errors}")
        if errors:
            sys.exit(1)
        if patched == 0:
            raise ValueError(
                f"No DTB segment in '{elf_path}' contains property '{prop_name}'"
            )
    finally:
        try:
            os.unlink(inc_path)
        except OSError:
            pass


# -- main patch routine ---


def patch_capsule_cert(
    elf_path: str,
    cert_cer_path: str,
    output_path: str,
    prop_name: str = _DEFAULT_NAME,
    meta_ph_index: int = 1,
) -> str:
    """
    Patch the capsule root certificate in *elf_path* and write to *output_path*.

    Args:
        elf_path:       Input ELF (uefi_dtbs or xbl_config).
        cert_cer_path:  DER certificate file (.cer).
        output_path:    Path for the patched output ELF.
        prop_name:      DTB property name to patch (default: QcCapsuleRootCert).
        meta_ph_index:  PH index of the XBLConfig metadata blob (default: 1).

    Returns:
        Detected ELF type string ("uefi_dtbs" or "xbl_config").
    """
    elf_type = detect_elf_type(elf_path)
    print(f"[+] Detected ELF type : {elf_type}")

    if elf_type == ELF_TYPE_UEFI_DTBS:
        from qcom_capsule_tool.patch_uefi_dtbs import patch_uefi_dtbs

        inc_fd, inc_path = tempfile.mkstemp(suffix=".inc")
        os.close(inc_fd)
        try:
            bin_to_hex(cert_cer_path, inc_path)
            results = patch_uefi_dtbs(elf_path, inc_path, output_path, prop_name)
        finally:
            try:
                os.unlink(inc_path)
            except OSError:
                pass

        patched = sum(1 for r in results if "patched" in r["status"])
        skipped = sum(1 for r in results if "skip" in r["status"])
        errors = sum(1 for r in results if "error" in r["status"])
        print(f"[+] uefi_dtbs: patched={patched}  skipped={skipped}  errors={errors}")
        if errors:
            sys.exit(1)

    else:  # xbl_config
        _patch_xbl_config(
            elf_path=elf_path,
            cert_cer_path=cert_cer_path,
            output_path=output_path,
            prop_name=prop_name,
            meta_ph_index=meta_ph_index,
        )

    return elf_type


# -- CLI ---


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="qcom-capsule-tool patch-capsule-cert",
        description=(
            "Patch QcCapsuleRootCert in a uefi_dtbs or xbl_config ELF. "
            "The ELF type is detected automatically."
        ),
    )
    ap.add_argument("elf_file", help="Input ELF file (uefi_dtbs or xbl_config)")
    ap.add_argument("cert_cer", help="DER certificate file (.cer)")
    ap.add_argument("output_elf", help="Output patched ELF file")
    ap.add_argument(
        "--prop-name",
        default=_DEFAULT_NAME,
        help="DTB property name to patch (default: %(default)s)",
    )
    ap.add_argument(
        "--meta-ph",
        type=int,
        default=1,
        help="XBLConfig metadata program-header index (default: %(default)s)",
    )
    args = ap.parse_args()

    print(f"[+] Input ELF  : {args.elf_file}")
    print(f"[+] Cert (.cer): {args.cert_cer}")
    print(f"[+] Output ELF : {args.output_elf}")

    patch_capsule_cert(
        elf_path=args.elf_file,
        cert_cer_path=args.cert_cer,
        output_path=args.output_elf,
        prop_name=args.prop_name,
        meta_ph_index=args.meta_ph,
    )

    print(f"[+] Done. Output written to: {args.output_elf}")


if __name__ == "__main__":
    main()
