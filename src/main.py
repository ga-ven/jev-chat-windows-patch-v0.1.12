# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import csv
import multiprocessing
import os
import queue
import random
import re
import threading
import time
import traceback
from collections import deque

from app import settings, update, worker
from app.capture import chat_area, find_chat_hwnd, grab, unread_rows
from app.fill import click_at, fill
from app.overlay import Overlay
from app.version import VERSION
from core.engine import analyze, extract_lead
from app.i18n import T

# {会话名: {history, result, rev, target, senders}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         "next_switch": 0.0, "scanning": False}
results = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
scan_result = queue.Queue()    # 自动切换：后台线程扫出来的未读行

# 自动切换会话的节奏。扫描只是一次 PrintWindow + 一小块像素统计，几十毫秒，但没必要一直扫。
SWITCH_IDLE = 10.0        # 列表里没有未读：隔这么久再看一眼
SWITCH_DWELL_AUTO = 7.0   # 会自动发：点开、生成、发出，留 7 秒再切下一个
SWITCH_DWELL_MANUAL = 25.0  # 不自动发：留足时间让人看候选、自己点填入

# 这些会话不是人，自动切换会点开看一眼但绝不自动发消息
_SYSTEM_CHATS = ("微信团队", "文件传输助手", "腾讯新闻", "订阅号", "服务号", "公众号",
                 "微信支付", "微信运动", "微信收款助手", "折叠的群聊")
_GROUP_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")

LEAD_COLUMNS = ["更新时间", "家长/会话", "地点", "年级科目", "学生", "上课时间", "课酬", "老师要求", "备注"]
_ledger_lock = threading.Lock()


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def is_group(title):
    """群名尾巴上带人数括号的算群聊。单人备注名很少这么写，误判概率够低。"""
    return bool(_GROUP_RE.search(title or ""))


def is_system_chat(title):
    return any(k in (title or "") for k in _SYSTEM_CHATS)


def may_auto_send(title):
    """自动切换巡场时，允许哪些会话直接发出去。
    不开自动切换就是原来的行为（只处理你正看着的那个会话），这里一律放行。"""
    if not settings.auto_switch():
        return True
    if is_system_chat(title):
        return False
    if is_group(title) and not settings.switch_groups():
        return False
    return True


def save_lead(title, lead):
    """把一条家教需求写进 CSV 台账：按会话名去重，新抽到的非空字段覆盖旧的，空字段不抹掉已有内容。
    同一个文件可能被几个分析线程同时写，加锁 + 先写 .tmp 再替换，别留半个文件。"""
    path = settings.ledger_path()
    row = {"更新时间": time.strftime("%Y-%m-%d %H:%M"), "家长/会话": title,
           "地点": lead.get("addr", ""), "年级科目": lead.get("subject", ""),
           "学生": lead.get("student", ""), "上课时间": lead.get("schedule", ""),
           "课酬": lead.get("pay", ""), "老师要求": lead.get("demand", ""),
           "备注": lead.get("note", "")}
    with _ledger_lock:
        rows, order = {}, []
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for old in csv.DictReader(f):
                    key = (old.get("家长/会话") or "").strip()
                    if not key:
                        continue
                    if key not in rows:
                        order.append(key)
                    rows[key] = {k: (old.get(k) or "") for k in LEAD_COLUMNS}
        except (OSError, csv.Error):
            rows, order = {}, []  # 文件不在 / 读坏了：从头建一份，别把这次的需求丢了
        if title in rows:
            for k, v in row.items():
                if v:
                    rows[title][k] = v
        else:
            rows[title] = row
            order.append(title)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:  # BOM：Excel 直接认出中文
                writer = csv.DictWriter(f, fieldnames=LEAD_COLUMNS)
                writer.writeheader()
                for key in order:
                    writer.writerow(rows[key])
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def open_ledger():
    """打开台账；还没生成过就返回 False，交给界面提示。"""
    path = settings.ledger_path()
    if not os.path.exists(path):
        return False
    try:
        os.startfile(path)  # noqa: S606 —— 就是让系统用默认程序打开这个 csv
        return True
    except OSError:
        return False


def scan_unread_bg():
    """后台抓一帧，找出会话列表里带未读徽标的行。扔队列给 tick，UI 线程不碰截图。"""
    try:
        hwnd, _ = find_chat_hwnd()
        frame, rect = grab(hwnd)
        if frame is None:
            scan_result.put(None)
            return
        area = chat_area(frame)
        # 认不出消息区（窗口太小/刚拖过）就按窗口宽度的经验比例估一下聊天区左边界
        x0 = area[0] if area else int(frame.shape[1] * 0.34)
        scan_result.put((hwnd, rect, x0, unread_rows(frame, x0)))
    except Exception:
        scan_result.put(None)


def drive_switch():
    """自动切换会话：扫未读 → 点最上面那个 → 等它生成/发出 → 冷却 → 再扫。
    只做「点一下会话行」，剩下的全交给原来的采集→分析→填入那条链，不另开一套。"""
    if not settings.auto_switch() or child is None or not capture_on.is_set():
        return
    now = time.time()
    try:
        hwnd, rect, x0, rows = scan_result.get_nowait()
    except queue.Empty:
        hwnd = rect = x0 = None
        rows = []
    else:
        state["scanning"] = False
        if hwnd is None or not rows:
            state["next_switch"] = now + SWITCH_IDLE
        else:
            y = rows[0]  # 最上面那个有未读的
            left, top = rect[0], rect[1]
            # 横向点在会话行中间偏右：避开头像和昵称，也别压到徽标本体
            click_at(left + int(x0 * 0.72), top + y)
            state["next_switch"] = now + (SWITCH_DWELL_AUTO if settings.auto_fill()
                                          else SWITCH_DWELL_MANUAL)
            return
    if state["busy"] or state["scanning"] or now < state["next_switch"]:
        return
    state["scanning"] = True
    threading.Thread(target=scan_unread_bg, daemon=True).start()


def split_reply(text, limit=3):
    """把一条回复拆成 2~3 段，像真人一句一句发。
    先按句末标点切，切不开再按逗号/空格切；太碎的片段并回前一段。
    拆不出两个像样的片段就整条发，别硬拆成刷屏。"""
    if len(text) < 12:
        return [text]
    parts = re.split(r"(?<=[。！？!?~…])", text)
    if len(parts) < 2:
        parts = re.split(r"[，,、\s]+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return [text]
    merged = []
    for p in parts:
        if merged and len(merged[-1]) < 8:  # 太短的并到前一段，免得一句只有两个字
            # 上一段以句末标点结尾就直接接，否则补个空格——原来是按空格拆开的，
            # 拼回去不加空格会连成「女老师的话先确认下有没有合适的」这种。
            tail = merged[-1][-1:]
            merged[-1] += ("" if tail in "。！？!?…~" else " ") + p
        else:
            merged.append(p)
    while len(merged) > limit:
        merged[-2] += merged[-1]
        merged.pop()
    return merged if len(merged) >= 2 else [text]


def fill_reply(text):
    # 每次填之前重新找窗口：微信重建窗口后（切账号、退出重登）旧句柄会落到一个
    # 160x28 的托盘小窗上，拿它算出来的点击坐标全在屏幕外，点了等于没点。
    try:
        hwnd, found = find_chat_hwnd()
        state["hwnd"], state["app"] = hwnd, found.key
    except RuntimeError:
        raise RuntimeError(T("未找到聊天窗口，请确认已经打开"))
    if state["area"] is None:
        raise RuntimeError(T("输入区域尚不可用，请确认聊天窗口可见（不要最小化）"))
    auto = settings.auto_send() or settings.auto_fill()
    target = None
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target and not settings.real_at():
            text = f"@{target} " + text  # 纯文本 @，微信不认成真正的 @，对方收不到通知
    # 分条只在会自动发的时候有意义：不开自动发送，几条会全堆在输入框里等着被一起发出去
    parts = split_reply(text) if (settings.multi_send() and auto) else [text]
    for i, part in enumerate(parts):
        at = target if (settings.real_at() and settings.reply_target() and i == 0) else None
        fill(state["hwnd"], state["area"], part, auto_send=auto, at_target=at)
        if i < len(parts) - 1:
            time.sleep(random.uniform(1.8, 3.2))  # 真人打字也有停顿，连发会像机器人
    # 自动巡场时，发完这一轮就把下一个会话的切换往后推一点，别刚发完就抢焦点
    state["next_switch"] = time.time() + (2.0 if auto else 4.0)


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。"""
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, state.get("app")),
                                daemon=True)
    p.start()
    return p


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_language_changed():
    """主界面切换语言后，同步刷新已经打开过的调试窗。"""
    if dbg is not None:
        dbg.retranslate()


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"], found = find_chat_hwnd()
            state["app"] = found.key
        except RuntimeError:
            ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def analyze_bg(msgs, title, revision, reply_to=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    role = settings.role_prompt()
    try:
        r = analyze(msgs, settings.relationship(), context=settings.context(),
                    model=settings.draft_model() or None,
                    provider=settings.draft_provider(),
                    base_url=settings.draft_base_url() or None,
                    reply_to=reply_to, style=settings.style(),
                    thinking=settings.thinking(),
                    jev_provider=settings.jev_provider(),
                    jev_model=settings.jev_model() or None,
                    jev_base_url=settings.jev_base_url() or None,
                    role=role)
        results.put(("ok", r, title, revision))
    except Exception as e:
        results.put(("err", f"{T('分析失败: ')}{e}", title, revision))
        return
    # 客服角色开着才顺手抽一次需求：这是替家长登记，跟回不回得上无关，失败了也不影响回复。
    if not role:
        return
    try:
        lead = extract_lead(msgs, provider=settings.draft_provider(),
                            model=settings.draft_model() or None,
                            base_url=settings.draft_base_url() or None)
        if lead:
            save_lead(title, lead)
    except Exception:
        pass  # 抽取是附加功能，别把回复也拖下水


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if not settings.has_llm_key():
        ov.set_status(lambda name=settings.draft_provider_name():
                      f"{T('起草来源 ')}{name}{T(' 没填密钥，去设置里补上')}", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, chat_of(title)["rev"], reply_to),
                     daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        # 只要这批新消息里有对方说的话，就该分析——不必等「最后一条是对方」。
        # 原来要求 new[-1] 是 her，导致自己刚说完话、对方后面又接了一句时它不动。
        # 反过来，整批都是自己发的就什么都不做：别自己接自己的话。
        if any(who == "her" for who, _, _ in new):
            msgs = list(chat["history"])
            if state["busy"]:
                state["rerun"] = (title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def tick():
    try:
        drain()
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    # 自动巡场时按会话类型放行：群聊/系统会话只看不发
                    ov.show(r, auto=may_auto_send(title))
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_status("生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r)
        drive_switch()  # 自动切换会话：每 50ms 经过这里，内部自己节流
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    debug_on = multiprocessing.Event()  # 同上，置位=子进程往队列里送整帧给调试窗
    child = dbg = None
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_language_changed=on_language_changed, on_open_ledger=open_ledger,
                 result_of=lambda t: chats.get(t, {}).get("result"))
    try:
        state["hwnd"], found = find_chat_hwnd()
        state["app"] = found.key
    except RuntimeError:
        ov.set_capture(False, "未找到聊天窗口，打开后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_jev_key():
        ov.set_status("请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            child.terminate()
