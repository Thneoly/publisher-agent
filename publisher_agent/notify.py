# -*- coding: utf-8 -*-
"""Windows toast 通知（红队裁定：v1 必带，零依赖零基建）。"""
from __future__ import annotations

import base64
import subprocess

PS_TEMPLATE = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{XML}
'@)
$toast = New-Object Windows.UI.Notifications.ToastNotification($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('publisher-agent').Show($toast)
"""


def toast(title: str, body: str = "") -> bool:
    """弹一条 Windows 通知。失败（无桌面会话等）静默降级——通知不能反过来打断主流程。"""
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{_esc(title)}</text>"
           + (f"<text>{_esc(body)}</text>" if body else "")
           + "</binding></visual></toast>")
    ps = PS_TEMPLATE.replace("{XML}", xml)
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
