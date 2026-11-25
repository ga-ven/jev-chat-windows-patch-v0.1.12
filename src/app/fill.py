# -*- coding: utf-8 -*-
"""把选中的候选填进微信输入框：写剪贴板 → 点输入框 → Ctrl+V。
auto_send 打开时（设置里「填入后自动发送」）粘完再按一次 Enter 发出去；
默认关闭，那时候绝不发回车、绝不点发送。"""
import ctypes
import ctypes.wintypes as w
import time

u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

# 64 位下 ctypes.windll 默认 restype 是 32 位 c_int，而 GlobalAlloc 返回 64 位 HGLOBAL——
# 不声明类型句柄会被截断成垃圾值，GlobalLock(垃圾) 返回 NULL，memmove(NULL,…) 就是
# "access violation writing 0x0"。所有带句柄/指针的函数必须显式声明。
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalFree.argtypes = [ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def set_clipboard(text):
    """写剪贴板。剪贴板可能被别的程序占着（剪贴板管理器、截图工具），重试几次。"""
    data = text.encode("utf-16-le") + b"\0\0"
    for attempt in range(10):
        if not u32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x2, len(data))  # GMEM_MOVEABLE
            if not h:
                raise RuntimeError("GlobalAlloc 失败")
            p = k32.GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                raise RuntimeError("GlobalLock 失败")
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT；成功后句柄归系统，不能 Free
                k32.GlobalFree(h)
                raise RuntimeError(f"SetClipboardData 失败 (attempt {attempt})")
            return
        finally:
            u32.CloseClipboard()
    raise RuntimeError("OpenClipboard 连续失败，剪贴板被其他程序占用")


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG),
                ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD),
                ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("union", _INPUTUNION)]


def type_text(text):
    """逐字敲进去，完全不碰剪贴板。
    剪贴板经常被别的程序长期占着（剪贴板管理器、翻译工具、常驻的 Electron 应用都会），
    这时候 Copy/Paste 那条路根本走不通，但键盘输入照样能用。
    走 KEYEVENTF_UNICODE 直接送字符，绕开输入法，中文也能进。"""
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:  # 基本平面外的字符拆成代理对
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for unit in units:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = _INPUT()
                inp.type = 1  # INPUT_KEYBOARD
                inp.union.ki = _KEYBDINPUT(0, unit, flags, 0, None)
                if not u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)):
                    # 被 UIPI 挡下（目标权限更高）时会返回 0，别静悄悄地发出去一条空消息
                    raise RuntimeError("逐字输入失败：SendInput 被系统拒绝")
        time.sleep(0.004)  # 敲太快微信会丢字


def fill(hwnd, area, text, auto_send=False, at_target=None):
    """area = 消息区 (x0, y0, x1, y1)；输入框就在底线 y1 下面。"""
    from app.capture import unminimize

    # 剪贴板经常被别的程序长期占着（剪贴板管理器、翻译工具、常驻应用），
    # 占着就写不进去。这时退回逐字敲——慢一点，但不依赖剪贴板。
    try:
        set_clipboard(text)
        paste = True
    except RuntimeError:
        paste = False
    r = w.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # 扩展边界，跟 WGC 帧对齐
        u32.GetWindowRect(hwnd, ctypes.byref(r))
    x0, _, x1, y1 = area[:4]
    height = r.bottom - r.top
    width = r.right - r.left
    # area 是采集子进程按「某一时刻那个窗口」算出来的。微信重建窗口（切账号、退出重登）
    # 之后句柄会落到一个 160x28 的托盘小窗上，area 就彻底对不上了 —— 这时候按窗口几何推。
    if not (0 < x1 <= width and 0 < y1 <= height):
        cx = r.left + int(width * 0.68)   # 会话列表约 300px，剩下的聊天区取中间
        cy = r.bottom - 80
    else:
        # 横向：取聊天区中点。原来用 x0+60，暗色主题下 chat_area 分不出会话列表和聊天区
        # （两边底色一样），x0 会退化成整个窗口左边缘，那样就点到左边的会话列表上去了。
        cx = r.left + (x0 + x1) // 2
        # 纵向：输入框文字区在消息区底线 y1 下面 40px。但 chat_area 找不到分隔线时会把 y1 退化成
        # 窗口底部（暗色主题下输入框那根线不是「整行单色」，认不出来），此时 y1+40 会落到窗口外面，
        # 点下去等于白点。这种情况按窗口高度往上推一个输入框中心的位置兜底。
        if y1 >= height - 20:
            cy = r.top + height - 80
        else:
            cy = r.top + y1 + 40  # 分隔线下 40px = 输入框文字区
    cy = min(max(cy, r.top + 40), r.bottom - 40)  # 无论如何别越过窗口上下边界
    unminimize(hwnd)

    # SetForegroundWindow 有前台窗口保护，普通后台进程会被拒；AttachThreadInput 绕过
    fg = u32.GetForegroundWindow()
    if fg != hwnd:
        fg_tid = u32.GetWindowThreadProcessId(fg, None)
        our_tid = k32.GetCurrentThreadId()
        u32.AttachThreadInput(our_tid, fg_tid, True)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(our_tid, fg_tid, False)
        time.sleep(0.15)  # 给微信一点时间响应前台切换

    old = w.POINT()
    u32.GetCursorPos(ctypes.byref(old))
    u32.SetCursorPos(cx, cy)
    time.sleep(0.05)
    u32.mouse_event(0x2, 0, 0, 0, 0)  # 左键按下
    u32.mouse_event(0x4, 0, 0, 0, 0)  # 抬起
    time.sleep(0.05)
    u32.SetCursorPos(old.x, old.y)
    time.sleep(0.05)
    # 光标移到已有文本的绝对末尾：点击落在文字中间时 caret 会插在中间，
    # 连续多次填入就串行错乱；Ctrl+End 保证新内容永远追加在最后
    u32.keybd_event(0x11, 0, 0, 0)  # Ctrl 按下
    u32.keybd_event(0x23, 0, 0, 0)  # End 按下（VK_END）
    u32.keybd_event(0x23, 0, 2, 0)  # End 抬起
    u32.keybd_event(0x11, 0, 2, 0)  # Ctrl 抬起
    time.sleep(0.05)
    if at_target:
        # 真 @：打「@名字」让微信弹出成员列表并过滤，再回车选中第一个匹配。
        # 选不中的话输入框里就留着「@名字」这段文字，正文接上去等于退化成纯文本 @，不会更糟。
        type_text("@" + at_target)
        time.sleep(0.45)
        u32.keybd_event(0x0D, 0, 0, 0)  # VK_RETURN：选中弹窗里第一项
        u32.keybd_event(0x0D, 0, 2, 0)
        time.sleep(0.25)
    if paste:
        u32.keybd_event(0x11, 0, 0, 0)  # Ctrl
        u32.keybd_event(0x56, 0, 0, 0)  # V
        u32.keybd_event(0x56, 0, 2, 0)
        u32.keybd_event(0x11, 0, 2, 0)
    else:
        type_text(text)  # 剪贴板被占，改成逐字敲
    if not auto_send:
        # 到此为止。发不发、改不改，人来。
        return
    # 自动发送：等输入框把粘贴的内容消化完，再敲 Enter（微信默认 Enter 发送）。
    # 输入框里如果本来就留着草稿，会连同本次粘贴一起发出去——设置项的说明里写明了。
    time.sleep(0.35)
    u32.keybd_event(0x0D, 0, 0, 0)  # VK_RETURN 按下
    u32.keybd_event(0x0D, 0, 2, 0)  # 抬起
