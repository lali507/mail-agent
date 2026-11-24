"""Desktop pop-up: a native Windows toast plus opening the review page in the browser."""
from __future__ import annotations

import base64
import logging
import subprocess
import sys
import webbrowser

log = logging.getLogger(__name__)

_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode('__TITLE__')) > $null
$t.Item(1).AppendChild($xml.CreateTextNode('__BODY__')) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def _toast_windows(title: str, body: str) -> None:
    script = _PS_TOAST.replace("__TITLE__", title.replace("'", "''")).replace("__BODY__", body.replace("'", "''"))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()  # avoids quoting/encoding issues
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        timeout=20, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def notify(title: str, body: str, url: str | None = None) -> None:
    try:
        if sys.platform == "win32":
            _toast_windows(title, body)
    except Exception:
        log.warning("toast failed", exc_info=True)
    if url:
        webbrowser.open(url, new=2)
