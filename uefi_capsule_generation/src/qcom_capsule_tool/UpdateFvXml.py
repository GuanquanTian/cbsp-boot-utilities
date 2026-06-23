# --------------------------------------------------------------------
# Copyright (c) 2025 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear
# --------------------------------------------------------------------

import argparse
import os
import re
import subprocess
import sys
from xml.dom import minidom

REPO_URL = "https://github.com/qualcomm-linux/qcom-ptool.git"
REPO_DIR = "qcom-ptool"
DEFAULT_REPO_DIR = REPO_DIR

SUPPORTED_PLATFORMS = {
    "QCS6490": "qcs6490-rb3gen2",
    "QCS9100": "qcs9100-ride-sx",
    "QCS8300": "qcs8300-ride-sx",
    "QCS615": "qcs615-adp-air",
    "QRB2210": "qrb2210-rb1",
    "CQ2390M": "shikra-evk",
    "IQ-X7181": "iq-x7181-evk",
    "IQ-X5121": "iq-x7181-evk",
    "Kaanapali": "kaanapali-mtp",
    "SM8750": "sm8750-mtp",
}


def get_target_name(soc_name):
    for platform, target in SUPPORTED_PLATFORMS.items():
        if soc_name == platform:
            return target


def safe_clone(repo_dir):
    if not os.path.exists(repo_dir):
        try:
            subprocess.run(["git", "clone", REPO_URL, repo_dir], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error cloning repo: {e}")
            sys.exit(1)


def read_partitions_conf(partition_conf_path):
    try:
        with open(partition_conf_path, "r") as f:
            return f.readlines()
    except FileNotFoundError:
        print(f"Error: {partition_conf_path} not found.")
        sys.exit(1)


def detect_storage_type_from_conf(lines):
    for line in lines:
        if re.search(r"--type=ufs", line, re.IGNORECASE):
            return "UFS"
        elif re.search(r"--type=emmc", line, re.IGNORECASE):
            return "EMMC"
        elif re.search(r"--type=spinor", line, re.IGNORECASE):
            return "SPINOR"
    print("Error: Could not detect StorageType from partitions.conf.")
    sys.exit(1)


def parse_partition_info(args, lines, storage_type):
    partition_info = {}
    # Unified regex to handle both UFS and eMMC partition lines.
    pattern = re.compile(
        r"--partition\s+"
        r"(?:--lun=(?P<lun>\d+)\s+)?"
        r"--name=(?P<name>[\w\-]+)\s+"
        r"--size=\d+KB\s+"
        r"--type-guid=(?P<guid>[\w\-]+)"
        r"(?:\s+--type=(?P<type>ufs|emmc|spinor))?"
        r"(?:\s+--filename=(?P<filename>[\w.\-]+))?",
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        gd = match.groupdict()
        lun = gd.get("lun")
        name = gd["name"]
        guid = gd["guid"]
        filename = gd.get("filename") or f"{name}.img"

        # Only UFS partition lines carry a LUN; eMMC and SPINOR never do.
        want_lun = storage_type == "UFS"

        if want_lun:
            # For UFS we only accept LUN 1 or 4
            if lun not in ["1", "4"]:
                continue
            entry = {"lun": lun, "guid": guid, "filename": filename}
        else:
            # For eMMC/SPINOR we ignore entries that contain a LUN
            if lun is not None:
                continue
            entry = {"guid": guid, "filename": filename}

        partition_info[name] = entry
    return partition_info


def find_base_names(partition_info):
    """Detect primary/backup partition pairs.

    Two naming conventions are used across targets: `<base>_a`/`<base>_b`
    (e.g. UFS targets like Kaanapali) and `<base>`/`<base>_BACKUP` (e.g.
    SPINOR targets like Hamoa/Purwa). Returns a list of
    (base, primary_name, backup_name) tuples.
    """
    pairs = []
    seen = set()
    for name in partition_info:
        if name in seen:
            continue
        if name.endswith("_a"):
            backup_name = name[:-2] + "_b"
            if backup_name in partition_info:
                base = name[:-2]
                pairs.append((base, name, backup_name))
                seen.update((name, backup_name))
                continue
        if name.endswith("_BACKUP"):
            continue
        backup_name = f"{name}_BACKUP"
        if backup_name in partition_info:
            pairs.append((name, name, backup_name))
            seen.update((name, backup_name))
    return pairs


def create_xml(args, pairs, partition_info):
    doc = minidom.Document()
    fvitems = doc.createElement("FVItems")
    doc.appendChild(fvitems)

    metadata = doc.createElement("Metadata")
    for tag, text in [("BreakingChangeNumber", "0"), ("FlashType", args.StorageType)]:
        elem = doc.createElement(tag)
        elem.appendChild(doc.createTextNode(text))
        metadata.appendChild(elem)
    fvitems.appendChild(metadata)

    for base, primary_name, backup_name in sorted(pairs, key=lambda p: p[0]):
        part_a = partition_info[primary_name]
        part_b = partition_info[backup_name]

        if args.StorageType == "UFS":
            disk_type = f"UFS_LUN{part_a['lun']}"
        elif args.StorageType in ("SPINOR", "NORUFS", "NORNVME"):
            disk_type = "SPINOR"
        else:
            disk_type = "EMMC_PARTITION_USER_DATA"

        fw_entry = doc.createElement("FwEntry")
        for tag, text in [
            ("InputBinary", part_a["filename"]),
            ("InputPath", "Images"),
            ("Operation", "IGNORE"),
            ("UpdateType", "UPDATE_PARTITION"),
            ("BackupType", "BACKUP_PARTITION"),
        ]:
            elem = doc.createElement(tag)
            elem.appendChild(doc.createTextNode(text))
            fw_entry.appendChild(elem)

        dest = doc.createElement("Dest")
        for tag, text in [
            ("DiskType", disk_type),
            ("PartitionName", primary_name),
            ("PartitionTypeGUID", part_a["guid"]),
        ]:
            elem = doc.createElement(tag)
            elem.appendChild(doc.createTextNode(text))
            dest.appendChild(elem)
        fw_entry.appendChild(dest)

        backup = doc.createElement("Backup")
        for tag, text in [
            ("DiskType", disk_type),
            ("PartitionName", backup_name),
            ("PartitionTypeGUID", part_b["guid"]),
        ]:
            elem = doc.createElement(tag)
            elem.appendChild(doc.createTextNode(text))
            backup.appendChild(elem)
        fw_entry.appendChild(backup)

        fvitems.appendChild(fw_entry)
    return doc


def write_xml(doc, output_file="FvUpdate.xml"):
    with open(output_file, "wb") as f:
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        f.write(xml_str)


def main():
    parser = argparse.ArgumentParser(
        description="Generate FvUpdate.xml from partitions.conf"
    )
    custom_usage = (
        "UpdateFvXml.py [-h] (-T TARGET & -S {UFS,EMMC,NORUFS,NORNVME}) "
        "| [-F PARTITIONS_CONF]"
    )
    parser = argparse.ArgumentParser(usage=custom_usage)
    parser.add_argument("-T", metavar="TARGET", help="Target argument")
    parser.add_argument(
        "-S",
        "--StorageType",
        choices=["UFS", "EMMC", "NORUFS", "NORNVME"],
        help="Specify storage type: UFS, EMMC, NORUFS, or NORNVME",
    )
    parser.add_argument(
        "-F", metavar="PARTITIONS_CONF", help="Partitions config argument"
    )
    parser.add_argument(
        "--ptool-path",
        dest="ptool_path",
        default=None,
        help="Path to an existing qcom-ptool directory; "
        "when provided, the repository is not cloned",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="PARTITION",
        default=None,
        help="Only emit FwEntry blocks for these partition base names "
        "(e.g. --only dtb). By default every partition pair is emitted.",
    )
    args = parser.parse_args()

    repo_dir = args.ptool_path if args.ptool_path else DEFAULT_REPO_DIR

    if args.F:
        if args.StorageType:
            print(
                "Error: Do not provide -S/--StorageType when using -F/--partitions_conf. It will be auto-detected."
            )
            sys.exit(1)
        if args.T:
            print(
                "Error: Do not provide -T/--StorageType when using -F/--partitions_conf."
            )
            sys.exit(1)
        partition_conf_path = args.F
        lines = read_partitions_conf(partition_conf_path)
        args.StorageType = detect_storage_type_from_conf(lines)
    elif args.T:
        if not args.StorageType:
            print("Error: You must provide -S/--StorageType when using -T/--target.")
            sys.exit(1)
        if not args.ptool_path:
            safe_clone(repo_dir)
        target = get_target_name(args.T)
        if not target:
            print("Provided target is Unknown !!! Please re-check")
            sys.exit(1)
        conf_dir = (
            "spinor"
            if args.StorageType in ("NORUFS", "NORNVME")
            else args.StorageType.lower()
        )
        partition_conf_path = os.path.join(
            repo_dir, "platforms", target, conf_dir, "partitions.conf"
        )
        lines = read_partitions_conf(partition_conf_path)
    else:
        print("Error: Invalid argument combination.")
        parser.print_usage()
        sys.exit(1)

    partition_info = parse_partition_info(args, lines, args.StorageType)
    pairs = find_base_names(partition_info)
    if args.only:
        wanted = set(args.only)
        found = {base for base, _, _ in pairs}
        missing = wanted - found
        if missing:
            print(
                "Error: --only requested partition(s) not found: "
                f"{', '.join(sorted(missing))}. Available: "
                f"{', '.join(sorted(found))}."
            )
            sys.exit(1)
        pairs = [p for p in pairs if p[0] in wanted]
    if not pairs:
        print(
            "Warning: No partition pairs (_a/_b or _BACKUP) found. FvUpdate.xml will not contain FwEntry blocks."
        )
    doc = create_xml(args, pairs, partition_info)
    write_xml(doc)
    print(
        f"FvUpdate.xml has been created successfully with StorageType={args.StorageType}."
    )


if __name__ == "__main__":
    main()
