"""Post-download verification (ADR-003 §D): digest, malware, format checks.

Sandbox downloads are promoted to the real cache share only after passing
every check; failures are quarantined, never served or recorded.
"""

import hashlib
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

from frogcache.cache import download_atomic, safe_join

GGUF_MAGIC = b"GGUF"


def sha256_file(path: Path) -> str:
    """Compute the sha256 hex digest of a file (1 MiB chunks).

    Args:
        path: file to hash.
    Returns:
        Hex digest.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_digest(path: Path, expected_sha256: str, required: bool) -> bool:
    """Verify the file's sha256 against the upstream digest (policy inside).

    Args:
        path: file to check.
        expected_sha256: upstream digest ('' = none provided).
        required: True for digest-capable sources (pypi, huggingface,
            github-releases assets) — a missing digest FAILS; False for
            legacy constructed-URL sources (github-compat) — a missing
            digest passes but is reported unverified.
    Returns:
        True when verified against the upstream digest, False when not
        verifiable (only possible with required=False).
    Raises:
        RuntimeError: on sha256 mismatch, or when required and no digest given.
    """
    digest = sha256_file(path)
    if not expected_sha256:
        if required:
            raise RuntimeError(
                f"no upstream sha256 for {path.name} (required for this source type)"
            )
        return False
    if digest != expected_sha256.lower():
        raise RuntimeError(
            f"sha256 mismatch for {path.name}: expected {expected_sha256}, got {digest}"
        )
    return True


def scan_malware(path: Path, timeout: int = 600) -> None:
    """ClamAV scan of one file; raises on detection or missing scanner.

    Args:
        path: file in the sandbox to scan.
        timeout: clamscan timeout in seconds.
    Raises:
        RuntimeError: clamscan missing, or malware detected.
    """
    try:
        result = subprocess.run(
            ["clamscan", "--no-summary", str(path)],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as err:
        raise RuntimeError(
            "clamscan not available — install clamav or mount the scanner sidecar"
        ) from err
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"clamscan timed out on {path.name}") from err
    if result.returncode != 0:
        raise RuntimeError(f"clamscan detected malware in {path.name}")


def _wheel_has_metadata(path: Path) -> bool:
    """True if the wheel zip contains dist-info METADATA and WHEEL files."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    return any(n.endswith(".dist-info/METADATA") for n in names) and any(
        n.endswith(".dist-info/WHEEL") for n in names
    )


def check_format(path: Path) -> None:
    """Type-aware format check ('working'); raises RuntimeError when invalid.

    .whl = valid zip with dist-info METADATA+WHEEL; .json = parses;
    .gguf = GGUF magic; other types pass (digest + AV checks still apply).

    Args:
        path: file in the sandbox.
    Raises:
        RuntimeError: format check failed.
    """
    name = path.name.lower()
    if name.endswith(".part"):
        name = name[: -len(".part")]  # staging suffix: check the real filename
    if name.endswith(".whl"):
        ok = zipfile.is_zipfile(path) and _wheel_has_metadata(path)
    elif name.endswith(".json"):
        try:
            json.loads(path.read_text())
            ok = True
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            ok = False
    elif name.endswith(".gguf"):
        with open(path, "rb") as fh:
            ok = fh.read(4) == GGUF_MAGIC
    else:
        ok = True
    if not ok:
        raise RuntimeError(f"format check failed for {path.name}")


def _quarantine(part: Path, quarantine: Path | None) -> None:
    """Move a failed download to quarantine (or delete if no quarantine dir)."""
    if quarantine is not None:
        quarantine.mkdir(parents=True, exist_ok=True)
        shutil.move(str(part), quarantine / part.name)
    else:
        part.unlink(missing_ok=True)


def verify_and_promote(
    part: Path,
    dest: Path,
    expected_sha256: str = "",
    quarantine: Path | None = None,
    digest_required: bool = False,
) -> tuple[bool, str, bool]:
    """Verify a sandbox .part file (digest → AV → format), then promote to dest.

    One try/except: any check raising RuntimeError quarantines the file.
    The destination is re-hashed after the move (cross-fs moves copy+unlink).

    Args:
        part: downloaded file inside the sandbox (not servable).
        dest: final path on the real cache share.
        expected_sha256: upstream-provided digest, '' if unknown.
        quarantine: quarantine directory inside the sandbox for failures.
        digest_required: True for digest-capable sources (see verify_digest).
    Returns:
        (ok, sha256, digest_verified); ok=False -> quarantined, nothing recorded.
    """
    dest = safe_join(str(dest.parent.parent), str(dest.relative_to(dest.parent.parent)))  # no-op guard
    part = safe_join(str(part.parent), part.name)
    digest = sha256_file(part)
    try:
        digest_verified = verify_digest(part, expected_sha256, digest_required)
        scan_malware(part)
        check_format(part)
    except RuntimeError:
        _quarantine(part, quarantine)
        return False, digest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # copy to a hidden temp ON THE DESTINATION filesystem, re-verify, then
    # atomically replace — consumers never see a partial file (cross-fs move
    # would copy directly into the served directory)
    tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copyfile(part, tmp)
        if sha256_file(tmp) != digest:
            raise RuntimeError(f"copy of {dest.name} failed re-hash (sandbox digest mismatch)")
        if digest_verified and sha256_file(tmp) != expected_sha256.lower():
            raise RuntimeError(f"copy of {dest.name} failed digest re-check")
        os.replace(tmp, dest)
    except (OSError, RuntimeError):
        tmp.unlink(missing_ok=True)
        _quarantine(part, quarantine)
        return False, digest, False
    part.unlink(missing_ok=True)
    return True, digest, digest_verified


def fetch_and_promote(
    url: str,
    dest: Path,
    sandbox_dir: Path,
    timeout: int,
    expected_sha256: str = "",
    quarantine: Path | None = None,
    digest_required: bool = False,
) -> tuple[bool, str, bool]:
    """Download url into the sandbox, verify, then promote to dest (ADR-003 §D).

    Args:
        url: absolute download URL (constructed against known hosts only).
        dest: final path on the real cache share.
        sandbox_dir: air-gapped staging dir (mounted only into the updater).
        timeout: download timeout in seconds.
        expected_sha256: upstream digest, '' if unknown.
        quarantine: quarantine dir inside the sandbox.
        digest_required: see verify_digest.
    Returns:
        Same as verify_and_promote.
    Raises:
        RuntimeError: download failed (nothing promoted).
    """
    stage = sandbox_dir / uuid.uuid4().hex  # unique per fetch — no shared staging names
    stage.mkdir(parents=True, exist_ok=True)
    part, _ = download_atomic(url, str(stage), f"{dest.name}.part", timeout)
    try:
        return verify_and_promote(part, dest, expected_sha256, quarantine, digest_required)
    finally:
        shutil.rmtree(stage, ignore_errors=True)  # sandbox stays temporary
