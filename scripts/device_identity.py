#!/usr/bin/env python3
"""Keep one device identity on the local computer, never on shared storage."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import uuid
from typing import Any, Dict, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
VOLUMES_ROOT = Path("/Volumes")
VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
VALID_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
VALID_NORMALIZED_VOLUME_IDENTITY = re.compile(r"^sha256:[0-9A-Fa-f]{64}$")


class DeviceIdentityError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def knowledge_base_id(root: Path = ROOT) -> str:
    manifest = root / "MANIFEST.json"
    value = "ai-knowledge"
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and parsed.get("knowledge_base_id"):
            value = str(parsed["knowledge_base_id"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return normalized[:128] or "ai-knowledge"


def local_state_path(root: Path = ROOT, home: Optional[Path] = None) -> Path:
    override = os.environ.get("AI_KNOWLEDGE_DEVICE_STATE", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise DeviceIdentityError("AI_KNOWLEDGE_DEVICE_STATE must be absolute")
        return path
    local_home = (home or Path.home()).expanduser()
    if platform.system() == "Windows":
        configured = os.environ.get("LOCALAPPDATA", "").strip() if home is None else ""
        state_root = (
            Path(configured).expanduser()
            if configured
            else local_home / "AppData" / "Local"
        )
        return (
            state_root
            / "AI-Knowledge"
            / knowledge_base_id(root)
            / "device.json"
        )
    return (
        local_home
        / "Library"
        / "Application Support"
        / "AI-Knowledge"
        / knowledge_base_id(root)
        / "device.json"
    )


def platform_material() -> str:
    system = platform.system()
    if system == "Darwin":
        try:
            output = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
            if match:
                try:
                    # Canonicalize case without changing the identifier used by
                    # the first installer release, which hashed Apple's usual
                    # uppercase IOPlatformUUID representation.
                    value = str(uuid.UUID(match.group(1))).upper()
                except (ValueError, AttributeError) as error:
                    raise DeviceIdentityError("Darwin IOPlatformUUID is invalid") from error
                return "platform-uuid:" + value
        except DeviceIdentityError:
            raise
        except (OSError, subprocess.CalledProcessError) as error:
            raise DeviceIdentityError("cannot determine Darwin IOPlatformUUID") from error
        raise DeviceIdentityError("cannot determine Darwin IOPlatformUUID")
    if system == "Linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if re.fullmatch(r"[0-9A-Fa-f]{32}", value) and value != "0" * 32:
                return "linux-machine-id:" + value.lower()
        raise DeviceIdentityError("cannot determine Linux machine-id")
    if system == "Windows":
        try:
            value = windows_machine_guid()
        except (OSError, ValueError, ImportError) as error:
            raise DeviceIdentityError("cannot determine Windows MachineGuid") from error
        return "windows-machine-guid:" + value
    # Unsupported platforms have no defined machine identity contract here.
    # Keep the fallback explicit so Darwin/Linux can never silently downgrade.
    return "unsupported-platform:%s:hostname:%s" % (
        system or "unknown",
        socket.gethostname() or "unknown-device",
    )


def platform_fingerprint() -> str:
    """Return a non-reversible binding for the current computer identity."""
    return hashlib.sha256(platform_material().encode("utf-8")).hexdigest()


def windows_machine_guid(registry: Any = None) -> str:
    """Read and normalize MachineGuid without persisting the raw identifier."""

    if registry is None:
        import winreg as registry  # type: ignore[import-not-found]
    access = registry.KEY_READ | getattr(registry, "KEY_WOW64_64KEY", 0)
    with registry.OpenKey(
        registry.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        access,
    ) as key:
        raw, _value_type = registry.QueryValueEx(key, "MachineGuid")
    return str(uuid.UUID(str(raw).strip().strip("{}"))).lower()


def derived_id() -> str:
    return platform_fingerprint()[:16]


def normalize_volume_identity(value: str) -> str:
    raw = str(value).strip()
    if raw.startswith("sha256:") and len(raw) == 71:
        return raw
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def marker_volume_identity(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        raw = value.get("volume_uuid") or value.get("VolumeUUID")
        return normalize_volume_identity(str(raw)) if raw else None
    return normalize_volume_identity(str(value)) if value else None


def mounted_volume_identity(mount: Path) -> str:
    """Return a stable, non-secret identity for a mounted volume."""
    executable = shutil.which("diskutil")
    if executable is None and Path("/usr/sbin/diskutil").is_file():
        executable = "/usr/sbin/diskutil"
    if executable:
        result = subprocess.run(
            [executable, "info", "-plist", str(mount)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            try:
                payload = plistlib.loads(result.stdout)
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                value = payload.get("VolumeUUID") or payload.get("APFSVolumeUUID")
                if value:
                    return normalize_volume_identity(str(value))
    # Non-macOS tests and unusual mounts still get a runtime mount binding.  On
    # macOS APFS the VolumeUUID path above is expected and remains stable.
    return normalize_volume_identity("st_dev:%d" % mount.stat().st_dev)


def validate_cold_data_root(
    root: Path = ROOT,
    home: Optional[Path] = None,
    require_bound_volume: bool = True,
) -> Dict[str, Any]:
    """Fail closed for shared cold storage while allowing explicit local staging."""
    data = root / ".kb-data"
    if not data.is_symlink():
        if data.is_dir():
            state = read_state(root, home)
            if state and state.get("cold_storage_mode") == "external":
                return {
                    "valid": False,
                    "mode": "local-staging",
                    "reason": "external-cold-link-was-replaced-by-local-directory",
                }
            return {"valid": True, "mode": "local-staging", "path": str(data)}
        return {"valid": False, "mode": "absent", "reason": "cold-data-unavailable"}
    target = data.resolve(strict=False)
    if not target.is_dir() or target.is_symlink():
        return {"valid": False, "mode": "external", "reason": "cold-data-unavailable"}
    try:
        relative = target.relative_to(VOLUMES_ROOT)
        mount = VOLUMES_ROOT / relative.parts[0]
    except (ValueError, IndexError):
        return {"valid": False, "mode": "external", "reason": "cold-data-not-under-volumes"}
    if not os.path.ismount(str(mount)):
        return {"valid": False, "mode": "external", "reason": "cold-data-volume-not-mounted"}
    marker = target.with_name(target.name + ".identity.json")
    if not marker.is_file() or marker.is_symlink():
        return {"valid": False, "mode": "external", "reason": "cold-data-identity-marker-missing"}
    try:
        identity = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"valid": False, "mode": "external", "reason": "cold-data-identity-marker-invalid"}
    if not isinstance(identity, dict):
        return {"valid": False, "mode": "external", "reason": "cold-data-identity-marker-invalid"}
    if identity.get("format") != "ai-knowledge-cold-data-v1":
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-identity-marker-format-mismatch",
        }
    expected_kb = knowledge_base_id(root)
    if identity.get("knowledge_base_id") != expected_kb:
        return {"valid": False, "mode": "external", "reason": "cold-data-knowledge-base-mismatch"}
    if identity.get("data_directory") != target.name:
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-identity-marker-data-directory-mismatch",
        }
    manifest_sha = identity.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or not VALID_SHA256.fullmatch(manifest_sha):
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-identity-marker-manifest-invalid",
        }
    stored_volume = identity.get("volume_identity")
    marker_uuid = (
        stored_volume.get("volume_uuid")
        if isinstance(stored_volume, Mapping)
        else None
    )
    if (
        not isinstance(marker_uuid, str)
        or not marker_uuid.strip()
        or marker_uuid != marker_uuid.strip()
    ):
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-volume-identity-invalid",
        }
    marker_volume = normalize_volume_identity(marker_uuid)
    if not VALID_NORMALIZED_VOLUME_IDENTITY.fullmatch(marker_volume):
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-volume-identity-invalid",
        }
    try:
        live_uuid = mounted_volume_identity(mount)
    except (OSError, TypeError, ValueError):
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-volume-identity-unavailable",
        }
    if not isinstance(live_uuid, str) or not live_uuid.strip():
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-volume-identity-unavailable",
        }
    live_volume = normalize_volume_identity(live_uuid)
    if not VALID_NORMALIZED_VOLUME_IDENTITY.fullmatch(live_volume):
        return {
            "valid": False,
            "mode": "external",
            "reason": "cold-data-volume-identity-unavailable",
        }
    if marker_volume != live_volume:
        return {"valid": False, "mode": "external", "reason": "cold-data-volume-identity-mismatch"}
    if require_bound_volume:
        state = read_state(root, home)
        bound_volume = state.get("cold_volume_identity") if state else None
        if not isinstance(bound_volume, str) or bound_volume != live_volume:
            return {"valid": False, "mode": "external", "reason": "cold-data-volume-not-bound-to-device"}
    return {
        "valid": True,
        "mode": "external",
        "path": str(target),
        "mount": str(mount),
        "knowledge_base_id": expected_kb,
        "volume_identity": live_volume,
        "identity_marker": str(marker),
    }


def validate_device_id(value: str) -> str:
    normalized = str(value).strip()
    if not VALID_ID.fullmatch(normalized):
        raise DeviceIdentityError("invalid device id")
    return normalized


def read_state(root: Path = ROOT, home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = local_state_path(root, home)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeviceIdentityError("cannot read local device state") from error
    if not isinstance(parsed, dict) or not parsed.get("id"):
        raise DeviceIdentityError("invalid local device state")
    parsed["id"] = validate_device_id(str(parsed["id"]))
    expected_kb = knowledge_base_id(root)
    state_kb = str(parsed.get("knowledge_base_id") or expected_kb)
    if state_kb != expected_kb:
        raise DeviceIdentityError("local device state belongs to another knowledge base")
    current_fingerprint = platform_fingerprint()
    saved_fingerprint = parsed.get("platform_fingerprint")
    if saved_fingerprint is None:
        # Legacy states can only be upgraded without ambiguity when their id was
        # deterministically derived from this Mac.  A custom id without a bound
        # fingerprint might have been copied from another machine.
        if parsed["id"] != derived_id():
            raise DeviceIdentityError("legacy local device state is not bound to this Mac")
        parsed["identity_upgrade_required"] = True
    elif not isinstance(saved_fingerprint, str) or saved_fingerprint != current_fingerprint:
        raise DeviceIdentityError("local device state belongs to another Mac")
    return parsed


def resolve_device_id(
    root: Path = ROOT,
    override: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    requested = override or os.environ.get("KB_SOURCE_DEVICE_ID", "").strip()
    state = read_state(root, home)
    if requested:
        requested_id = validate_device_id(requested)
        if state is not None:
            if requested_id != state["id"]:
                raise DeviceIdentityError("device id override conflicts with the bound local identity")
            return requested_id
        if requested_id != derived_id():
            raise DeviceIdentityError("a new device id must be derived from and bound to this Mac")
        return requested_id
    return state["id"] if state else derived_id()


def atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), 0o600)
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_state(
    root: Path,
    device_id: str,
    name: str,
    home: Optional[Path] = None,
    **extra: Any,
) -> Path:
    path = local_state_path(root, home)
    previous = read_state(root, home)
    normalized_id = validate_device_id(device_id)
    if previous is not None and previous["id"] != normalized_id:
        raise DeviceIdentityError("refusing to replace the bound local device identity")
    if previous is None and normalized_id != derived_id():
        raise DeviceIdentityError("a new device id must be derived from and bound to this Mac")
    payload: Dict[str, Any] = dict(previous or {})
    payload.pop("identity_upgrade_required", None)
    payload.update(
        {
            "format": "ai-knowledge-local-device-v1",
            "knowledge_base_id": knowledge_base_id(root),
            "id": normalized_id,
            "name": str(name),
            "root": str(root.resolve()),
            "platform_fingerprint": platform_fingerprint(),
            "updated_at": utc_now(),
        }
    )
    payload.update(extra)
    atomic_write(path, payload)
    return path
