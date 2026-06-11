#!/usr/bin/env python3
# Copyright (c) Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
patch-uefi-dtbs: Patch QcCapsuleRootCert in all DTBs within a uefi_dtbs ELF.

Unlike xbl_config.elf (XBLConfig metadata v2), uefi_dtbs.elf embeds multiple
raw DTBs concatenated in one ELF segment.  This command:

  1. Scans every ELF segment for concatenated DTBs (magic 0xd00dfeed).
  2. For each DTB that contains QcCapsuleRootCert, locates the node path
     using libfdt directly - no external dtc dependency, handles both direct
     (/sw/uefi/uefiplat) and overlay (/fragment@N/__overlay__/.../uefiplat) forms.
  3. Patches the property value from a .inc hex file (output of bin-to-hex).
  4. Reconstructs the segment with correct delta-offset tracking across
     multiple DTBs so later DTBs are always addressed at their true position.
  5. Updates ELF p_filesz/p_memsz, and shifts p_offset/sh_offset/e_shoff for
     all headers that follow the modified segment when the segment grows.
  6. Updates all SHA-384 hashes (per-DTB and per-segment) by binary
     search-and-replace - no hardcoded offsets.
"""

import argparse
import hashlib
import os
import struct
import sys
import tempfile
from io import BytesIO
from typing import List, Optional, Tuple

import libfdt
from elftools.elf.elffile import ELFFile

from qcom_capsule_tool.dtb_utils import DTB_MAGIC, find_cert_node, patch_dtb, scan_dtbs
from qcom_capsule_tool.elf_utils import (
    _ph_file_offset_field,
    _ph_filesz_field,
    _ph_memsz_field,
    _pack,
    _write_ph_field,
    _write_sh_field,
    update_elf_headers_for_growth,
)

_DEFAULT_PROP_NAME = "QcCapsuleRootCert"

# -- DTB introspection ---


def _get_model(dtb_bytes: bytes) -> str:
    """Return the /model string from *dtb_bytes* using libfdt; 'unknown' on error."""
    try:
        fdt = libfdt.Fdt(dtb_bytes)
        root = fdt.path_offset("/")
        prop = fdt.getprop(root, "model")
        return bytes(prop).rstrip(b"\x00").decode("utf-8", errors="replace")
    except Exception:
        return "unknown"


# -- core function ---


def patch_uefi_dtbs(
    elf_path: str,
    cert_inc_path: str,
    output_path: str,
    prop_name: str = _DEFAULT_PROP_NAME,
) -> List[dict]:
    """
    Patch *prop_name* in every DTB embedded in *elf_path*.

    Args:
        elf_path:      Path to the input uefi_dtbs ELF.
        cert_inc_path: Path to the .inc hex file produced by ``bin-to-hex``.
        output_path:   Path for the output patched ELF.
        prop_name:     DTB property name to patch (default: QcCapsuleRootCert).

    Returns:
        List of result dicts, one entry per DTB found:
            segment   -- ELF program-header index
            dtb_index -- position among DTBs in that segment
            offset    -- byte offset of DTB within the segment (at time of patch)
            model     -- value of the /model property
            node_path -- node path that was patched (or None)
            status    -- human-readable outcome string
    """
    with open(elf_path, "rb") as f:
        raw = bytearray(f.read())

    # Pre-scan: identify which segment indices contain DTBs using the original
    # layout.  We process them one at a time and re-parse ELF between iterations
    # so that a growing segment does not corrupt offsets used in later passes.
    elf0 = ELFFile(BytesIO(bytes(raw)))
    seg_indices_with_dtbs = [
        i for i, seg in enumerate(elf0.iter_segments()) if scan_dtbs(seg.data())
    ]

    results: List[dict] = []

    for seg_idx in seg_indices_with_dtbs:
        # Re-parse from the current (potentially modified) raw bytes so that
        # p_offset values for this segment are up-to-date.
        elf = ELFFile(BytesIO(bytes(raw)))
        is_64 = elf.elfclass == 64

        seg = list(elf.iter_segments())[seg_idx]
        seg_data = bytearray(seg.data())
        seg_file_offset = seg["p_offset"]
        orig_seg_size = len(seg_data)

        dtbs = scan_dtbs(bytes(seg_data))
        old_seg_hash = hashlib.sha384(bytes(seg_data)).digest()

        # - Phase 1: patch DTBs with delta-offset tracking -
        #
        # dtbs[] is a snapshot of (original_offset, original_size) taken
        # before any patching.  As each DTB is spliced back into seg_data its
        # neighbours shift; `delta` accumulates that shift so every subsequent
        # DTB is read from its true current position.
        delta = 0
        seg_modified = False
        per_dtb_hash_pairs: List[Tuple[bytes, bytes]] = []

        for dtb_idx, (dtb_off_orig, dtb_sz) in enumerate(dtbs):
            dtb_off = dtb_off_orig + delta  # true position in current seg_data
            dtb_bytes = bytes(seg_data[dtb_off : dtb_off + dtb_sz])

            model = _get_model(dtb_bytes)
            node_path = find_cert_node(dtb_bytes, prop_name)

            if node_path is None:
                results.append(
                    dict(
                        segment=seg_idx,
                        dtb_index=dtb_idx,
                        offset=dtb_off,
                        model=model,
                        node_path=None,
                        status=f"skip (no {prop_name})",
                    )
                )
                continue

            try:
                old_dtb_hash = hashlib.sha384(dtb_bytes).digest()
                patched = patch_dtb(dtb_bytes, node_path, cert_inc_path, prop_name)
                new_dtb_hash = hashlib.sha384(patched).digest()
            except Exception as exc:
                results.append(
                    dict(
                        segment=seg_idx,
                        dtb_index=dtb_idx,
                        offset=dtb_off,
                        model=model,
                        node_path=node_path,
                        status=f"error: {exc}",
                    )
                )
                continue

            per_dtb_hash_pairs.append((old_dtb_hash, new_dtb_hash))

            # Splice patched DTB back; Python bytearray handles size change.
            seg_data = (
                seg_data[:dtb_off] + bytearray(patched) + seg_data[dtb_off + dtb_sz :]
            )
            delta += len(patched) - dtb_sz
            seg_modified = True

            results.append(
                dict(
                    segment=seg_idx,
                    dtb_index=dtb_idx,
                    offset=dtb_off,
                    model=model,
                    node_path=node_path,
                    status="patched",
                )
            )

        if not seg_modified:
            continue

        new_seg_hash = hashlib.sha384(bytes(seg_data)).digest()
        grow = len(seg_data) - orig_seg_size

        # - Phase 2: write seg_data into raw; fix ELF headers -
        #
        # bytearray slice assignment with a different-length replacement
        # inserts/removes bytes at the splice point, growing or shrinking raw
        # automatically - no manual tail-copy required.
        raw[seg_file_offset : seg_file_offset + orig_seg_size] = seg_data

        if grow != 0:
            update_elf_headers_for_growth(raw, elf, seg_file_offset, grow)

        # Update p_filesz and p_memsz of the modified segment.
        filesz_f, filesz_sz = _ph_filesz_field(is_64)
        memsz_f, memsz_sz = _ph_memsz_field(is_64)
        _write_ph_field(raw, elf, seg_idx, filesz_f, filesz_sz, len(seg_data))
        _write_ph_field(raw, elf, seg_idx, memsz_f, memsz_sz, len(seg_data))

        # - Phase 3: update SHA-384 hashes by binary search-and-replace -
        #
        # Search only within the PT_NULL hash segment that follows the modified
        # PT_LOAD segment - avoids false matches if payload data happens to
        # contain bytes identical to a 48-byte hash.  Fall back to a whole-file
        # search only when no such segment exists (non-standard ELF layout).
        raw_bytes = bytes(raw)
        segs_now = list(ELFFile(BytesIO(raw_bytes)).iter_segments())
        hash_seg = next(
            (s for s in segs_now[seg_idx + 1 :] if s["p_type"] == "PT_NULL"),
            None,
        )
        if hash_seg is not None:
            h_start = hash_seg["p_offset"]
            h_end = h_start + hash_seg["p_filesz"]
        else:
            h_start, h_end = 0, len(raw_bytes)

        for old_h, new_h in per_dtb_hash_pairs:
            pos = raw_bytes.find(old_h, h_start, h_end)
            if pos != -1:
                raw[pos : pos + 48] = new_h
                print(f"[i] Per-DTB SHA-384 updated at file 0x{pos:x}")
            else:
                print("[!] Per-DTB SHA-384 not found in hash segment (non-fatal)")

        pos = raw_bytes.find(old_seg_hash, h_start, h_end)
        if pos != -1:
            raw[pos : pos + 48] = new_seg_hash
            print(f"[i] Segment SHA-384 updated at file 0x{pos:x}")
        else:
            print("[!] Segment SHA-384 not found in hash segment (non-fatal)")

    with open(output_path, "wb") as f:
        f.write(raw)

    return results


# -- CLI ---


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="qcom-capsule-tool patch-uefi-dtbs",
        description="Patch QcCapsuleRootCert in all DTBs within a uefi_dtbs ELF",
    )
    ap.add_argument("elf_file", help="Input uefi_dtbs ELF file")
    ap.add_argument("cert_inc", help=".inc hex file from bin-to-hex")
    ap.add_argument("output_elf", help="Output patched ELF file")
    args = ap.parse_args()

    print(f"[+] Input ELF : {args.elf_file}")
    print(f"[+] Cert .inc : {args.cert_inc}")

    results = patch_uefi_dtbs(args.elf_file, args.cert_inc, args.output_elf)

    patched = skipped = errors = 0
    print(f"\n{'=' * 64}")
    for r in results:
        if "patched" in r["status"]:
            tag = "PATCHED"
            patched += 1
        elif "skip" in r["status"]:
            tag = "SKIPPED"
            skipped += 1
        else:
            tag = "ERROR"
            errors += 1
        print(f"  [{tag:<7}] PH#{r['segment']} DTB#{r['dtb_index']}  {r['model']}")
        if r["node_path"]:
            print(f"             node  : {r['node_path']}")
        print(f"             status: {r['status']}")
    print(f"{'=' * 64}")
    print(f"  patched={patched}  skipped={skipped}  errors={errors}")
    print(f"  output : {args.output_elf}")
    print(f"{'=' * 64}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
