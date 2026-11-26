# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import time
import traceback

import numpy as np

from app import chatapps
from app.capture import Capture, chat_area, unminimize
from app.ocr import Reader, read_title, similar


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


# 100% DPI 下「左侧图标栏 + 会话列表」的物理宽度。实测 175% 缩放下聊天区从 536px 开始，
# 536 / 1.75 ≈ 306，所以按 DPI 等比放大即可，不能再写死 300。
LIST_W_100 = 306


def _dpi_of(hwnd):
    """窗口所在显示器的 DPI。拿不到就当 96。"""
    u = ctypes.windll.user32
    try:
        v = int(u.GetDpiForWindow(hwnd))
        if v:
            return v
    except Exception:
        pass
    try:
        g = ctypes.windll.gdi32
        dc = g.CreateDCW("DISPLAY", None, None, None)
        v = int(g.GetDeviceCaps(dc, 88))  # LOGPIXELSX
        g.DeleteDC(dc)
        if v:
            return v
    except Exception:
        pass
    return 96


def _list_edge(hwnd):
    """会话列表右边界，相对窗口左边的物理像素（截图是物理像素：run() 里 SetProcessDPIAware 了）。"""
    return max(1, int(round(LIST_W_100 * _dpi_of(hwnd) / 96.0)))


def _packet(full, area, title, reader, lines):
    """调试视图的一帧：整帧缩到长边 ≤1100 再走队列（原帧 2560 宽裸传要 20MB），只在内存里传，不落盘。
    整数步长切片够用，不引新依赖；框和坐标照发原始值，画的那边按 scale 折算。"""
    k = max(1, -(-max(full.shape[:2]) // 1100))
    small = np.ascontiguousarray(full[::k, ::k])
    return {"w": small.shape[1], "h": small.shape[0], "rgb": small.tobytes(), "scale": k,
            "area": tuple(int(v) for v in area[:4]) if area else None,
            "pane_top": int(area[5]) if area else 0, "title": title,
            "boxes": reader.last_boxes if reader else [],
            "lines": [(w, n, t) for w, n, t, _ in lines],
            "ocr_ms": reader.last_ms if reader else 0, "ts": time.time()}


def run(q, hwnd, enabled, debug_on, app_key=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。
    debug_on 置位才往队列里送整帧（一帧 2~3MB），关着一点额外活都不干。"""
    ctypes.windll.user32.SetProcessDPIAware()
    list_edge = _list_edge(hwnd)  # 会话列表右边界，随 DPI 放大
    app = chatapps.get(app_key)
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            if full is not None:
                reader, lines = None, []  # 调试视图要用，消息区没认出来时就是空的
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                if area is None:
                    if not warned:
                        q.put(("status", "消息区认不出来（窗口太小？）"))
                        warned = True
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    # 头部：会话名在这里。x0 是否已经等于聊天区左边界，取决于主题：
                    # 浅色主题下会话列表底色和聊天区不同，chat_area 能分出边界（x0 就是列表右边），
                    # 这时再往后跳 300px 就会把群名开头和每行消息的左边一起裁掉；
                    # 暗色主题下两者底色一样，x0 退化成整个窗口左边缘，才需要跳到 list_edge，
                    # 否则「搜索」框和列表条目会被 read_title 当成群名（它取同一行最左边的文字）。
                    left = x0 if x0 > list_edge * 0.7 else list_edge
                    head_x0 = min(left, max(x0, x1 - 120))
                    crop = full[y_pane:y0, head_x0:x1]
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        name = read_title(crop, app)
                        # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                        name = next((k for k in readers if similar(k, name)), name) if name else ""
                        # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                        name = name or title or "当前会话"
                        if name != title:
                            title = name
                            q.put(("chat", title))
                    reader = readers.setdefault(title, Reader(app))
                    # 消息区同理：不跳的话「腾讯新闻」「文件传输助手」「微信团队」这些
                    # 列表条目会被当成对方说的话，还会害得整段逻辑错乱。
                    body_x0 = min(left, max(x0, x1 - 200))
                    lines = reader.read(full[y0:y1, body_x0:x1], bg)
                    new = reader.new_lines(lines)
                    if new:
                        q.put(("lines", title, new, rect))
                if debug_on.is_set():
                    q.put(("debug", _packet(full, area, title, reader, lines)))
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（聊天窗口关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
