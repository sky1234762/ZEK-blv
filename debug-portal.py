#!/usr/bin/env python3
"""调试 portal 屏幕采集：打印 streams 详情，并逐一尝试 gst 的不同连接方式。

用法：./debug-portal.py
会弹出「选择屏幕」对话框，选完它会自动测 4 种 pipewiresrc 连法，各跑几秒。
"""
import dbus
import dbus.mainloop.glib
import os
import subprocess
import sys
import time

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
from gi.repository import GLib

bus = dbus.SessionBus()
desktop = bus.get_object("org.freedesktop.portal.Desktop",
                         "/org/freedesktop/portal/desktop")
sc = dbus.Interface(desktop, "org.freedesktop.portal.ScreenCast")

token = "debug_%d" % int(time.time() * 1000)


def wait_response(request_path, timeout=120):
    holder = {}
    loop = GLib.MainLoop()

    def on_response(response, results):
        holder["response"] = int(response)
        holder["results"] = dict(results) if results else {}
        loop.quit()

    bus.add_signal_receiver(on_response, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=request_path)
    GLib.timeout_add_seconds(timeout, loop.quit)
    loop.run()
    if "response" not in holder:
        raise SystemExit("portal 超时")
    return holder["response"], holder["results"]


print("== 1) CreateSession ==")
req = sc.CreateSession({"handle_token": token + "_s",
                        "session_handle_token": token + "_sess"})
resp, results = wait_response(req)
print("  response=%d session_handle=%s" % (resp, results.get("session_handle")))
session_handle = results["session_handle"]

print("== 2) SelectSources（去弹窗里选屏幕）==")
req = sc.SelectSources(session_handle, {
    "handle_token": token + "_sel",
    "types": dbus.UInt32(1),
    "multiple": dbus.Boolean(False),
    "cursor_mode": dbus.UInt32(2),
})
resp, _ = wait_response(req)
print("  response=%d" % resp)

print("== 3) Start ==")
req = sc.Start(session_handle, "", {"handle_token": token + "_start"})
resp, results = wait_response(req)
print("  response=%d" % resp)
streams = results.get("streams") or []
print("  streams 数量: %d" % len(streams))
for i, s in enumerate(streams):
    # s 是 (uint32 id, a{sv} props)
    sid = int(s[0])
    props = {str(k): v for k, v in dict(s[1]).items()}
    print("  stream[%d]: id(uint32)=%d  props=%s" % (i, sid, props))

print("== 4) OpenPipeWireRemote ==")
fd_obj = sc.OpenPipeWireRemote(session_handle, {})
fd = fd_obj.take()
print("  fd=%d" % fd)

# 收集候选 selector
candidates = []
sid = int(streams[0][0])
# 1) 直接用 id，且不接 videoconvert（隔离：是连不上节点还是格式问题）
candidates.append(("target-object=%d(直连fakesink)" % sid,
                   ["target-object=%d" % sid], "direct"))
# 2) 当前主方案：id + videoconvert
candidates.append(("target-object=%d" % sid, ["target-object=%d" % sid], "convert"))
# 3) 用 path 属性
candidates.append(("path=%d" % sid, ["path=%d" % sid], "convert"))
# 4) 自动连接
candidates.append(("自动(autoconnect)", [], "convert"))
# 5) pipewire-serial（如果有）
props = {str(k): v for k, v in dict(streams[0][1]).items()}
if "pipewire-serial" in props:
    serial = int(props["pipewire-serial"])
    candidates.append(("target-object=%d(pipewire-serial)" % serial,
                       ["target-object=%d" % serial], "convert"))

print("\n== 5) 逐种连接方式测试 pipewiresrc ==")
os.set_inheritable(fd, True)

for name, sel, mode in candidates:
    gst = ["gst-launch-1.0", "-q", "pipewiresrc", "fd=%d" % fd] + sel
    gst += ["do-timestamp=true"]
    if mode == "direct":
        gst += ["!", "fakesink", "num-buffers=30"]
    else:
        gst += ["!", "videoconvert",
                "!", "video/x-raw,format=BGRx,width=640,height=360,framerate=30/1",
                "!", "fakesink", "num-buffers=30"]
    logfile = "/tmp/gst_debug_%s.log" % name.replace("=", "_").replace("(", "").replace(")", "")
    env = dict(os.environ)
    env["GST_DEBUG"] = "2,pipewire*:4"
    print("\n--- 测试: %s ---" % name)
    print("命令:", " ".join(gst))
    try:
        with open(logfile, "wb") as lf:
            p = subprocess.run(gst, env=env, stderr=lf, stdout=lf,
                               pass_fds=[fd], timeout=10)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = "超时(可能正常采集但没到30帧?)"
    print("  退出码:", rc)
    # 从日志提取关键错误
    try:
        with open(logfile, "r", errors="replace") as lf:
            txt = lf.read()
        errs = [l for l in txt.splitlines()
                if "stream error" in l.lower() or "error" in l.lower()
                or "negoti" in l.lower() or "node" in l.lower()][:6]
        if errs:
            print("  关键日志:")
            for e in errs:
                print("    ", e[:200])
        else:
            print("  （无 error/node/negoti 关键字）")
    except OSError:
        pass

print("\n完成。session_handle=%s fd=%d" % (session_handle, fd))
try:
    dbus.Interface(bus.get_object("org.freedesktop.portal.Desktop", session_handle),
                   "org.freedesktop.portal.Session").Close()
except Exception:
    pass
