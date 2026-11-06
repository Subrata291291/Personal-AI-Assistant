import argparse
import errno
import math
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable

from sqlalchemy.engine import URL, make_url


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

SUPPORTED_SCHEMES = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "postgresql+psycopg": "postgresql+psycopg",
}

_MASKED_INPUT_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Read-MaskedUrl([string] $title) {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $title
    $form.StartPosition = 'CenterScreen'
    $form.Size = New-Object System.Drawing.Size(620, 150)
    $form.MinimizeBox = $false
    $form.MaximizeBox = $false
    $form.FormBorderStyle = 'FixedDialog'

    $label = New-Object System.Windows.Forms.Label
    $label.Text = 'Paste the PostgreSQL URL. Input is masked.'
    $label.AutoSize = $true
    $label.Location = New-Object System.Drawing.Point(12, 12)

    $input = New-Object System.Windows.Forms.TextBox
    $input.PasswordChar = '*'
    $input.Size = New-Object System.Drawing.Size(575, 24)
    $input.Location = New-Object System.Drawing.Point(12, 38)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = 'Continue'
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $ok.Location = New-Object System.Drawing.Point(410, 72)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = 'Cancel'
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $cancel.Location = New-Object System.Drawing.Point(500, 72)

    $form.Controls.Add($label)
    $form.Controls.Add($input)
    $form.Controls.Add($ok)
    $form.Controls.Add($cancel)
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel

    $dialogResult = $form.ShowDialog()
    if ($dialogResult -ne [System.Windows.Forms.DialogResult]::OK) {
        $input.Clear()
        $form.Dispose()
        return $null
    }

    $value = $input.Text
    $input.Clear()
    $form.Dispose()
    return $value
}

$comparisonUrl = Read-MaskedUrl 'Comparison PostgreSQL URL'
if ($null -eq $comparisonUrl) {
    [Environment]::Exit(2)
}
[Console]::Out.WriteLine($comparisonUrl)
$comparisonUrl = $null
"""

_OUTPUT_FIELDS = (
    "URL_FORMAT_VALID",
    "SQLALCHEMY_DRIVER",
    "PORT",
    "USERNAME_PRESENT",
    "PASSWORD_PRESENT",
    "DATABASE_PRESENT",
    "ENDPOINT_CLASS",
    "DNS_RESOLUTION",
)


def _endpoint_class(host: str | None, port: int) -> str:
    if not host:
        return "OTHER"

    normalized_host = host.rstrip(".").lower()
    labels = normalized_host.split(".")
    if (
        len(labels) == 4
        and labels[0] == "db"
        and labels[1]
        and labels[2:] == ["supabase", "co"]
        and port == 5432
    ):
        return "DIRECT"

    if normalized_host.endswith(".pooler.supabase.com"):
        if port == 5432:
            return "SHARED_SESSION_POOLER"
        if port == 6543:
            return "SHARED_TRANSACTION_POOLER"

    return "OTHER"


def _dns_resolution(
    host: str,
    resolver: Callable[..., object] = socket.getaddrinfo,
) -> str:
    try:
        resolver(host, None, type=socket.SOCK_STREAM)
    except Exception:
        return "FAILED"
    return "SUCCESS"


def _inspect_url(raw_url: str) -> dict[str, str]:
    url: URL | None = None
    try:
        url = make_url(raw_url)
        normalized_driver = SUPPORTED_SCHEMES.get(url.drivername)
        if normalized_driver is None:
            raise ValueError
        if url.drivername != normalized_driver:
            url = url.set(drivername=normalized_driver)
        host = url.host
        if not host:
            raise ValueError
        port = url.port or 5432
    except Exception:
        result = {
            "URL_FORMAT_VALID": "False",
            "SQLALCHEMY_DRIVER": "unavailable",
            "PORT": "unavailable",
            "USERNAME_PRESENT": str(bool(url and url.username)),
            "PASSWORD_PRESENT": str(bool(url and url.password)),
            "DATABASE_PRESENT": str(bool(url and url.database)),
            "ENDPOINT_CLASS": "OTHER",
            "DNS_RESOLUTION": "FAILED",
        }
        url = None
        return result

    result = {
        "URL_FORMAT_VALID": "True",
        "SQLALCHEMY_DRIVER": url.drivername,
        "PORT": str(port),
        "USERNAME_PRESENT": str(bool(url.username)),
        "PASSWORD_PRESENT": str(bool(url.password)),
        "DATABASE_PRESENT": str(bool(url.database)),
        "ENDPOINT_CLASS": _endpoint_class(host, port),
        "DNS_RESOLUTION": _dns_resolution(host),
    }
    url = None
    host = None
    return result


def _powershell_environment() -> dict[str, str]:
    required = {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "PSMODULEPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in required
    }


def _load_current_configured_url() -> str:
    from app.core.config import settings

    database_url = settings.DATABASE_URL
    if database_url is None:
        raise RuntimeError
    return database_url.get_secret_value()


def _tcp_reachability(
    raw_url: str,
    timeout: float = 5.0,
    connector: Callable[..., socket.socket] = socket.create_connection,
) -> tuple[str, str | None]:
    connection: socket.socket | None = None
    try:
        url = make_url(raw_url)
        if url.drivername not in SUPPORTED_SCHEMES or not url.host:
            return "FAILED", "OTHER"
        host = url.host
        port = url.port or 5432
        url = None
        connection = connector((host, port), timeout=timeout)
        return "SUCCESS", None
    except Exception as exc:
        if isinstance(exc, socket.gaierror):
            category = "DNS"
        elif isinstance(exc, (TimeoutError, socket.timeout)) or getattr(
            exc, "errno", None
        ) == errno.ETIMEDOUT:
            category = "TIMEOUT"
        elif isinstance(exc, ConnectionRefusedError) or getattr(
            exc, "errno", None
        ) == errno.ECONNREFUSED:
            category = "CONNECTION_REFUSED"
        else:
            category = "OTHER"
        return "FAILED", category
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError
    return timeout


def _read_masked_url() -> str:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        raise RuntimeError
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-STA",
            "-Command",
            _MASKED_INPUT_SCRIPT,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_powershell_environment(),
        check=False,
    )
    if completed.returncode != 0:
        completed.stdout = ""
        completed.stderr = ""
        raise RuntimeError

    lines = completed.stdout.splitlines()
    completed.stdout = ""
    completed.stderr = ""
    if len(lines) != 1 or not lines[0]:
        lines.clear()
        raise RuntimeError
    value = lines[0]
    lines.clear()
    return value


def _print_result(label: str, values: dict[str, str]) -> None:
    print(label)
    for field in _OUTPUT_FIELDS:
        print(f"{field}={values[field]}")


def _print_tcp_result(result: tuple[str, str | None]) -> None:
    reachability, error_category = result
    print(f"TCP_REACHABILITY={reachability}")
    if error_category is not None:
        print(f"ERROR_CATEGORY={error_category}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-tcp",
        action="store_true",
        help="Also test TCP reachability for the current configured URL.",
    )
    parser.add_argument(
        "--tcp-timeout",
        type=_positive_timeout,
        default=5.0,
        help="TCP connection timeout in seconds (default: 5).",
    )
    args = parser.parse_args()

    current_url: str | None = None
    comparison_url: str | None = None
    current_result: dict[str, str] | None = None
    comparison_result: dict[str, str] | None = None
    try:
        current_url = _load_current_configured_url()
        comparison_url = _read_masked_url()
        current_result = _inspect_url(current_url)
        comparison_result = _inspect_url(comparison_url)
        _print_result("CURRENT_CONFIGURED_URL", current_result)
        _print_result("COMPARISON_URL", comparison_result)
        if args.test_tcp:
            _print_tcp_result(
                _tcp_reachability(current_url, timeout=args.tcp_timeout)
            )
        return 0
    except Exception:
        print(
            "Masked URL input or local diagnostic failed.",
            file=sys.stderr,
        )
        return 1
    finally:
        current_url = None
        comparison_url = None
        current_result = None
        comparison_result = None


if __name__ == "__main__":
    raise SystemExit(main())
