#!/bin/bash
# Copyright (c) Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause-Clear
# Capsule generation script for IQ-X7181
#
# Usage:
#   ./build_capsule_iqx7181.sh [OPTIONS]
#   ./build_capsule_iqx7181.sh --fw-ver 0.0.2.0
#
# Prerequisites:
#   - python3 (>= 3.10); pyelftools and pylibfdt are installed
#       automatically in a managed venv (pass --no-venv to use system Python)
#   - Certificates directory ready (set CERT_DIR, or individual cert vars)
#   - Images directory ready (set IMAGES_DIR)
#   - FvUpdate.xml is generated automatically from qcom-ptool's
#       partitions.conf for --target/--storage; only the dtb (dtb.bin)
#       partition pair is included by default (--fvupdate-only to change,
#       --fvupdate-all for every partition pair); pass --fvupdate to use an
#       existing file, or --ptool-path to reuse a local qcom-ptool checkout
#
# FFS/FV images and the final capsule are generated natively in Python;
# no edk2 checkout or BaseTools binaries are required.
#
# The tool modules are invoked directly from src/ (no package installation needed).

set -euo pipefail

# ---- User-configurable variables ------------------------------------

FW_VERSION="${FW_VERSION:-0.0.2.0}"
LOWEST_FW_VER="${LOWEST_FW_VER:-0.0.0.0}"
FMP_GUID="${FMP_GUID:-0F6D58FC-2258-4D27-9E23-D77219B0897C}"
CAPSULE_NAME="${CAPSULE_NAME:-capsule_iqx7181.cap}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-${SCRIPT_DIR}/build/iqx7181}"

# FvUpdate.xml source. If FVUPDATE_XML points at an existing file it is used
# as-is; otherwise it is generated automatically from qcom-ptool's
# partitions.conf for TARGET/STORAGE via `update-fv-xml`.
FVUPDATE_XML="${FVUPDATE_XML:-}"
# Target and storage type used when auto-generating FvUpdate.xml.
TARGET="${TARGET:-IQ-X7181}"
STORAGE="${STORAGE:-NORUFS}"
# Optional path to an existing qcom-ptool checkout; when set, it is not cloned.
PTOOL_PATH="${PTOOL_PATH:-}"
# Partition base name(s) to include in the auto-generated FvUpdate.xml
# (passed to `update-fv-xml --only`). This capsule only carries the Linux
# DTB (dtb.bin); leave empty to emit every partition pair instead.
FVUPDATE_ONLY="${FVUPDATE_ONLY:-dtb}"
IMAGES_DIR="${IMAGES_DIR:-${SCRIPT_DIR}/Images}"
UEFI_DTBS_ELF="${UEFI_DTBS_ELF:-${IMAGES_DIR}/uefi_dtbs.elf}"
UEFI_DTBS_XZ="${UEFI_DTBS_XZ:-${IMAGES_DIR}/uefi_dtbs.xz}"
UEFI_DTBS_OUT="${UEFI_DTBS_OUT:-${BUILD_DIR}/uefi_dtbs_patched.elf}"
UEFI_DTBS_XZ_OUT="${UEFI_DTBS_XZ_OUT:-${BUILD_DIR}/uefi_dtbs.xz}"
# Optional: set XBL_CONFIG_ELF to patch the capsule cert in xbl_config.elf as well.
XBL_CONFIG_ELF="${XBL_CONFIG_ELF:-${IMAGES_DIR}/xbl_config.elf}"
XBL_CONFIG_OUT="${XBL_CONFIG_OUT:-${BUILD_DIR}/xbl_config_patched.elf}"

CERT_DIR="${CERT_DIR:-${SCRIPT_DIR}/Certificates}"
CERT_PEM="${CERT_PEM:-${CERT_DIR}/QcFMPCert.pem}"
CERT_ROOT_PEM="${CERT_ROOT_PEM:-${CERT_DIR}/QcFMPRoot.pub.pem}"
CERT_SUB_PEM="${CERT_SUB_PEM:-${CERT_DIR}/QcFMPSub.pub.pem}"
CERT_ROOT_CER="${CERT_ROOT_CER:-${CERT_DIR}/QcFMPRoot.cer}"
# Set to 1 to auto-generate a test cert chain when certs are absent (default).
# Set to 0 (or pass --no-gen-certs) to require real OEM certs.
GENERATE_TEST_CERTS="${GENERATE_TEST_CERTS:-1}"
CERT_PASSWORD="${CERT_PASSWORD:-testpassword}"
# Set to 1 to auto-create a Python venv and install required packages (default).
# Set to 0 (or pass --no-venv) to use the system/active Python environment.
SETUP_VENV="${SETUP_VENV:-1}"
VENV_DIR="${VENV_DIR:-${SCRIPT_DIR}/.venv}"

# ---- Argument parsing -----------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fw-ver)      FW_VERSION="$2";    shift ;;
        --lfw-ver)     LOWEST_FW_VER="$2"; shift ;;
        --output)      CAPSULE_NAME="$2";  shift ;;
        --images)      IMAGES_DIR="$2"
                       UEFI_DTBS_ELF="${IMAGES_DIR}/uefi_dtbs.elf"
                       UEFI_DTBS_XZ="${IMAGES_DIR}/uefi_dtbs.xz"
                       XBL_CONFIG_ELF="${IMAGES_DIR}/xbl_config.elf"
                       shift ;;
        --dtbs-xz-out) UEFI_DTBS_XZ_OUT="$2"; shift ;;
        --cert-dir)    CERT_DIR="$2"
                       CERT_PEM="${CERT_DIR}/QcFMPCert.pem"
                       CERT_ROOT_PEM="${CERT_DIR}/QcFMPRoot.pub.pem"
                       CERT_SUB_PEM="${CERT_DIR}/QcFMPSub.pub.pem"
                       CERT_ROOT_CER="${CERT_DIR}/QcFMPRoot.cer"
                       shift ;;
        --fvupdate)    FVUPDATE_XML="$2"; shift ;;
        --target)      TARGET="$2"; shift ;;
        --storage)     STORAGE="$2"; shift ;;
        --ptool-path)  PTOOL_PATH="$2"; shift ;;
        --fvupdate-only) FVUPDATE_ONLY="$2"; shift ;;
        --fvupdate-all)  FVUPDATE_ONLY="" ;;
        --build-dir)   BUILD_DIR="$2"; shift ;;
        --no-gen-certs) GENERATE_TEST_CERTS=0 ;;
        --no-venv)     SETUP_VENV=0 ;;
        --venv-path)   VENV_DIR="$2"; shift ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --fw-ver  X.X.X.X    Firmware version (default: ${FW_VERSION})"
            echo "  --lfw-ver X.X.X.X    Lowest firmware version (default: ${LOWEST_FW_VER})"
            echo "  --output  name.cap   Output capsule filename (default: ${CAPSULE_NAME})"
            echo "  --images  <dir>      Images directory (default: ${IMAGES_DIR})"
            echo "  --cert-dir <dir>     Certificates directory (default: ${CERT_DIR})"
            echo "  --fvupdate <file>    Use an existing FvUpdate.xml instead of generating one"
            echo "  --target <name>      Target for auto-generated FvUpdate.xml (default: ${TARGET})"
            echo "  --storage <type>     Storage type: UFS, EMMC, NORUFS, or NORNVME (default: ${STORAGE})"
            echo "  --ptool-path <dir>   Existing qcom-ptool checkout (skips clone when generating FvUpdate.xml)"
            echo "  --fvupdate-only <base...> Partition base name(s) to include in auto-generated FvUpdate.xml (default: ${FVUPDATE_ONLY})"
            echo "  --fvupdate-all       Include every partition pair in auto-generated FvUpdate.xml"
            echo "  --dtbs-xz-out <file> Compressed uefi_dtbs output filename (default: uefi_dtbs.xz in BUILD_DIR)"
            echo "  --build-dir <dir>    Build output directory (default: ${BUILD_DIR})"
            echo "  --no-gen-certs       Require real OEM certs; do not auto-generate test chain"
            echo "  --no-venv            Skip venv creation; use system/active Python"
            echo "  --venv-path <dir>    Python venv directory (default: .venv)"
            echo ""
            echo "Environment variables (same names as above flags, uppercase with underscores):"
            echo "  FW_VERSION, LOWEST_FW_VER, FMP_GUID, CAPSULE_NAME"
            echo "  BUILD_DIR, FVUPDATE_XML (optional), TARGET, STORAGE, PTOOL_PATH, FVUPDATE_ONLY, IMAGES_DIR"
            echo "  UEFI_DTBS_ELF, UEFI_DTBS_XZ, UEFI_DTBS_XZ_OUT"
            echo "  XBL_CONFIG_ELF (optional: patch xbl_config cert too), XBL_CONFIG_OUT"
            echo "  CERT_DIR, CERT_PEM, CERT_ROOT_PEM, CERT_SUB_PEM, CERT_ROOT_CER"
            echo "  GENERATE_TEST_CERTS (default: 1), CERT_PASSWORD"
            echo "  SETUP_VENV (default: 1), VENV_DIR"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# ---- Helper ---------------------------------------------------------

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
error() { echo "[ERROR] $*" >&2; exit 1; }

# ---- Step tracking --------------------------------------------------

declare -A _STEP_STATUS
declare -A _STEP_LABEL
_STEP_ORDER=()
_CURRENT_STEP=""

_step_init()  { _STEP_ORDER+=("$1"); _STEP_STATUS["$1"]="PENDING"; _STEP_LABEL["$1"]="${2:-$1}"; }
_step_start() { _CURRENT_STEP="$1"; }
_step_ok()    { _STEP_STATUS["$1"]="OK";      _CURRENT_STEP=""; }
_step_skip()  { _STEP_STATUS["$1"]="SKIPPED"; _CURRENT_STEP=""; }

_print_summary() {
    local rc=$?
    [[ -n "$_CURRENT_STEP" && $rc -ne 0 ]] && _STEP_STATUS["$_CURRENT_STEP"]="FAILED"
    echo ""
    echo "============================================================"
    echo "  Build Summary"
    echo "============================================================"
    local past_fail=0
    for key in "${_STEP_ORDER[@]}"; do
        local label="${_STEP_LABEL[$key]}"
        local status="${_STEP_STATUS[$key]}"
        if [[ "$status" == "PENDING" ]]; then
            [[ $past_fail -eq 1 ]] && status="NOT RUN" || status="FAILED"
        fi
        [[ "$status" == "FAILED" ]] && past_fail=1
        case "$status" in
            OK)        printf "  %-28s  [   OK    ]\n" "$label" ;;
            SKIPPED)   printf "  %-28s  [ SKIPPED ]\n" "$label" ;;
            FAILED)    printf "  %-28s  [  FAILED ]\n" "$label" ;;
            "NOT RUN") printf "  %-28s  [ NOT RUN ]\n" "$label" ;;
        esac
    done
    echo "------------------------------------------------------------"
    if [[ $rc -eq 0 ]]; then
        echo "  Result:  SUCCESS"
        echo "  Capsule: ${BUILD_DIR}/${CAPSULE_NAME}"
    else
        echo "  Result:  FAILED (exit code $rc)"
    fi
    echo "============================================================"
}
trap _print_summary EXIT

_step_init "venv"       "Python venv"
_step_init "certs"      "Certificate chain"
_step_init "dtbpatch"   "uefi_dtbs patch"
_step_init "dtbxz"      "uefi_dtbs.xz"
_step_init "xblpatch"   "xbl_config patch"
_step_init "sysfw"      "SYSFW_VERSION.bin"
_step_init "fvxml"      "FvUpdate.xml"
_step_init "fv"         "Firmware Volume"
_step_init "json"       "config.json"
_step_init "capsule"    "Capsule (.cap)"

# ---- Resolve Python interpreter and set up module path --------------

PY3="python3"
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
QCT="${PY3} -m qcom_capsule_tool.cli"

# ---- Python virtual environment -------------------------------------

_step_start "venv"
if [[ "${SETUP_VENV}" -eq 1 ]]; then
    if [[ ! -f "${VENV_DIR}/bin/python3" ]]; then
        info "Creating Python venv at ${VENV_DIR}..."
        python3 -m venv "${VENV_DIR}"
    else
        info "Reusing existing venv: ${VENV_DIR}"
    fi
    info "Installing Python dependencies (pyelftools pylibfdt)..."
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip -q
    "${VENV_DIR}/bin/python" -m pip install pyelftools pylibfdt -q
    PY3="${VENV_DIR}/bin/python3"
    QCT="${PY3} -m qcom_capsule_tool.cli"
    info "Using Python: $(${PY3} --version)"
    _step_ok "venv"
else
    info "--- venv: skipped (--no-venv) ---"
    _step_skip "venv"
fi

# ---- Preflight checks -----------------------------------------------

info "=== IQ-X7181 Capsule Generation ==="
info "FW_VERSION   : ${FW_VERSION}"
info "LOWEST_FW_VER: ${LOWEST_FW_VER}"
info "FMP_GUID     : ${FMP_GUID}"
info "CAPSULE_NAME : ${CAPSULE_NAME}"
info "BUILD_DIR    : ${BUILD_DIR}"

# FvUpdate.xml is either supplied via --fvupdate or generated later from
# TARGET/STORAGE; only validate it here when an explicit path was given.
[ -z "${FVUPDATE_XML}" ] || [ -f "${FVUPDATE_XML}" ] \
    || error "FvUpdate.xml not found: ${FVUPDATE_XML}"
[ -d "${IMAGES_DIR}" ]   || error "Images directory not found: ${IMAGES_DIR}"

# ---- Certificate chain ----------------------------------------------

certs_complete() {
    [[ -f "${CERT_PEM}" && -f "${CERT_ROOT_PEM}" && -f "${CERT_SUB_PEM}" \
       && -f "${CERT_DIR}/QcFMPRoot.cer" ]]
}

_step_start "certs"
if certs_complete; then
    info "--- Certs: existing chain found, skipping generation ---"
    _step_skip "certs"
elif [[ "${GENERATE_TEST_CERTS}" -eq 1 ]]; then
    warn "No complete cert chain found; generating TEST certificates."
    warn "For production, supply real OEM certs and use --no-gen-certs."

    OPENSSL_CFG=""
    for cand in \
        "${SCRIPT_DIR}/../.github/opensslroot.cfg" \
        "${SCRIPT_DIR}/.github/opensslroot.cfg"
    do
        if [[ -f "$cand" ]]; then OPENSSL_CFG="$cand"; break; fi
    done
    [[ -n "${OPENSSL_CFG}" ]] \
        || error "opensslroot.cfg not found; ensure .github/ is present relative to the script."

    mkdir -p "${CERT_DIR}"
    cd "${CERT_DIR}"
    cp "${OPENSSL_CFG}" ./opensslroot.cfg
    mkdir -p demoCA/newcerts
    : > demoCA/index.txt
    echo 01 > demoCA/serial
    openssl rand -out randfile 256

    pw="${CERT_PASSWORD}"

    info "Generating Root CA..."
    openssl genrsa -aes256 -passout pass:"${pw}" -out QcFMPRoot.key 2048
    openssl req -new -x509 -config opensslroot.cfg \
        -subj '/CN=OEM Root CA/O=FMP/OU=OEM Key/L=Test/ST=Test/C=US' \
        -days 3650 -passin pass:"${pw}" -key QcFMPRoot.key -out QcFMPRoot.crt
    openssl x509 -in QcFMPRoot.crt -out QcFMPRoot.cer -outform DER
    openssl x509 -inform DER -in QcFMPRoot.cer -outform PEM -out QcFMPRoot.pub.pem

    info "Generating Sub CA..."
    openssl genrsa -aes256 -passout pass:"${pw}" -out QcFMPSub.key 2048
    openssl req -new -config opensslroot.cfg \
        -subj '/CN=OEM Intermediate CA/O=FMP/OU=OEM Key/L=Test/ST=Test/C=US' \
        -passin pass:"${pw}" -key QcFMPSub.key -out QcFMPSub.csr
    openssl ca -config opensslroot.cfg -extensions v3_ca -batch \
        -in QcFMPSub.csr -days 3650 -out QcFMPSub.crt -cert QcFMPRoot.crt \
        -passin pass:"${pw}" -keyfile QcFMPRoot.key
    openssl x509 -in QcFMPSub.crt -outform PEM -out QcFMPSub.pub.pem

    info "Generating Leaf signing certificate..."
    openssl genrsa -aes256 -passout pass:"${pw}" -out QcFMPCert.key 2048
    openssl req -new -config opensslroot.cfg \
        -subj '/CN=OEM FMP Signer/O=FMP/OU=OEM Key/L=Test/ST=Test/C=US' \
        -passin pass:"${pw}" -key QcFMPCert.key -out QcFMPCert.csr
    openssl ca -config opensslroot.cfg -extensions usr_cert -batch \
        -in QcFMPCert.csr -days 3650 -out QcFMPCert.crt -cert QcFMPSub.crt \
        -passin pass:"${pw}" -keyfile QcFMPSub.key
    openssl rsa -in QcFMPCert.key -passin pass:"${pw}" -out QcFMPCert.unenc.key
    cat QcFMPCert.crt QcFMPCert.unenc.key > QcFMPCert.pem
    chmod 600 QcFMPCert.pem QcFMPCert.unenc.key QcFMPCert.key QcFMPSub.key QcFMPRoot.key

    cd - >/dev/null
    info "TEST cert chain generated -> ${CERT_DIR}"
    _step_ok "certs"
else
    error "Certificate chain incomplete and GENERATE_TEST_CERTS=0. Place these files in ${CERT_DIR}:
    QcFMPCert.pem     (leaf cert + unencrypted private key, PEM)
    QcFMPRoot.pub.pem (root CA public cert, PEM)
    QcFMPSub.pub.pem  (sub CA public cert, PEM)
    QcFMPRoot.cer     (root CA cert, DER)
  Or re-run without --no-gen-certs to auto-generate a test chain."
fi

[ -f "${CERT_PEM}" ]      || error "Certificate not found: ${CERT_PEM}"
[ -f "${CERT_ROOT_PEM}" ] || error "Root cert not found: ${CERT_ROOT_PEM}"
[ -f "${CERT_SUB_PEM}" ]  || error "Sub cert not found: ${CERT_SUB_PEM}"
[ -f "${CERT_ROOT_CER}" ] || error "Root DER cert not found: ${CERT_ROOT_CER}"

mkdir -p "${BUILD_DIR}"

# ---- Patch uefi_dtbs (elf or xz) ---------------------------------------

_step_start "dtbpatch"
# Only one of uefi_dtbs.elf or uefi_dtbs.xz may be present in Images/
if [[ -f "${UEFI_DTBS_ELF}" && -f "${UEFI_DTBS_XZ}" ]]; then
    error "Both uefi_dtbs.elf and uefi_dtbs.xz found in ${IMAGES_DIR}. Only one is allowed."
elif [[ -f "${UEFI_DTBS_ELF}" ]]; then
    : # ELF already set
elif [[ -f "${UEFI_DTBS_XZ}" ]]; then
    info "--- Found uefi_dtbs.xz, decompressing ---"
    UEFI_DTBS_ELF="${BUILD_DIR}/uefi_dtbs.elf"
    xz -dkf "${UEFI_DTBS_XZ}" --stdout > "${UEFI_DTBS_ELF}"
    info "Decompressed: ${UEFI_DTBS_XZ} -> ${UEFI_DTBS_ELF}"
else
    warn "Neither uefi_dtbs.elf nor uefi_dtbs.xz found in ${IMAGES_DIR}, skipping patch"
    _step_skip "dtbpatch"
fi

if [[ "${_STEP_STATUS[dtbpatch]}" != "SKIPPED" ]]; then
    info "--- Patching uefi_dtbs.elf ---"
    info "  Input : ${UEFI_DTBS_ELF}"
    info "  Output: ${UEFI_DTBS_OUT}"
    ${QCT} patch-capsule-cert "${UEFI_DTBS_ELF}" "${CERT_ROOT_CER}" "${UEFI_DTBS_OUT}"
    info "Patched ELF: ${UEFI_DTBS_OUT}"
    _step_ok "dtbpatch"
fi

# ---- Compress patched uefi_dtbs.elf ------------------------------------

_step_start "dtbxz"
if [[ -f "${UEFI_DTBS_OUT}" ]]; then
    info "--- Compressing ${UEFI_DTBS_OUT} -> ${UEFI_DTBS_XZ_OUT} ---"
    xz -c "${UEFI_DTBS_OUT}" > "${UEFI_DTBS_XZ_OUT}"
    info "Compressed : ${UEFI_DTBS_XZ_OUT}"
    ls -lh "${UEFI_DTBS_XZ_OUT}"
    _step_ok "dtbxz"
else
    _step_skip "dtbxz"
fi

# ---- Patch xbl_config ----------------------------------------

_step_start "xblpatch"
if [[ -f "${XBL_CONFIG_ELF}" ]]; then
    info "--- Patching xbl_config.elf ---"
    info "  Input : ${XBL_CONFIG_ELF}"
    info "  Output: ${XBL_CONFIG_OUT}"
    ${QCT} patch-capsule-cert "${XBL_CONFIG_ELF}" "${CERT_ROOT_CER}" "${XBL_CONFIG_OUT}"
    cp "${XBL_CONFIG_OUT}" "${BUILD_DIR}/xbl_config.elf"
    info "Patched ELF: ${XBL_CONFIG_OUT} (also copied to ${BUILD_DIR}/xbl_config.elf)"
    _step_ok "xblpatch"
else
    info "--- xbl_config.elf not found in ${IMAGES_DIR}, skipping ---"
    _step_skip "xblpatch"
fi

cd "${BUILD_DIR}"

# ---- Step 1: Generate firmware version file -------------------------

_step_start "sysfw"
info "--- Step 1: Generating SYSFW_VERSION.bin ---"
${QCT} sysfw-version-create \
    -Gen \
    -FwVer "${FW_VERSION}" \
    -LFwVer "${LOWEST_FW_VER}" \
    -O SYSFW_VERSION.bin

info "Firmware version contents:"
${QCT} sysfw-version-create --PrintAll SYSFW_VERSION.bin
_step_ok "sysfw"

# ---- Step 2: FvUpdate.xml (provided or auto-generated) --------------

_step_start "fvxml"
if [[ -n "${FVUPDATE_XML}" ]]; then
    info "--- Step 2: Using user-provided FvUpdate.xml ---"
    cp "${FVUPDATE_XML}" FvUpdate.xml
    info "Copied: ${FVUPDATE_XML} -> ${BUILD_DIR}/FvUpdate.xml"
else
    info "--- Step 2: Generating FvUpdate.xml (TARGET=${TARGET}, STORAGE=${STORAGE}) ---"
    ptool_arg=()
    [[ -n "${PTOOL_PATH}" ]] && ptool_arg=(--ptool-path "${PTOOL_PATH}")
    only_arg=()
    [[ -n "${FVUPDATE_ONLY}" ]] && only_arg=(--only ${FVUPDATE_ONLY})
    # update-fv-xml writes FvUpdate.xml into the current directory (BUILD_DIR).
    ${QCT} update-fv-xml -T "${TARGET}" -S "${STORAGE}" "${ptool_arg[@]}" "${only_arg[@]}"
    [ -f FvUpdate.xml ] || error "update-fv-xml did not produce FvUpdate.xml"
    info "Generated: ${BUILD_DIR}/FvUpdate.xml"
fi
_step_ok "fvxml"

# ---- Step 3: Create Firmware Volume ---------------------------------

_step_start "fv"
info "--- Step 3: Creating Firmware Volume (firmware.fv) ---"
${QCT} fv-create firmware.fv \
    -FvType SYS_FW \
    FvUpdate.xml \
    SYSFW_VERSION.bin \
    "${BUILD_DIR}" \
    "${IMAGES_DIR}"
_step_ok "fv"

# ---- Step 4: Update JSON parameters ---------------------------------

_step_start "json"
info "--- Step 4: Updating config.json ---"
${QCT} update-json \
    -j config.json \
    -f SYS_FW \
    -b SYSFW_VERSION.bin \
    -pf firmware.fv \
    -p "${CERT_PEM}" \
    -x "${CERT_ROOT_PEM}" \
    -oc "${CERT_SUB_PEM}" \
    -g "${FMP_GUID}"
_step_ok "json"

# ---- Step 5: Generate capsule file ----------------------------------

_step_start "capsule"
info "--- Step 5: Generating capsule file ---"
${QCT} generate-capsule \
    -e \
    -j config.json \
    -o "${CAPSULE_NAME}" \
    --capflag PersistAcrossReset \
    -v

info "--- Capsule info ---"
${QCT} generate-capsule --dump-info "${CAPSULE_NAME}"

ls -lh "${BUILD_DIR}/${CAPSULE_NAME}"
_step_ok "capsule"
