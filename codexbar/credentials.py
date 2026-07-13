"""OAuth credential capture and DPAPI-backed multi-account vault for CodexBar."""

from __future__ import annotations

import base64
import calendar
import ctypes
from ctypes import wintypes
import json
import os
import threading
import time
from typing import TypedDict

from . import config, diagnostics


class Credentials(TypedDict):
    """OAuth credentials required by the quota endpoints."""

    version: int
    access_token: str
    refresh_token: str
    account_id: str
    id_token: str | None
    captured_at: float
    last_refresh: str | None


class CredentialVault(TypedDict):
    """Encrypted vault payload that can hold multiple accounts."""

    version: int
    active_account_id: str | None
    accounts: dict[str, Credentials]


class AccountSummary(TypedDict):
    """Small account descriptor used by the right-click menu."""

    account_id: str
    label: str
    active: bool


class AuthRequiredError(RuntimeError):
    """No usable OAuth subscription login is available locally."""


class ReloginRequiredError(RuntimeError):
    """The saved refresh token is no longer usable."""


class CredentialVaultError(RuntimeError):
    """The encrypted vault could not be encrypted, decrypted, or validated."""


AUTH_PATH = config.AUTH_PATH
VAULT_PATH = config.VAULT_PATH
LEGACY_VAULT_PATH = config.LEGACY_VAULT_PATH
VAULT_MAGIC = b"CQW1"
CREDENTIAL_VERSION = 1
VAULT_FORMAT_VERSION = 2
DPAPI_ENTROPY = b"CodexBar:v1"
LEGACY_DPAPI_ENTROPY = b"CodexQuotaWidget:v1"
CREDENTIAL_LOCK = threading.RLock()

_IS_WINDOWS = os.name == "nt"
_CRYPT32 = ctypes.windll.crypt32 if _IS_WINDOWS else None
_KERNEL32 = ctypes.windll.kernel32 if _IS_WINDOWS else None


if _IS_WINDOWS:
    class DATA_BLOB(ctypes.Structure):
        """Windows DPAPI buffer descriptor."""

        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


    _CRYPT32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _CRYPT32.CryptProtectData.restype = wintypes.BOOL
    _CRYPT32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _CRYPT32.CryptUnprotectData.restype = wintypes.BOOL
    _KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32.LocalFree.restype = ctypes.c_void_p


def decode_jwt_exp(token: str) -> int | None:
    """Decode the ``exp`` field from a JWT without signature validation."""

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def _data_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
    if not _IS_WINDOWS:
        raise CredentialVaultError("DPAPI is only supported on Windows")
    source, source_buffer = _data_blob(data)
    entropy_blob, entropy_buffer = _data_blob(entropy)
    output = DATA_BLOB()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(source),
        "CodexBar credentials",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    if not ok:
        raise CredentialVaultError(f"DPAPI encrypt failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _KERNEL32.LocalFree(output.pbData)
        _ = source_buffer, entropy_buffer


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    if not _IS_WINDOWS:
        raise CredentialVaultError("DPAPI is only supported on Windows")
    source, source_buffer = _data_blob(data)
    entropy_blob, entropy_buffer = _data_blob(entropy)
    output = DATA_BLOB()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    if not ok:
        raise CredentialVaultError(
            "The saved login could not be decrypted by the current Windows user"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _KERNEL32.LocalFree(output.pbData)
        _ = source_buffer, entropy_buffer


def _normalize_credentials(data: object) -> Credentials:
    if not isinstance(data, dict):
        raise CredentialVaultError("Credential payload is invalid")
    required = ("access_token", "refresh_token", "account_id")
    if any(not isinstance(data.get(key), str) or not data[key] for key in required):
        raise CredentialVaultError(
            "Credentials are missing access_token, refresh_token, or account_id"
        )
    return Credentials(
        version=CREDENTIAL_VERSION,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        account_id=data["account_id"],
        id_token=data.get("id_token") if isinstance(data.get("id_token"), str) else None,
        captured_at=float(data.get("captured_at", time.time())),
        last_refresh=(
            data.get("last_refresh") if isinstance(data.get("last_refresh"), str) else None
        ),
    )


def _empty_vault() -> CredentialVault:
    return CredentialVault(
        version=VAULT_FORMAT_VERSION,
        active_account_id=None,
        accounts={},
    )


def _wrap_single_credential(credentials: Credentials) -> CredentialVault:
    return CredentialVault(
        version=VAULT_FORMAT_VERSION,
        active_account_id=credentials["account_id"],
        accounts={credentials["account_id"]: credentials},
    )


def _normalize_vault(data: object) -> tuple[CredentialVault, bool]:
    """Return a v2 vault and whether the input was an old single-account payload."""

    if not isinstance(data, dict):
        raise CredentialVaultError("Vault payload is invalid")
    if data.get("version") != VAULT_FORMAT_VERSION or "accounts" not in data:
        return _wrap_single_credential(_normalize_credentials(data)), True

    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, dict):
        raise CredentialVaultError("Vault accounts payload is invalid")

    accounts: dict[str, Credentials] = {}
    for account_id, value in raw_accounts.items():
        if not isinstance(account_id, str):
            continue
        credentials = _normalize_credentials(value)
        accounts[credentials["account_id"]] = credentials

    active = data.get("active_account_id")
    if not isinstance(active, str) or active not in accounts:
        active = next(iter(accounts), None)

    return CredentialVault(
        version=VAULT_FORMAT_VERSION,
        active_account_id=active,
        accounts=accounts,
    ), False


def _credentials_from_auth(auth: object) -> Credentials | None:
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(tokens, dict):
        return None
    try:
        return _normalize_credentials(
            {
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "account_id": tokens.get("account_id"),
                "id_token": tokens.get("id_token"),
                "last_refresh": auth.get("last_refresh"),
            }
        )
    except CredentialVaultError:
        return None


def _credentials_match(left: Credentials, right: Credentials) -> bool:
    keys = ("access_token", "refresh_token", "account_id", "id_token")
    return all(left.get(key) == right.get(key) for key in keys)


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            pass
    return 0.0


def _active_credentials_are_newer(active: Credentials, saved: Credentials) -> bool:
    """Prefer fresher credentials when the same account appears in two places."""

    active_exp = decode_jwt_exp(active.get("access_token", "")) or 0
    saved_exp = decode_jwt_exp(saved.get("access_token", "")) or 0
    if active_exp != saved_exp:
        return active_exp > saved_exp
    active_refresh = _timestamp(active.get("last_refresh"))
    saved_refresh = _timestamp(saved.get("last_refresh"))
    if active_refresh != saved_refresh:
        return active_refresh > saved_refresh
    return _timestamp(active.get("captured_at")) > _timestamp(saved.get("captured_at"))


def _read_payload(
    path: str,
    entropies: tuple[bytes, ...],
) -> object | None:
    try:
        with open(path, "rb") as file:
            encrypted = file.read()
    except FileNotFoundError:
        return None
    if not encrypted.startswith(VAULT_MAGIC):
        raise CredentialVaultError("Saved login has an invalid vault header")

    last_error: CredentialVaultError | None = None
    for entropy in entropies:
        try:
            payload = _dpapi_unprotect(encrypted[len(VAULT_MAGIC) :], entropy)
            return json.loads(payload.decode("utf-8"))
        except CredentialVaultError as error:
            last_error = error
        except Exception as error:
            raise CredentialVaultError("Saved login is corrupted") from error
    if last_error:
        raise last_error
    return None


def _write_payload(
    payload: object,
    path: str,
    entropy: bytes = DPAPI_ENTROPY,
) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    encrypted = VAULT_MAGIC + _dpapi_protect(encoded, entropy)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "wb") as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def save_vault(
    vault: CredentialVault,
    path: str | None = None,
    entropy: bytes = DPAPI_ENTROPY,
) -> None:
    """Encrypt and persist the v2 multi-account vault."""

    normalized, _migrated = _normalize_vault(vault)
    _write_payload(normalized, path or VAULT_PATH, entropy)


def load_vault(
    path: str | None = None,
    entropies: tuple[bytes, ...] = (DPAPI_ENTROPY,),
) -> CredentialVault | None:
    """Load a v2 vault, migrating old single-account payloads when possible."""

    path = path or VAULT_PATH
    data = _read_payload(path, entropies)
    if data is None:
        return None
    vault, migrated = _normalize_vault(data)
    if migrated and path == VAULT_PATH and entropies == (DPAPI_ENTROPY,):
        save_vault(vault)
    return vault


def save_credentials(
    credentials: Credentials,
    path: str | None = None,
    entropy: bytes = DPAPI_ENTROPY,
) -> None:
    """Upsert credentials into the active vault, or write a legacy payload to a custom path."""

    normalized = _normalize_credentials(credentials)
    if path is not None or entropy != DPAPI_ENTROPY:
        _write_payload(normalized, path or VAULT_PATH, entropy)
        return

    vault = load_vault() or _empty_vault()
    vault["accounts"][normalized["account_id"]] = normalized
    vault["active_account_id"] = normalized["account_id"]
    save_vault(vault)


def save_refreshed_credentials(credentials: Credentials) -> bool:
    """Update an existing cached account without changing account selection.

    A refresh finishing after the user deleted or switched accounts must not
    re-add the old account or make it active again. An unreadable vault is also
    left untouched so a temporary DPAPI/format problem cannot destroy accounts.
    """

    normalized = _normalize_credentials(credentials)
    with CREDENTIAL_LOCK:
        try:
            vault = load_vault()
        except CredentialVaultError as error:
            diagnostics.log_exception("vault_refresh_save", error)
            return False
        if not vault or normalized["account_id"] not in vault["accounts"]:
            return False
        vault["accounts"][normalized["account_id"]] = normalized
        save_vault(vault)
    return True


def load_credentials(
    path: str | None = None,
    entropies: tuple[bytes, ...] = (DPAPI_ENTROPY,),
) -> Credentials | None:
    """Return the active account credentials from a v2 or legacy vault."""

    vault = load_vault(path, entropies)
    if not vault:
        return None
    active = vault.get("active_account_id")
    if isinstance(active, str):
        return vault["accounts"].get(active)
    return None


def migrate_legacy_vault() -> Credentials | None:
    """Migrate the old CodexQuota vault to the new multi-account CodexBar vault."""

    if os.path.exists(VAULT_PATH) or not os.path.exists(LEGACY_VAULT_PATH):
        return None
    legacy_vault = load_vault(
        path=LEGACY_VAULT_PATH, entropies=(LEGACY_DPAPI_ENTROPY,)
    )
    if legacy_vault:
        save_vault(legacy_vault)
        active = legacy_vault.get("active_account_id")
        if isinstance(active, str):
            return legacy_vault["accounts"].get(active)
    return None


def account_label(account_id: str) -> str:
    """Return a short stable label for account menu items."""

    suffix = account_id[-8:] if len(account_id) > 8 else account_id
    return f"账号 {suffix}"


def list_accounts() -> list[AccountSummary]:
    """List saved accounts for the UI account switcher."""

    vault = load_vault()
    if not vault:
        try:
            migrated = migrate_legacy_vault()
        except CredentialVaultError:
            migrated = None
        vault = load_vault() if migrated else None
    if not vault:
        return []
    active = vault.get("active_account_id")
    return [
        AccountSummary(
            account_id=account_id,
            label=account_label(account_id),
            active=(account_id == active),
        )
        for account_id in sorted(vault["accounts"])
    ]


def set_active_account(account_id: str) -> Credentials:
    """Switch the active account and return its credentials."""

    vault = load_vault()
    if not vault or account_id not in vault["accounts"]:
        raise AuthRequiredError("Saved account was not found")
    vault["active_account_id"] = account_id
    save_vault(vault)
    return vault["accounts"][account_id]


def delete_account(account_id: str | None = None) -> bool:
    """Delete one saved account; if none remains, remove the vault."""

    vault = load_vault()
    if not vault:
        return False
    target = account_id or vault.get("active_account_id")
    if not isinstance(target, str) or target not in vault["accounts"]:
        return False

    del vault["accounts"][target]
    if not vault["accounts"]:
        return clear_credentials()
    if vault.get("active_account_id") == target:
        vault["active_account_id"] = sorted(vault["accounts"])[0]
    save_vault(vault)
    return True


def has_credentials(path: str | None = None) -> bool:
    """Return whether an encrypted vault file exists."""

    if path is not None:
        return os.path.isfile(path)
    return os.path.isfile(VAULT_PATH) or os.path.isfile(LEGACY_VAULT_PATH)


def clear_credentials(path: str | None = None) -> bool:
    """Delete all saved CodexBar account caches."""

    if path is not None:
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    removed = False
    for candidate in (VAULT_PATH, LEGACY_VAULT_PATH):
        try:
            os.remove(candidate)
            removed = True
        except FileNotFoundError:
            pass
    return removed


def _load_auth() -> object:
    with open(AUTH_PATH, encoding="utf-8") as file:
        return json.load(file)


def _active_credentials(vault: CredentialVault | None) -> Credentials | None:
    if not vault:
        return None
    active = vault.get("active_account_id")
    if isinstance(active, str):
        return vault["accounts"].get(active)
    return None


def resolve_credentials() -> Credentials:
    """Resolve the selected OAuth credentials without writing back to auth.json."""

    auth: object = None
    auth_mtime = 0.0
    try:
        auth = _load_auth()
        auth_mtime = os.path.getmtime(AUTH_PATH)
    except (OSError, ValueError, TypeError):
        pass

    active = _credentials_from_auth(auth)
    if active:
        active["captured_at"] = auth_mtime or time.time()
        try:
            vault = load_vault() or _empty_vault()
        except CredentialVaultError as error:
            # Keep using the current OAuth login in memory, but never overwrite
            # an unreadable multi-account vault with a new single-account file.
            diagnostics.log_exception("vault_capture", error)
            return active

        previous_active = vault.get("active_account_id")
        saved = vault["accounts"].get(active["account_id"])
        should_select_auth_account = (
            saved is None
            or not isinstance(previous_active, str)
            or previous_active not in vault["accounts"]
            or previous_active == active["account_id"]
        )
        if (
            not saved
            or active["account_id"] != saved["account_id"]
            or _active_credentials_are_newer(active, saved)
        ):
            vault["accounts"][active["account_id"]] = active
            if should_select_auth_account:
                vault["active_account_id"] = active["account_id"]
            save_vault(vault)
            selected = _active_credentials(vault)
            return selected or active

        if should_select_auth_account:
            vault["active_account_id"] = saved["account_id"]
        save_vault(vault)
        selected = _active_credentials(vault)
        return selected or saved

    try:
        vault = load_vault()
    except CredentialVaultError:
        vault = None
    if not vault:
        try:
            migrated = migrate_legacy_vault()
        except CredentialVaultError as error:
            raise AuthRequiredError(f"Saved login is unusable: {error}") from error
        if migrated:
            return migrated
        try:
            vault = load_vault()
        except CredentialVaultError as error:
            raise AuthRequiredError(f"Saved login is unusable: {error}") from error

    saved = _active_credentials(vault)
    if saved:
        return saved
    if isinstance(auth, dict) and "OPENAI_API_KEY" in auth:
        raise AuthRequiredError(
            "Current login uses API key mode; run codex login --device-auth once to capture a subscription account"
        )
    raise AuthRequiredError(
        "No saved Codex subscription account was found; run codex login --device-auth"
    )
