# Copyright (c) Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear

"""
Shared ELF program-header / section-header patching helpers.

Used by both xblconfig_parser and patch_uefi_dtbs to avoid duplication.
All helpers operate on a raw *bytearray* of the ELF image together with a
pyelftools *ELFFile* object that was opened against the **original** bytes
(before any splicing), so the header field offsets remain valid.
"""

import struct
from typing import Tuple

from elftools.elf.elffile import ELFFile


# -- Field layout ---
# ELF32 Phdr: type(4) offset(4) vaddr(4) paddr(4) filesz(4) memsz(4) flags(4) align(4)
# ELF64 Phdr: type(4) flags(4) offset(8) vaddr(8) paddr(8) filesz(8) memsz(8) align(8)
# ELF32 Shdr: name(4) type(4) flags(4) addr(4) offset(4) ...
# ELF64 Shdr: name(4) type(4) flags(8) addr(8) offset(8) ...


def _ph_file_offset_field(is_64: bool) -> Tuple[int, int]:
    """Return (field_offset_in_phdr, field_size) for p_offset."""
    return (8, 8) if is_64 else (4, 4)


def _ph_filesz_field(is_64: bool) -> Tuple[int, int]:
    """Return (field_offset_in_phdr, field_size) for p_filesz."""
    return (0x20, 8) if is_64 else (0x10, 4)


def _ph_memsz_field(is_64: bool) -> Tuple[int, int]:
    """Return (field_offset_in_phdr, field_size) for p_memsz."""
    return (0x28, 8) if is_64 else (0x14, 4)


def _sh_offset_field(is_64: bool) -> Tuple[int, int]:
    """Return (field_offset_in_shdr, field_size) for sh_offset."""
    return (24, 8) if is_64 else (16, 4)


def _pack(endian: str, size: int, value: int) -> bytes:
    return struct.pack(endian + {4: "I", 8: "Q"}[size], value)


# -- Field writers ---


def _write_ph_field(
    data: bytearray,
    elf: ELFFile,
    seg_idx: int,
    field_off: int,
    field_size: int,
    value: int,
) -> None:
    endian = "<" if elf.little_endian else ">"
    pos = elf.header["e_phoff"] + seg_idx * elf.header["e_phentsize"] + field_off
    data[pos : pos + field_size] = _pack(endian, field_size, value)


def _write_sh_field(
    data: bytearray,
    elf: ELFFile,
    sec_idx: int,
    field_off: int,
    field_size: int,
    value: int,
) -> None:
    endian = "<" if elf.little_endian else ">"
    pos = elf.header["e_shoff"] + sec_idx * elf.header["e_shentsize"] + field_off
    data[pos : pos + field_size] = _pack(endian, field_size, value)


# -- Growth fixup ---


def update_elf_headers_for_growth(
    data: bytearray, elf: ELFFile, seg_file_offset: int, grow: int
) -> None:
    """
    After splicing *grow* bytes at *seg_file_offset*, fix all ELF offsets that
    point past the splice point:
      - p_offset for every subsequent program header
      - sh_offset for every subsequent section header
      - e_shoff in the ELF header itself if the section-header table moved

    *grow* may be negative (segment shrank); offsets are still corrected.
    *elf* must be bound to the **pre-splice** bytes so header field positions
    are still valid when we write into *data*.
    """
    is_64 = elf.elfclass == 64
    endian = "<" if elf.little_endian else ">"

    off_field, off_sz = _ph_file_offset_field(is_64)
    for i, seg in enumerate(elf.iter_segments()):
        if seg["p_offset"] > seg_file_offset:
            _write_ph_field(data, elf, i, off_field, off_sz, seg["p_offset"] + grow)

    sh_off_field, sh_off_sz = _sh_offset_field(is_64)
    for i, sec in enumerate(elf.iter_sections()):
        if sec["sh_offset"] > seg_file_offset:
            _write_sh_field(
                data, elf, i, sh_off_field, sh_off_sz, sec["sh_offset"] + grow
            )

    e_shoff = elf.header["e_shoff"]
    if e_shoff > seg_file_offset:
        e_shoff_pos = 0x28 if is_64 else 0x20
        e_shoff_sz = 8 if is_64 else 4
        data[e_shoff_pos : e_shoff_pos + e_shoff_sz] = _pack(
            endian, e_shoff_sz, e_shoff + grow
        )
