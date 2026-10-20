"""Who ran a scan: the person at the computer and the Google account used."""

from __future__ import annotations

import getpass
import platform
import socket
import sys
from dataclasses import dataclass


@dataclass
class RunIdentity:
    full_name: str = ""       # Windows display name (domain), if available
    login: str = ""           # OS login name
    machine: str = ""         # computer name
    os: str = ""              # e.g. "Windows 11"
    google_account: str = ""  # identity BigQuery saw (SESSION_USER), e.g. jane@corp.com
    auth_method: str = ""     # "Google login" or "Service account key"

    @property
    def display(self) -> str:
        """e.g. 'Jane Doe (jdoe) on LAPTOP-42'."""
        who = self.full_name or self.login or "unknown user"
        if self.full_name and self.login and self.login.lower() not in self.full_name.lower():
            who += f" ({self.login})"
        return f"{who} on {self.machine}" if self.machine else who


def local_identity(auth_method: str = "") -> RunIdentity:
    try:
        login = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some sandboxes
        login = ""
    return RunIdentity(full_name=_windows_full_name(), login=login,
                       machine=socket.gethostname(), os=_os_name(), auth_method=auth_method)


def _os_name() -> str:
    if sys.platform == "win32":
        release = platform.release()
        try:
            # Windows 11 still reports release "10"; the build number tells them apart.
            if release == "10" and int(platform.version().split(".")[2]) >= 22000:
                release = "11"
        except (IndexError, ValueError):
            pass
        return f"Windows {release}"
    return f"{platform.system()} {platform.release()}".strip()


def _windows_full_name() -> str:
    """Display name from the domain controller; empty off VPN or off Windows."""
    if sys.platform != "win32":
        return ""
    try:
        import win32api
        import win32net

        info = win32net.NetUserGetInfo(win32net.NetGetAnyDCName(), win32api.GetUserName(), 2)
        return info.get("full_name") or ""
    except Exception:  # noqa: BLE001 - off VPN / pywin32 missing
        pass
    try:
        # Works without pywin32 and without reaching the domain controller.
        import ctypes

        size = ctypes.c_ulong(0)
        secur32 = ctypes.windll.secur32
        secur32.GetUserNameExW(3, None, ctypes.byref(size))  # 3 = NameDisplay
        buf = ctypes.create_unicode_buffer(size.value)
        if secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def google_account(client, timeout: float = 30.0) -> str:
    """Email BigQuery sees for these credentials (free: reads no table data)."""
    job = client.query("SELECT SESSION_USER() AS who")
    rows = list(job.result(timeout=timeout))
    return str(rows[0]["who"]) if rows else ""
