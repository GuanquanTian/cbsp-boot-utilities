# Copyright (c) Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
Common DTB utilities for patching QcCapsuleRootCert in uefi_dtbs and xbl_config ELFs.

Provides:
  - scan_dtbs       : locate all DTBs in a raw byte buffer
  - find_cert_node  : walk a DTB and return the node path owning a property
  - patch_dtb       : replace a DTB property value and return patched bytes
"""

import os
import struct
import tempfile
from typing import List, Optional, Tuple

import libfdt

from qcom_capsule_tool.set_dtb_property import set_dtb_property

DTB_MAGIC = 0xD00DFEED


def scan_dtbs(data: bytes) -> List[Tuple[int, int]]:
    """Return (offset, totalsize) for every DTB found in *data*."""
    results: List[Tuple[int, int]] = []
    i = 0
    while i <= len(data) - 8:
        if struct.unpack(">I", data[i : i + 4])[0] == DTB_MAGIC:
            size = struct.unpack(">I", data[i + 4 : i + 8])[0]
            if size >= 8 and i + size <= len(data):
                results.append((i, size))
                i = (i + size + 3) & ~3
                continue
        i += 4
    return results


def _fdt_first_subnode(fdt: libfdt.Fdt, node_off: int) -> int:
    try:
        return fdt.first_subnode(node_off)
    except libfdt.FdtException:
        return -1


def _fdt_next_subnode(fdt: libfdt.Fdt, node_off: int) -> int:
    try:
        return fdt.next_subnode(node_off)
    except libfdt.FdtException:
        return -1


def find_cert_node(
    dtb_bytes: bytes, prop_name: str = "QcCapsuleRootCert"
) -> Optional[str]:
    """
    Walk *dtb_bytes* with libfdt and return the first node path that owns
    a *prop_name* property.

    Handles both layouts without any external dtc dependency:
      - Regular DTB  : /sw/uefi/uefiplat
      - Overlay DTB  : /fragment@N/__overlay__/uefi/uefiplat
    """
    try:
        fdt = libfdt.Fdt(dtb_bytes)
    except Exception:
        return None

    def _walk(node_off: int, path: str) -> Optional[str]:
        try:
            fdt.getprop(node_off, prop_name)
            return path
        except libfdt.FdtException:
            pass

        child = _fdt_first_subnode(fdt, node_off)
        while child >= 0:
            try:
                name = fdt.get_name(child)
            except Exception:
                child = _fdt_next_subnode(fdt, child)
                continue
            child_path = path + name if path == "/" else f"{path}/{name}"
            result = _walk(child, child_path)
            if result is not None:
                return result
            child = _fdt_next_subnode(fdt, child)

        return None

    try:
        root = fdt.path_offset("/")
        return _walk(root, "/")
    except Exception:
        return None


def patch_dtb(
    dtb_bytes: bytes,
    node_path: str,
    cert_inc_path: str,
    prop_name: str = "QcCapsuleRootCert",
) -> bytes:
    """Patch *prop_name* in a single DTB and return the patched bytes."""
    tmp_in = tmp_out = ""
    try:
        tmp_in_fd, tmp_in = tempfile.mkstemp(suffix=".dtb")
        os.close(tmp_in_fd)
        with open(tmp_in, "wb") as f:
            f.write(dtb_bytes)
        tmp_out_fd, tmp_out = tempfile.mkstemp(suffix=".dtb")
        os.close(tmp_out_fd)
        os.unlink(tmp_out)
        set_dtb_property(
            tmp_in, node_path, prop_name, f"@list:{cert_inc_path}", tmp_out
        )
        with open(tmp_out, "rb") as f:
            return f.read()
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass
