"""moz 托盘常驻：浏览器页面关着也能收到主动关心，并弹成 Windows 通知。

职责刻意做窄：判定和措辞都在后端做，这里只负责
① 定时拉待读消息 ② 弹通知 ③ 确认已读 ④ 托盘菜单。
托盘没开也不影响功能——消息会留在队列里，下次打开页面照样补给你。

启动：.venv\\Scripts\\pythonw.exe backend\\tray_app.py
"""

import ctypes
import json
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import pystray
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from winotify import Notification

HERE = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(HERE, "..", ".env"))

API_BASE = os.environ.get("MOZ_API_BASE", "http://127.0.0.1:8000/api")
WEB_URL = os.environ.get("MOZ_WEB_URL", "http://127.0.0.1:3000")
USER_ID = os.environ.get("MOZ_USER_ID", "web_user_001")
ACCESS_KEY = os.environ.get("MOZ_ACCESS_KEY", "")
POLL_SECONDS = 20
APP_ID = "moz.companion"

_state = {"paused": False, "online": False}


def _icon_image(online: bool):
    """画一个托盘小图：在线用品牌橙，掉线变灰，一眼能看出后端在不在。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (212, 120, 92, 255) if online else (176, 165, 154, 255)
    d.ellipse([6, 6, 58, 58], fill=fill)
    d.ellipse([22, 20, 32, 30], fill=(255, 255, 255, 255))
    d.ellipse([38, 20, 48, 30], fill=(255, 255, 255, 255))
    d.arc([18, 26, 46, 50], start=20, end=160, fill=(255, 255, 255, 255), width=4)
    return img


def _get(path):
    req = urllib.request.Request(API_BASE + path, headers={"Content-Type": "application/json"})
    if ACCESS_KEY:
        req.add_header("X-Access-Key", ACCESS_KEY)
    with urllib.request.urlopen(req, timeout=6) as res:
        return json.loads(res.read().decode("utf-8"))


def _post(path, payload):
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if ACCESS_KEY:
        req.add_header("X-Access-Key", ACCESS_KEY)
    with urllib.request.urlopen(req, timeout=6) as res:
        return json.loads(res.read().decode("utf-8"))


def _notify(text: str) -> None:
    try:
        Notification(app_id=APP_ID, title="moz", msg=text, duration="long").show()
    except Exception as e:  # 通知失败不该拖死轮询
        print("[托盘] 弹通知失败:", type(e).__name__, e)


def _poll_loop(icon: pystray.Icon) -> None:
    while True:
        try:
            # 按了暂停就别再谎报"有人在听"：那只是这台机器还在轮询
            listening = 0 if _state["paused"] else 1
            items = _get(f"/care/pending?user_id={USER_ID}&listening={listening}").get("items", [])
            if not _state["online"]:
                _state["online"] = True
                icon.title = "moz · 已连接" if not _state["paused"] else "moz · 已暂停主动关心"
            if items and not _state["paused"]:
                # 先 ack 再弹：万一通知失败，也不会让同一条反复轰炸
                _post(f"/care/ack?user_id={USER_ID}", {"ids": [it["id"] for it in items]})
                for it in items:
                    _notify(it["text"])
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            if _state["online"]:
                _state["online"] = False
                icon.title = "moz · 后端未启动"
        time.sleep(POLL_SECONDS)


def _toggle_pause(icon: pystray.Icon, _item=None) -> None:
    _state["paused"] = not _state["paused"]
    icon.title = "moz · 已暂停主动关心" if _state["paused"] else "moz · 已连接"


def _open_app(icon: pystray.Icon, _item=None) -> None:
    webbrowser.open(WEB_URL)


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体：已经有托盘在跑就退出，否则同一条消息会弹两次通知。

    注意要用 use_last_error=True：普通 windll 调用之间 GetLastError 会被 ctypes
    自己的调用覆盖，实测会永远判定成"没有实例"。
    """
    if os.name != "nt":
        return True
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\moz-tray-single-instance")
    if not handle:
        return True  # 连句柄都没拿到，就别因此把自己锁死
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def main() -> None:
    if not _acquire_single_instance():
        print("[托盘] 已有一个 moz 托盘在运行，本实例退出")
        return
    menu = pystray.Menu(
        pystray.MenuItem("打开 moz", _open_app, default=True),
        pystray.MenuItem(
            "暂停主动关心", lambda i: _toggle_pause(i), checked=lambda i: _state["paused"]
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda i: i.stop()),
    )
    icon = pystray.Icon("moz", _icon_image(True), "moz · 启动中", menu)
    threading.Thread(target=_poll_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
