# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → Jev 判断 → 带着判断起草 3 条 → Jev 排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。
"""
from __future__ import annotations

try:
    from .draft import draft_candidates
    from .jev_client import JevError, ask
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text
except ImportError:
    from draft import draft_candidates
    from jev_client import JevError, ask
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state, guidance_text

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}


def _add_usage(total: dict, one: dict | None) -> None:
    """两次 Jev 调用的 usage 相加（tokens、cost）；非数字的字段后来的盖掉前面的。"""
    for k, v in (one or {}).items():
        total[k] = total.get(k, 0) + v if isinstance(v, (int, float)) else v


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "deepseek",
            base_url: str | None = None, reply_to: str | None = None, style: str = "",
            thinking: bool = False, jev_provider: str = "openrouter",
            jev_model: str | None = None, jev_base_url: str | None = None,
            role: str = "") -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（core.providers.DRAFT_PROVIDERS），base_url 只有自定义来源要传。
    jev_provider / jev_model: 判断和排序走哪家、哪个模型（core.providers.JEV_PROVIDERS）。
    jev_base_url: 判断来源自定义时自己填的地址，别的来源传 None。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    role: 客服角色设定（设置里的「客服角色设定」），填了就按这个身份说话，只影响起草。
    model / jev_model = None 用该来源的默认模型。

    返回 {candidates, best_index, best_reply, scores, answers, usage, reply_to}。
    scores 是每条候选的胜出概率（0~1），取自 best_reply.probabilities，取不到记 0.0。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。

    三段式（issue #4）：先让 Jev 答 7 道判断题，把判断当小抄喂给起草，最后 Jev 只排序。
    判断那次挂了就退回老路：盲起草 + 判断和排序一次问完，行为跟以前一样。usage 是两次之和。
    """
    state = build_state(messages, relationship, keep=context, reply_to=reply_to)
    usage: dict = {}
    answers: dict = {}
    judged = False
    try:
        first = ask(state, dict(JUDGE_QUESTIONS), timeout=timeout,
                    provider=jev_provider, model=jev_model, base_url=jev_base_url)
        answers = first.get("answers") or {}
        _add_usage(usage, first.get("usage"))
        judged = True
    except JevError:
        pass  # 退回盲起草 + 老的一次合问；错误不打日志（里面可能带请求内容）

    candidates = draft_candidates(messages, relationship, provider=provider, model=model,
                                  base_url=base_url, timeout=timeout, keep=context,
                                  reply_to=reply_to, style=style, thinking=thinking,
                                  guidance=guidance_text(answers) if judged else None,
                                  role=role)
    if not candidates:  # 注入过滤可以把起草结果全扔掉；接着取 [0] 会 IndexError
        raise JevError("起草结果没有可用候选回复")

    questions = {} if judged else dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    if questions:
        try:
            second = ask(state, questions, timeout=timeout,
                         provider=jev_provider, model=jev_model, base_url=jev_base_url)
        except JevError:
            if not judged:  # 老路只有这一次调用，挂了就是挂了
                raise
            second = {}  # 判断还在，只是没排上序：下面按第一条推荐
        answers = {**answers, **(second.get("answers") or {})}
        _add_usage(usage, second.get("usage"))

    best_key = (answers.get("best_reply") or {}).get("choice")
    best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
    if best_index >= len(candidates):
        best_index = 0

    probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
    scores = [0.0, 0.0, 0.0]
    for key, idx in _REPLY_IDX.items():
        try:
            scores[idx] = float(probabilities.get(key, 0.0))
        except (TypeError, ValueError):
            scores[idx] = 0.0  # 脏数据一律按 0 处理

    return {
        "candidates": candidates,
        "best_index": best_index,
        "best_reply": candidates[best_index],
        "scores": scores,
        "answers": answers,
        "usage": usage,
        "reply_to": reply_to,
    }


_LEAD_SYSTEM = (
    "你是家教机构的教务老师，负责把一段微信聊天整理成一条「家教需求单」。\n"
    "只输出一个 JSON 对象，不要解释、不要代码围栏、不要多余的字。键固定为：\n"
    "addr：地点，尽量写到「城市+区+小区/路段」，例如「深圳市罗湖区鸿翔花园」；\n"
    "subject：年级+科目，例如「高一化学」；\n"
    "student：学生情况，例如「女孩」「初二三，基础较弱」；\n"
    "schedule：上课时间，例如「一周一次，周六上午或者下午」；\n"
    "pay：课酬，例如「340/次」「一小时 200」；\n"
    "demand：对老师的要求，例如「女老师，节奏紧凑」；\n"
    "note：其他值得记的补充（学习目标、试课意愿等），没有就空。\n"
    "规则：聊天里没提到的字段一律填空字符串，不许猜、不许编；"
    "整段聊天里没有任何家教需求信息（比如只是闲聊、寒暄、已经成交后的话）就输出 {}。\n"
    "聊天里 her 是家长，me 是机构客服。"
)

_LEAD_KEYS = ("addr", "subject", "student", "schedule", "pay", "demand", "note")


def extract_lead(messages: list, provider: str = "deepseek", model: str | None = None,
                 base_url: str | None = None, timeout: float = 25,
                 thinking: bool = False, keep: int = 40) -> dict:
    """把对话里家长的家教需求抽成结构化字段；没有需求就返回 {}。
    跟 analyze 分开：这条只在客服角色模式下、每次分析成功后顺手跑一次，失败了不影响回复。"""
    import json

    try:
        from .llm import chat
        from .providers import DRAFT_PROVIDERS, LLM_ENV
        from .jev_client import _api_key
    except ImportError:
        from llm import chat
        from providers import DRAFT_PROVIDERS, LLM_ENV
        from jev_client import _api_key
    if not messages:
        return {}
    lines = []
    for m in messages[-keep:]:
        if isinstance(m, dict):
            who, text, name = m.get("from"), m.get("text"), m.get("name")
        else:
            who, text = m[0], m[1]
            name = m[2] if len(m) > 2 else None
        lines.append(f"{name if who == 'her' and name else who}: {text}")
    spec = DRAFT_PROVIDERS[provider]
    content = chat(spec.protocol, base_url or spec.base, _api_key(LLM_ENV),
                   model or spec.default, _LEAD_SYSTEM,
                   ["聊天记录（最后一条是最新）:\n" + "\n".join(lines)],
                   temperature=0.1, max_tokens=400, thinking=thinking,
                   extra_body=spec.extra(thinking), headers=spec.headers, timeout=timeout)
    text = (content or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: str(data.get(k) or "").strip()[:300] for k in _LEAD_KEYS}
    return out if any(out.values()) else {}


if __name__ == "__main__":
    # 候选被过滤光时要抛 JevError，不能在取第一条时 IndexError。
    from unittest.mock import patch

    with patch("__main__.ask", return_value={"answers": {}, "usage": {}}), \
         patch("__main__.draft_candidates", return_value=[]):
        try:
            analyze([("her", "hello")], "friends")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "没有可用候选" in str(e)
    print("engine ok")
