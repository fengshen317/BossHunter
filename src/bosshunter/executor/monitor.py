"""Monitor module - Watch for HR replies and generate reply suggestions."""

import time
import json
import hashlib
from threading import Event
from urllib.parse import parse_qs, urlencode, urlparse

from rich.console import Console

from bosshunter.ai.credentials import call_anthropic_text
from bosshunter.browser import new_tab, close_tab, evaluate, click, wait_for_load, navigate
from bosshunter.db import (
    get_db, get_jobs_by_status, insert_job,
    update_job_status, update_job_conversation_id, add_history,
)
# 复用 sender 的输入框选择器，避免两处选择器定义漂移。
from bosshunter.executor.sender import CHAT_INPUT_SELECTOR
from bosshunter.throttle import RequestThrottle, SendWindowChecker

console = Console()

PORTFOLIO_URL = None  # Set via config: profile.portfolio_url

# JS: Extract chat list with full message context
JS_EXTRACT_CHAT_LIST = """
(() => {
    const items = document.querySelectorAll('li[role=listitem]');
    const results = [];
    items.forEach(item => {
        const nameText = item.querySelector('.name-text');
        const nameBox = item.querySelector('.name-box');
        const lastMsgEl = item.querySelector('.last-msg-text');
        const msgStatus = item.querySelector('.message-status');
        const unreadBadge = item.querySelector('.notice-badge');

        if (!nameText) return;

        const spans = nameBox ? nameBox.querySelectorAll('span') : [];
        const company = spans.length >= 2 ? spans[1].textContent.trim() : '';
        const hrTitle = spans.length >= 3 ? spans[spans.length - 1].textContent.trim() : '';

        // 提取会话链接里的 conversation_id，供未跟踪的 HR 主动会话建影子岗位并直达会话。
        const linkEl = item.querySelector('a[href*="/web/geek/chat?"], a[href*="chat?id"], a[href*="geek/chat"]');
        let conversation_id = '';
        if (linkEl) {
            const href = linkEl.getAttribute('href') || '';
            const m = href.match(/[?&]id=([^&]+)/);
            if (m) conversation_id = m[1];
        }

        // Determine if HR replied: no message-status means the last message is FROM HR.
        // 注意：列表预览阶段无法可靠区分“这条是我发的”还是“HR 发的、我已读未回”——
        // status-read 既可能表示本人消息已读回执，也可能表示对方消息我已读过。
        // 因此这里只把 is_our_message 作为诊断字段保留，绝不用于过滤候选；
        // 真正的归属和“我有没有回过”判断交给 _handle_conversation 打开完整会话后，
        // 用 .item-myself/.item-friend 精确判定（可靠且不会污染状态/历史）。
        const statusClass = msgStatus ? msgStatus.className : '';
        const isOurMessage = statusClass.includes('status-read')
            || statusClass.includes('status-delivery')
            || statusClass.includes('status-sent');
        const lastMessage = lastMsgEl ? lastMsgEl.textContent.trim() : '';
        const unreadCount = unreadBadge ? Number.parseInt(unreadBadge.textContent.trim(), 10) || 0 : 0;
        const isSystemHint = /^您正在与Boss.*沟通$/.test(lastMessage);
        const hasReply = unreadCount > 0 || (!!lastMessage && !isOurMessage && !isSystemHint);
        // 候选筛选：宽松但安全。只要预览有真实文本（非系统占位文案），就进入核验候选。
        // 这是 bug 前的安全网设计——宁可多打开几个会话核验，也不能漏掉 HR 的真回复。
        // 误纳的候选（最后一条是我发的、或我已回复过的）会在 _handle_conversation 里
        // 被安全跳过（skipped_user_replied），不会改状态、不会写假历史。
        const needsInspection = !!lastMessage && !isSystemHint;

        results.push({
            hr_name: nameText.textContent.trim(),
            company: company,
            hr_title: hrTitle,
            last_message: lastMessage.substring(0, 200),
            has_reply: hasReply,
            needs_inspection: needsInspection,
            unread_count: unreadCount,
            is_our_message: isOurMessage,
            conversation_id: conversation_id,
            element_index: results.length
        });
    });
    return JSON.stringify(results);
})()
"""

# JS: Extract full conversation messages from an open chat, including rich/system cards
JS_EXTRACT_CONVERSATION = r"""
(() => {
    const MESSAGE_SELECTORS = '.chat-message, .message-item';
    const TEXT_SELECTORS = '.msg-text, .text, .message-text';
    function normalizeText(value) {
        return (value || '').replace(/\s+/g, ' ').trim();
    }

    function isVisible(el) {
        if (!el) return false;
        const rects = el.getClientRects();
        const style = window.getComputedStyle(el);
        return rects.length > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    }

    function visibleText(el) {
        if (!isVisible(el)) return '';
        return normalizeText(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
    }

    function collectVisibleText(root) {
        // 普通文本消息只读取正文节点。旧逻辑还会继续遍历正文内部所有
        // class 含 card/button 的父子节点，导致同一段文字被拼接多次。
        const textEl = root.querySelector(TEXT_SELECTORS);
        const primaryText = textEl ? visibleText(textEl) : '';
        if (primaryText) return primaryText;

        // 富卡片没有标准正文节点时读取容器一次即可；innerText 已包含按钮文案，
        // 不再重复遍历卡片的祖先、后代和 action 元素。
        return visibleText(root);
    }

    function isResumeRequestCard(text) {
        const hasAttachmentResume = text.includes('附件简历') || text.includes('您的附件简历');
        const hasIntent = ['是否同意', '同意', '想要一份', '请求', '获取', '发送', '发给'].some(kw => text.includes(kw));
        return hasAttachmentResume && hasIntent;
    }

    const msgs = Array.from(document.querySelectorAll(MESSAGE_SELECTORS)).filter(msg => {
        // 去重：若该节点内部还嵌套了别的消息节点，说明它只是外层容器（如
        // .chat-message 包着 .message-item），不是单条消息，必须剔除。
        // 旧逻辑查 parentElement.closest(MESSAGE_SELECTORS)——方向反了：BOSS 的
        // .message-item 父级是 .im-list，.im-list 向上能命中祖先 .chat-message，
        // 于是所有真正的消息（.message-item）都被误判为“嵌套”而过滤掉，
        // 只剩 .chat-message 容器本身；容器再靠 querySelector 命中第一个
        // .item-friend 子节点，把整段（含本人招呼）全标成 hr。
        return !msg.querySelector(MESSAGE_SELECTORS);
    });
    const results = [];
    msgs.forEach(msg => {
        // BOSS 直聘真实类名：item-myself=我的消息 / item-friend=对方消息 / item-system=系统消息。
        // 归属优先看节点自身的 class（实测 .message-item 直接带 item-myself/item-friend）；
        // 自身没有时再退而查祖先或后代，作为 DOM 漂移兜底。
        // 未识别归属的节点一律算 unknown，绝不默认算 HR——否则会把本人发言当成待回复问题。
        const ownerSelectors = [
            '.item-myself', '.message-self', '.is-self', '.msg-self',
            '.item-friend', '.message-friend', '.is-friend', '.msg-friend',
            '.item-system', '.system-card'
        ].join(',');
        let owner = msg.matches(ownerSelectors)
            ? msg
            : (msg.closest(ownerSelectors) || msg.querySelector(ownerSelectors));
        const ownerClass = owner ? String(owner.className || '') : '';
        const isMe = /(^|\s)(item-myself|message-self|is-self|msg-self)(\s|$)/.test(ownerClass);
        const isHr = /(^|\s)(item-friend|message-friend|is-friend|msg-friend)(\s|$)/.test(ownerClass);
        const isSystem = /(^|\s)(item-system|system-card)(\s|$)/.test(ownerClass);
        const text = collectVisibleText(msg);
        if (text) {
            results.push({
                sender: isSystem ? 'system' : (isMe ? 'me' : (isHr ? 'hr' : 'unknown')),
                text: text.substring(0, 500),
                kind: isResumeRequestCard(text) ? 'resume_request_card' : 'message'
            });
        }
    });
    return JSON.stringify(results);
})()
"""

# JS: Check if resume dialog appeared
JS_CHECK_RESUME_DIALOG = """
(async () => {
    for (let i = 0; i < 10; i++) {
        const dialog = document.querySelector('.choose-resume-dialog');
        if (dialog && dialog.offsetHeight > 0) {
            const items = dialog.querySelectorAll('.list-item');
            const names = Array.from(items).map(it => {
                const n = it.querySelector('.resume-name');
                return n ? n.textContent.trim() : '';
            });
            return JSON.stringify({success: true, items: names.length, names: names});
        }
        await new Promise(r => setTimeout(r, 300));
    }
    return JSON.stringify({success: false, error: 'dialog_not_appeared'});
})()
"""

# JS: Verify resume was sent
JS_VERIFY_RESUME_SENT = """
(async () => {
    await new Promise(r => setTimeout(r, 2000));
    const msgs = document.querySelectorAll('.message-item');
    const lastEight = Array.from(msgs).slice(-8);
    for (const m of lastEight) {
        const text = m.textContent.trim();
        if (text.includes('附件简历请求已发送') || text.includes('联系方式已隐藏') || text.includes('简历已发送')) {
            return JSON.stringify({sent: true});
        }
    }
    return JSON.stringify({sent: false});
})()
"""

# JS: 在已打开的聊天页填入消息并点击发送按钮。
# 与 sender.py 的招呼语发送保持一致：用原生 setter + input 事件让
# Vue/React 的 v-model 同步到 data，再点击发送按钮，而不是直接调内部
# handleSubmit——旧做法 innerText 不触发 v-model，handleSubmit 读到的
# 是空内容直接 return，但 JS 不抛异常，导致"假成功"。
# 返回点击前的消息数量，供调用方轮询验证消息真的发出。
JS_SEND_CHAT_MESSAGE = r"""
(async () => {
    const input = document.querySelector(%(input_selector)s);
    if (!input) return JSON.stringify({success: false, error: 'no_chat_input'});

    input.focus();
    if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
        const prototype = input instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, %(message)s);
    } else {
        input.innerText = %(message)s;
        input.textContent = %(message)s;
    }
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: %(message)s
    }));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: ' '}));
    input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: ' '}));
    // 等 Vue/React 根据输入事件刷新发送按钮的 disabled 状态。
    await new Promise(resolve => setTimeout(resolve, 150));

    const sendSelectors = [
        '[ka="chat_send"]', '[ka^="chat_send"]',
        '.send-message', '.btn-send', '.send-btn', '.chat-send', '.chat-send-btn'
    ];
    const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const isDisabled = (el) => {
        if (el.disabled) return true;
        if (el.getAttribute('aria-disabled') === 'true') return true;
        // BOSS 用 class 名里的 disable/disabled 标记禁用态（实测）。
        return /(^|[\s_-])disable(d)?([\s_-]|$)/.test(String(el.className || '').toLowerCase());
    };
    const candidates = sendSelectors.flatMap(sel =>
        Array.from(document.querySelectorAll(sel)).map(el => ({el, sel}))
    );
    const score = (it) => {
        const text = (it.el.innerText || it.el.textContent || '').trim();
        let v = isVisible(it.el) ? 1000 : 0;
        if (it.sel === '[ka="chat_send"]') v += 200;
        if (it.sel === '.send-message') v += 120;
        if (text === '发送') v += 80;
        if (text.includes('发送')) v += 30;
        if (it.el.classList && it.el.classList.contains('btn-send')) v += 40;
        return v;
    };
    candidates.sort((a, b) => score(b) - score(a));
    const btn = candidates.find(it => score(it) > 0 && !isDisabled(it.el));
    if (!btn) {
        const any = candidates.find(it => score(it) > 0);
        return JSON.stringify(any
            ? {success: false, error: 'send_button_disabled'}
            : {success: false, error: 'no_send_button'});
    }
    // 记录点击前的消息数量，用于后续验证消息真的进入列表。
    const list = document.querySelector('.message-list, .chat-record, .message-content, [class*="message-list"]');
    const beforeCount = list ? list.children.length : -1;
    btn.el.click();
    return JSON.stringify({success: true, clicked: true, before_count: beforeCount});
})()
"""

# JS: 发送后验证——输入框已清空（发送成功的强信号）或消息列表数量增长。
# BOSS 发送成功必清空输入框（v-model 重置），发送失败则保留内容；
# 消息列表数量增长是辅助信号，避免输入框选择器在部分页面漂移时误判。
JS_CHAT_SEND_VERIFY = r"""
(() => {
    const input = document.querySelector(%(input_selector)s);
    let currentText = '';
    if (input) {
        if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {
            currentText = input.value || '';
        } else {
            currentText = input.innerText || input.textContent || '';
        }
    }
    const inputCleared = input && !currentText.trim();
    const list = document.querySelector('.message-list, .chat-record, .message-content, [class*="message-list"]');
    const listCount = list ? list.children.length : -1;
    return JSON.stringify({input_cleared: inputCleared, list_found: !!list, list_count: listCount});
})()
"""


def _call_claude(prompt: str, config: dict) -> str | None:
    """Call Claude API and return response text."""
    try:
        return call_anthropic_text(prompt, config, 500)
    except Exception as e:
        console.print(f"[red]API 调用失败: {e}[/red]")
        return None


def _detect_rejection(messages: list[dict]) -> bool:
    """Check if HR is rejecting in messages AFTER user's last reply."""
    rejection_keywords = ["不合适", "不匹配", "不太合适", "暂时没有", "不符合", "不太符合",
                          "很遗憾", "无法推进", "不考虑", "已招满", "岗位已关闭", "对不起"]
    # Only check HR messages after my last reply
    hr_msgs_after = _get_hr_messages_after_last_reply(messages)
    for msg in hr_msgs_after:
        text = msg["text"]
        if _looks_like_non_rejection_action_card(text):
            continue
        for kw in rejection_keywords:
            if kw in text:
                return True
    return False


def _looks_like_non_rejection_action_card(text: str) -> bool:
    """Ignore BOSS system cards whose button labels look like rejection words."""
    text = text or ""
    if _looks_like_resume_request_card(text):
        return True
    if "您是否接受此工作地点" in text and "暂不考虑" in text and "可以接受" in text:
        return True
    return False


def _looks_like_resume_request_card(text: str) -> bool:
    """Detect BOSS rich-card requests for the user's attachment resume.

    This is intentionally stricter than the normal text-message resume detector:
    card handling should only trigger when strong attachment-resume wording appears
    with an action or request signal.
    """
    text = text or ""
    attachment_signals = ["附件简历", "您的附件简历", "我的附件简历"]
    intent_signals = ["是否同意", "同意", "想要一份", "请求", "获取", "发送", "发给"]
    rejection_context = ["不匹配", "不合适", "不太合适", "不符合", "不太符合", "很遗憾", "无法推进", "祝", "已招满", "岗位已关闭"]

    if any(kw in text for kw in rejection_context):
        return False
    return any(kw in text for kw in attachment_signals) and any(kw in text for kw in intent_signals)


def _is_short_resume_acknowledgement(text: str) -> bool:
    """Treat standalone positive HR acknowledgements as resume intent."""
    normalized = "".join(str(text or "").split()).strip("，,。.!！?？~～…")
    return normalized in {"好", "好的", "可以"}


def _detect_resume_request(messages: list[dict]) -> bool:
    """Check if HR is asking for a resume in messages AFTER user's last reply.

    Excludes messages that are actually rejections containing the word '简历'.
    Also detects BOSS rich cards requesting the user's attachment resume.
    """
    resume_keywords = ["简历", "简历发", "发一份简历", "看看简历", "发个简历", "发下简历",
                       "附件", "发一下简历", "方便发", "看看你的简历"]
    # Rejection context: if '简历' appears alongside rejection phrases, it's NOT a request
    rejection_context = ["不匹配", "不合适", "不太合适", "不符合", "不太符合", "很遗憾",
                         "无法推进", "祝", "已招满", "岗位已关闭"]
    # Only check HR messages after my last reply
    hr_msgs_after = _get_hr_messages_after_last_reply(messages)
    for msg in hr_msgs_after:
        text = msg["text"]
        # Skip if this message contains rejection context
        has_rejection = any(kw in text for kw in rejection_context)
        if has_rejection:
            continue
        if msg.get("kind") == "resume_request_card" or _looks_like_resume_request_card(text):
            return True
        if _is_short_resume_acknowledgement(text):
            return True
        for kw in resume_keywords:
            if kw in text:
                return True
    return False


def _has_resume_request_card(messages: list[dict]) -> bool:
    """Check for BOSS resume request cards in HR messages after the user's last reply."""
    hr_msgs_after = _get_hr_messages_after_last_reply(messages)
    for msg in hr_msgs_after:
        text = msg.get("text", "")
        if msg.get("kind") == "resume_request_card" or _looks_like_resume_request_card(text):
            return True
    return False


def _get_hr_messages_after_last_reply(messages: list[dict]) -> list[dict]:
    """Get HR messages that came AFTER the user's last reply.

    If user never replied, return all HR messages.
    """
    # Find the index of user's last message
    last_my_idx = -1
    for i, msg in enumerate(messages):
        if msg["sender"] == "me":
            last_my_idx = i

    # Get HR messages after that point
    after = messages[last_my_idx + 1:] if last_my_idx >= 0 else messages
    return [m for m in after if m["sender"] == "hr"]


def _deduplicate_conversation_messages(messages: list[dict]) -> list[dict]:
    """Collapse adjacent duplicate DOM results without removing real repeated replies."""
    deduplicated: list[dict] = []
    for message in messages:
        normalized = " ".join(str(message.get("text", "")).split())
        identity = (
            str(message.get("sender", "")),
            str(message.get("kind", "message")),
            normalized,
        )
        if deduplicated:
            previous = deduplicated[-1]
            previous_identity = (
                str(previous.get("sender", "")),
                str(previous.get("kind", "message")),
                " ".join(str(previous.get("text", "")).split()),
            )
            if identity == previous_identity:
                continue
        deduplicated.append(message)
    return deduplicated


def _get_actionable_messages_after_last_reply(messages: list[dict]) -> list[dict]:
    """Return HR messages plus actionable platform cards after the user's last reply.

    BOSS may render an attachment-resume request as a system card. Keeping that card
    in the reply context lets the AI answer it, while unrelated system cards stay out
    of the pending-question fingerprint and dashboard text.
    """
    last_my_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("sender") == "me":
            last_my_idx = i

    after = messages[last_my_idx + 1:] if last_my_idx >= 0 else messages
    return [
        msg
        for msg in after
        if msg.get("sender") == "hr"
        or msg.get("kind") == "resume_request_card"
        or _looks_like_resume_request_card(str(msg.get("text", "")))
    ]


def _truncate_text(text: str, limit: int) -> str:
    """Limit text length for history payloads."""
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _build_reply_detail(messages: list[dict], ai_reply: str, schema: str = "reply_pending.v1") -> str:
    """Build structured history detail containing the HR question and AI reply."""
    hr_messages = _get_actionable_messages_after_last_reply(messages)
    hr_question = "\n".join(str(msg.get("text", "")) for msg in hr_messages[-3:]).strip()
    reply_fingerprint = _reply_fingerprint_from_hr_question(hr_question)
    payload = {
        "schema": schema,
        "hr_question": _truncate_text(hr_question, 1000),
        "resume_requested": _detect_resume_request(messages)
        or any(
            msg.get("kind") == "resume_request_card"
            or _looks_like_resume_request_card(str(msg.get("text", "")))
            for msg in hr_messages
        ),
        "reply_fingerprint": reply_fingerprint,
        "ai_reply": _truncate_text(ai_reply or "", 1000),
        "conversation_tail": [
            {
                "sender": str(msg.get("sender", "")),
                "text": _truncate_text(str(msg.get("text", "")), 500),
            }
            for msg in messages[-6:]
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_resume_failure_detail(messages: list[dict], failure_reason: str) -> str:
    """Build a resume failure payload without mixing system errors into HR text."""
    payload = json.loads(_build_reply_detail(messages, "", "resume_failed.v2"))
    payload["system_reason"] = _truncate_text(
        failure_reason or "定制简历生成失败，未获得更具体的错误信息",
        2000,
    )
    return json.dumps(payload, ensure_ascii=False)


def _parse_reply_detail(detail: str) -> dict:
    """Parse structured reply detail, returning an empty dict for legacy text rows."""
    if not isinstance(detail, str) or not detail.strip().startswith("{"):
        return {}
    try:
        payload = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _reply_fingerprint_from_hr_question(hr_question: str) -> str:
    """Build a stable fingerprint for the HR message currently awaiting a decision."""
    return " ".join(str(hr_question or "").split())


def _reply_fingerprint_from_messages(messages: list[dict]) -> str:
    hr_messages = _get_actionable_messages_after_last_reply(messages)
    hr_question = "\n".join(str(msg.get("text", "")) for msg in hr_messages[-3:]).strip()
    return _reply_fingerprint_from_hr_question(hr_question)


def _reply_fingerprint_from_detail(detail: str) -> str:
    payload = _parse_reply_detail(detail)
    fingerprint = payload.get("reply_fingerprint") or payload.get("pending_reply_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return _reply_fingerprint_from_hr_question(fingerprint)
    hr_question = payload.get("hr_question") or payload.get("pending_hr_question")
    return _reply_fingerprint_from_hr_question(hr_question) if isinstance(hr_question, str) else ""


def _build_reply_resolution_detail(
    schema: str,
    note: str,
    pending_detail: str,
    manual_reply: str = "",
    pending_history_id: int | None = None,
) -> str:
    """Build structured history detail for manual reply decisions."""
    pending_payload = _parse_reply_detail(pending_detail)
    pending_hr_question = pending_payload.get("hr_question") if isinstance(pending_payload.get("hr_question"), str) else ""
    payload = {
        "schema": schema,
        "note": note,
        "pending_history_id": pending_history_id,
        "pending_reply_fingerprint": _reply_fingerprint_from_detail(pending_detail),
        "pending_hr_question": _truncate_text(pending_hr_question, 1000),
        "manual_reply": _truncate_text(manual_reply, 1000),
    }
    return json.dumps(payload, ensure_ascii=False)


def _check_if_i_already_replied(messages: list[dict]) -> bool:
    """Check if I (the user) already replied after the last HR message.

    Returns True if the last message in conversation is from 'me'.
    """
    if not messages:
        return False
    return messages[-1]["sender"] == "me"


def _check_if_portfolio_sent(messages: list[dict], portfolio_url: str = "") -> bool:
    """Check if portfolio URL was already sent in conversation."""
    if not portfolio_url:
        return True  # No portfolio configured, treat as already sent
    for msg in messages:
        if msg["sender"] == "me" and portfolio_url in msg["text"]:
            return True
    return False


def _generate_auto_reply(messages: list[dict], job: dict, config: dict) -> str | None:
    """Generate a natural reply based on conversation context."""
    # Build conversation context
    sender_labels = {"me": "我", "hr": "HR", "system": "平台卡片"}
    conv_text = "\n".join(
        f"{sender_labels.get(str(message.get('sender')), '平台消息')}: {message.get('text', '')}"
        for message in messages[-10:]
    )

    # Read resume summary from file
    from pathlib import Path
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    resume_summary = resume_path.read_text(encoding="utf-8")[:800] if resume_path.exists() else "（未配置简历）"

    prompt = f"""你是一位求职者，正在BOSS直聘上和HR沟通。请根据对话上下文生成一条自然、礼貌的回复。

## 对话记录
{conv_text}

## 我的背景
{resume_summary}

## 岗位信息
- 职位：{job.get('title', '')}
- 公司：{job.get('company', '')}

## 要求
1. 根据HR最后一条消息的意图来回复，自然对话
2. 语气像真人在手机上打字，不要太正式
3. 字数控制在30-100字
4. 不要重复之前已经说过的内容
5. 如果HR在约面试时间，积极配合
6. 不要捏造经历或头衔
7. 如果HR既索要简历又提出问题，回复中必须同时回应这两个意图
8. 如果HR只索要简历，礼貌确认会发送即可，不要声称已经发送

请直接输出回复文本，不要加任何标记。"""

    return _call_claude(prompt, config)


def _refine_reply(hr_question: str, current_reply: str, suggestion: str, config: dict) -> str | None:
    """Refine a pending reply according to the user's explicit revision suggestion."""
    prompt = f"""你正在帮助求职者修改一条准备发给 HR 的回复。

## HR 的消息
{hr_question}

## 当前回复
{current_reply}

## 用户的修改建议
{suggestion}

## 要求
1. 严格落实用户的修改建议，同时保持礼貌、自然、适合 BOSS 直聘聊天。
2. 不得捏造用户未提供的经历、能力、时间或承诺。
3. 保留当前回复中未被修改建议否定的有效信息。
4. 只输出优化后的回复正文，不要解释修改过程，不要添加引号或标题。
"""
    return _call_claude(prompt, config)


def _generate_reply_from_text(
    hr_message: str,
    job: dict | None,
    config: dict,
    conversation_history: str = "",
) -> str | None:
    """根据用户手动粘贴的 HR 消息文本生成回复语。

    监测不到 HR 回复时（列表扫描漏掉、会话没进候选等），用户可以把 HR 的话
    粘贴进来，本函数复用与 _generate_auto_reply 一致的回复生成规则，但不依赖
    浏览器抓取的 messages 列表，也不写任何数据库/历史——只返回回复语文本，
    由用户自行复制到 BOSS 发送。

    Args:
        hr_message: 用户粘贴的 HR 消息原文（必填）。
        job: 可选的岗位上下文（title/company），用于让回复更贴合岗位；为空时省略岗位段。
        config: 配置字典，用于读取 resume_path 和 AI 凭据。
        conversation_history: 可选的之前对话记录（用户也可粘贴），帮助模型理解上下文。

    Returns:
        生成的回复语文本，或 None（AI 调用失败）。
    """
    hr_message = (hr_message or "").strip()
    if not hr_message:
        return None

    # 复用 _generate_auto_reply 的简历摘要读取逻辑，保持回复风格一致。
    from pathlib import Path
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    resume_summary = resume_path.read_text(encoding="utf-8")[:800] if resume_path.exists() else "（未配置简历）"

    job = job or {}
    title = str(job.get("title", "") or "").strip()
    company = str(job.get("company", "") or "").strip()
    job_section = (
        f"- 职位：{title}\n- 公司：{company}" if (title or company) else "（未提供岗位信息）"
    )

    history_section = conversation_history.strip() if conversation_history.strip() else "（无）"

    prompt = f"""你是一位求职者，正在BOSS直聘上和HR沟通。用户手动把HR的最新消息粘贴给你，请生成一条自然、礼貌的回复。

## HR 的最新消息
{hr_message}

## 之前的对话记录（如有）
{history_section}

## 我的背景
{resume_summary}

## 岗位信息
{job_section}

## 要求
1. 根据HR消息的意图来回复，自然对话
2. 语气像真人在手机上打字，不要太正式
3. 字数控制在30-100字
4. 如果HR在约面试时间，积极配合
5. 不要捏造经历或头衔
6. 如果HR既索要简历又提出问题，回复中必须同时回应这两个意图
7. 如果HR只索要简历，礼貌确认会发送即可，不要声称已经发送

请直接输出回复文本，不要加任何标记。"""

    return _call_claude(prompt, config)


def _verify_chat_message_sent(target_id: str, before_count: int, input_selector: str, attempts: int = 12) -> bool:
    """轮询确认消息真的发出。

    两个成功信号，任一满足即判定成功：
    1) 输入框内容已清空——BOSS 发送成功必清空输入框，发送失败则保留内容。
       这是主信号，不依赖消息列表选择器（避免类名漂移导致"发送成功却判定失败"）。
    2) 消息列表数量增长——辅助信号，防输入框选择器在部分页面不匹配。

    Args:
        target_id: 聊天页 target id。
        before_count: 点击发送前消息列表的子元素数量；-1 表示未找到消息列表。
        input_selector: 聊天输入框的 CSS 选择器（与发送时一致）。
        attempts: 最多轮询次数（每次间隔 0.5s）。
    """
    verify_js = JS_CHAT_SEND_VERIFY % {"input_selector": json.dumps(input_selector, ensure_ascii=False)}
    for _ in range(max(1, attempts)):
        time.sleep(0.5)
        raw = evaluate(target_id, verify_js)
        if not raw:
            continue
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if state.get("input_cleared"):
            return True
        if state.get("list_found"):
            count = int(state.get("list_count", -1))
            # 能找到消息列表：要求数量严格增长；找不到（-1）：要求至少出现一条。
            if before_count >= 0:
                if count > before_count:
                    return True
            elif count > 0:
                return True
    return False


def _send_message_in_chat(target_id: str, message: str) -> bool:
    """在已打开的聊天页发送一条文本消息，并验证消息确实发出。

    Returns:
        True 仅当消息真正出现在聊天列表里；否则 False。
        绝不基于"JS 没抛异常"就判定成功——点击发送按钮后必须轮询验证，
        否则会像旧实现一样出现"显示成功但实际未回复"的假成功。
    """
    js_send = JS_SEND_CHAT_MESSAGE % {
        "input_selector": json.dumps(CHAT_INPUT_SELECTOR, ensure_ascii=False),
        "message": json.dumps(message, ensure_ascii=False),
    }
    result = evaluate(target_id, js_send)
    if not result:
        return False
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return False

    if not data.get("success") or not data.get("clicked"):
        return False

    try:
        before_count = int(data.get("before_count")) if data.get("before_count") is not None else -1
    except (TypeError, ValueError):
        before_count = -1

    if not _verify_chat_message_sent(target_id, before_count, CHAT_INPUT_SELECTOR):
        console.print("[yellow]    消息未确认发出（输入框未清空且消息列表未增长），可能被拦截或失败[/yellow]")
        return False
    return True


def _build_stable_chat_url(conversation_id: object, job_id: object) -> str | None:
    """根据稳定会话标识构造不含临时安全令牌的聊天地址。"""
    conversation_value = str(conversation_id or "").strip()
    job_value = str(job_id or "").strip()
    if not conversation_value or not job_value:
        return None
    return f"https://www.zhipin.com/web/geek/chat?{urlencode({'id': conversation_value, 'jobId': job_value})}"


def _parse_job_chat_redirect(redirect_url: object, job_id: str) -> tuple[str, str] | None:
    """校验岗位页聊天地址，并返回稳定会话标识与聊天 URL。"""
    if not isinstance(redirect_url, str) or not redirect_url.startswith("/web/geek/chat?"):
        return None
    query = parse_qs(urlparse(redirect_url).query)
    conversation_id = query.get("id", [""])[0]
    redirect_job_id = query.get("jobId", [""])[0]
    if not conversation_id or redirect_job_id != job_id:
        return None
    stable_url = _build_stable_chat_url(conversation_id, job_id)
    return (conversation_id, stable_url) if stable_url else None


def _conversation_page_state(target_id: str) -> dict:
    """读取聊天页是否存在可见输入框及当前选中联系人。"""
    raw = evaluate(
        target_id,
        """
        (() => {
            const input = document.querySelector('#chat-input');
            const inputVisible = !!input && !!(
                input.offsetWidth ||
                input.offsetHeight ||
                input.getClientRects().length
            );
            const selected = document.querySelector('.friend-content.selected');
            return JSON.stringify({
                success: inputVisible,
                input_visible: inputVisible,
                selected_text: selected ? (selected.innerText || selected.textContent || '').trim() : ''
            });
        })()
        """,
    )
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _wait_for_conversation_ready(target_id: str, attempts: int = 20) -> bool:
    """等待具体聊天会话加载完成，而不是只等待聊天列表页 URL。"""
    for _ in range(max(1, attempts)):
        if _conversation_page_state(target_id).get("input_visible"):
            return True
        time.sleep(0.5)
    return False


def _conversation_state_matches_job(state: dict, job: dict) -> bool:
    """验证列表点击后的选中联系人属于目标岗位。"""
    if not state.get("input_visible"):
        return False

    selected_text = "".join(str(state.get("selected_text") or "").split())
    if not selected_text:
        return False

    hr_name = "".join(str(job.get("hr_name") or "").split())
    if hr_name:
        return hr_name in selected_text

    company = "".join(str(job.get("company") or "").split())
    company_prefix = company[:4]
    return bool(company_prefix and company_prefix in selected_text)


def _wait_for_selected_conversation(target_id: str, job: dict, attempts: int = 10) -> bool:
    """等待列表点击生效，并确认选中的联系人没有串会话。"""
    for _ in range(max(1, attempts)):
        if _conversation_state_matches_job(_conversation_page_state(target_id), job):
            return True
        time.sleep(0.5)
    return False


def _remember_conversation_id(job_id: str, conversation_id: str) -> None:
    """把旧岗位恢复出的稳定会话标识写回数据库。"""
    db = get_db()
    try:
        update_job_conversation_id(db, job_id, conversation_id)
    finally:
        db.close()


def _open_conversation(job: dict, config: dict) -> str | None:
    """精确打开 HR 会话，失败时才回退到聊天列表文本匹配。

    Strategies tried in order:
      A) 使用数据库保存的 conversation_id 直达会话
      B) 从岗位详情页读取"继续沟通"的精确地址并回填 conversation_id
      C) 聊天列表按 HR 名称 + 公司匹配，点击后再次验证
    """
    stable_url = _build_stable_chat_url(job.get("conversation_id"), job.get("id"))
    if stable_url:
        target_id = new_tab(stable_url)
        if target_id and _wait_for_conversation_ready(target_id):
            return target_id
        if target_id:
            close_tab(target_id)

    job_url = job.get("url", "")

    # 老数据恢复：只读取“继续沟通”的精确地址，不触发“立即沟通”。
    if job_url:
        for attempt in range(2):
            target_id = new_tab(job_url)
            if not target_id:
                if attempt == 0:
                    time.sleep(3)
                    continue
                break

            time.sleep(3)
            wait_for_load(target_id, timeout=10)
            time.sleep(1)

            raw = evaluate(
                target_id,
                """
                (() => {
                    const buttons = Array.from(document.querySelectorAll('a.btn-startchat[redirect-url]'));
                    const button = buttons.find((el) => {
                        const text = (el.innerText || el.textContent || '').trim();
                        const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        return visible && text.includes('继续沟通');
                    });
                    return JSON.stringify({
                        success: !!button,
                        button_text: button ? (button.innerText || button.textContent || '').trim() : '',
                        redirect_url: button ? button.getAttribute('redirect-url') : ''
                    });
                })()
                """,
            )
            try:
                button_state = raw if isinstance(raw, dict) else json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                button_state = {}

            redirect = _parse_job_chat_redirect(button_state.get("redirect_url"), str(job["id"]))
            if button_state.get("success") and redirect:
                conversation_id, precise_url = redirect
                if navigate(target_id, precise_url) and _wait_for_conversation_ready(target_id):
                    _remember_conversation_id(str(job["id"]), conversation_id)
                    return target_id

            close_tab(target_id)
            if attempt == 0:
                time.sleep(2)

    # 兼容旧数据和页面异常：最后才从列表按文本匹配。
    target_id = _open_conversation_from_chat_list(job, config)
    return target_id


def _open_conversation_from_chat_list(job: dict, config: dict) -> str | None:
    """Open conversation from chat list. Matches by name+company, then company-only fallback."""
    monitor_cfg = config.get("monitor", {})
    chat_url = monitor_cfg.get("chat_url", "https://www.zhipin.com/web/geek/chat")
    target_id = new_tab(chat_url)
    if not target_id:
        return None

    time.sleep(4)
    wait_for_load(target_id, timeout=10)
    time.sleep(3)

    hr_name = (job.get("hr_name") or "").strip()
    company = (job.get("company") or "").strip()

    hr_name_json = json.dumps(hr_name, ensure_ascii=False)
    company_json = json.dumps(company, ensure_ascii=False)

    # Build JS that tries: 1) exact hr_name+company match, 2) company-only match
    js_click_chat = f"""
    (async () => {{
        const items = document.querySelectorAll('li[role=listitem]');
        let companyOnlyMatch = null;

        for (const item of items) {{
            const nameEl = item.querySelector('.name-text');
            const nameBox = item.querySelector('.name-box');
            const spans = nameBox ? nameBox.querySelectorAll('span') : [];
            const name = (nameEl ? nameEl.textContent : '').trim();
            const comp = spans.length >= 2 ? spans[1].textContent.trim() : '';

            const hrName = {hr_name_json};
            const targetComp = {company_json};

            // Exact match: HR name + company
            if (hrName && name === hrName && (comp.includes(targetComp) || targetComp.includes(comp))) {{
                const fc = item.querySelector('.friend-content') || item;
                const rect = fc.getBoundingClientRect();
                const x = rect.x + rect.width / 2;
                const y = rect.y + rect.height / 2;
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
                    fc.dispatchEvent(new PointerEvent(type, {{
                        bubbles: true, cancelable: true, composed: true,
                        clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse'
                    }}));
                }}
                await new Promise(r => setTimeout(r, 2000));
                return JSON.stringify({{success: true, match: 'exact'}});
            }}

            // Company-only match (save first match as fallback)
            if (!companyOnlyMatch && targetComp && (comp.includes(targetComp) || targetComp.includes(comp))) {{
                companyOnlyMatch = item;
            }}
        }}

        // Fallback: company-only match (when hr_name is empty or not found)
        if (companyOnlyMatch) {{
            const fc = companyOnlyMatch.querySelector('.friend-content') || companyOnlyMatch;
            const rect = fc.getBoundingClientRect();
            const x = rect.x + rect.width / 2;
            const y = rect.y + rect.height / 2;
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
                fc.dispatchEvent(new PointerEvent(type, {{
                    bubbles: true, cancelable: true, composed: true,
                    clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse'
                }}));
            }}
            await new Promise(r => setTimeout(r, 2000));
            return JSON.stringify({{success: true, match: 'company_only'}});
        }}

        return JSON.stringify({{success: false, error: 'conversation_not_found', total: items.length}});
    }})()
    """

    result = evaluate(target_id, js_click_chat)
    if not result:
        close_tab(target_id)
        return None

    try:
        click_result = json.loads(result) if isinstance(result, str) else result
        if click_result.get("success"):
            if _wait_for_selected_conversation(target_id, job):
                return target_id
    except (json.JSONDecodeError, TypeError):
        pass

    # Retry: scroll chat list to load more and try again
    console.print("[dim]    对话列表未找到，尝试滚动加载...[/dim]")
    evaluate(target_id, """
    (() => {
        const nav = document.querySelector('nav') || document.querySelector('.chat-list');
        if (nav) nav.scrollTop = nav.scrollHeight;
    })()
    """)
    time.sleep(3)

    result = evaluate(target_id, js_click_chat)
    if result:
        try:
            click_result = json.loads(result) if isinstance(result, str) else result
            if click_result.get("success"):
                if _wait_for_selected_conversation(target_id, job):
                    return target_id
        except (json.JSONDecodeError, TypeError):
            pass

    close_tab(target_id)
    return None


def _deliver_resume_to_chat(target_id: str) -> bool:
    """Send platform resume in an already-open chat conversation.

    Flow: Click "发简历" → select resume → confirm send
    """
    # Click "发简历" button
    if not click(target_id, '[d-c="62009"]'):
        console.print("[dim]    未找到发简历按钮[/dim]")
        return False

    time.sleep(2)

    # Wait for resume selection dialog
    dialog_result = evaluate(target_id, JS_CHECK_RESUME_DIALOG)
    if not dialog_result:
        return False

    try:
        dialog_data = json.loads(dialog_result) if isinstance(dialog_result, str) else dialog_result
    except (json.JSONDecodeError, TypeError):
        return False

    if not dialog_data.get("success"):
        console.print(f"[dim]    简历选择框未出现: {dialog_data.get('error', '')}[/dim]")
        return False

    console.print(f"[dim]    可选简历: {dialog_data.get('names', [])}[/dim]")

    # Select first resume item
    if not click(target_id, ".choose-resume-dialog .list-item"):
        return False
    time.sleep(1)

    # Click send button
    if not click(target_id, ".choose-resume-dialog .btn-confirm"):
        return False
    time.sleep(2)

    # Verify success
    verify_result = evaluate(target_id, JS_VERIFY_RESUME_SENT)
    if verify_result:
        try:
            verify_data = json.loads(verify_result) if isinstance(verify_result, str) else verify_result
            if verify_data.get("sent"):
                return True
        except (json.JSONDecodeError, TypeError):
            pass

    console.print("[dim]    发送验证不确定，可能已发送[/dim]")
    return True  # Optimistic - resume dialog appeared and we clicked send


def check_replies(config: dict) -> list[dict]:
    """Open BOSS chat page and detect conversations with HR replies.

    Returns list of conversations with replies (including matched job info).
    """
    db = get_db()
    monitor_cfg = config.get("monitor", {})
    chat_url = monitor_cfg.get("chat_url", "https://www.zhipin.com/web/geek/chat")

    # Get jobs we've sent greetings to (or already replied/resume_sent/follow_up_sent/needs_resume - keep monitoring)
    sent_jobs = get_jobs_by_status(db, "sent")
    replied_jobs = get_jobs_by_status(db, "replied")
    resume_sent_jobs = get_jobs_by_status(db, "resume_sent")
    follow_up_jobs = get_jobs_by_status(db, "follow_up_sent")
    needs_resume_jobs = get_jobs_by_status(db, "needs_resume")
    manual_jobs = get_jobs_by_status(db, "manual")
    all_tracked_jobs = sent_jobs + replied_jobs + resume_sent_jobs + follow_up_jobs + needs_resume_jobs + manual_jobs

    console.print(f"[bold]扫描聊天列表，并核对 {len(all_tracked_jobs)} 个已跟踪对话...[/bold]")

    # Open chat page
    target_id = new_tab(chat_url)
    if not target_id:
        console.print("[red]无法打开聊天页面[/red]")
        db.close()
        return []

    time.sleep(3)
    wait_for_load(target_id, timeout=10)
    time.sleep(2)

    # Extract chat list
    raw = evaluate(target_id, JS_EXTRACT_CHAT_LIST)
    close_tab(target_id)

    if not raw:
        console.print("[yellow]未能获取聊天列表[/yellow]")
        db.close()
        return []

    try:
        conversations = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        console.print("[yellow]聊天列表解析失败[/yellow]")
        db.close()
        return []

    if not conversations:
        console.print("[dim]聊天列表为空[/dim]")
        db.close()
        return []

    console.print(f"[dim]获取到 {len(conversations)} 条对话[/dim]")

    # Match conversations to tracked jobs and collect candidates for inspection.
    # ⚠ 这里只做“候选收集”，绝不基于列表预览修改岗位状态或写 HR 回复历史——
    # 预览文本 last_message 是“会话里最后一条消息”，往往就是我自己发出的招呼/
    # 回复。如果在这里就把 sent→replied，会把“我发的消息”误判成“HR回复”。
    # 真正是否需要回复，交给 _handle_conversation 打开完整会话、按 .item-myself
    # 归属和“我是否已回复过”判定。状态推进也挪到那里之后统一做。
    # 注意：匹配不到跟踪岗位的会话（HR 主动打招呼、用户在 BOSS 上手动聊的）
    # 也要处理——为它们建一条影子岗位记录，纳入自动回复监测，而不是仅提示。
    results = []
    for conv in conversations:
        if not conv.get("needs_inspection", conv.get("has_reply", False)):
            continue

        matched_job = _match_conversation_to_job(conv, all_tracked_jobs)
        if matched_job:
            # 不在此处改状态、不写“HR回复”历史。预览未核验，先标“待核验”。
            console.print(
                f"[dim]  · {matched_job['company']} - {matched_job['title']} 待打开核验[/dim]"
            )
        else:
            # 未跟踪会话：建影子岗位，复用 _handle_conversation 生成回复语。
            matched_job = _ensure_manual_job(db, conv)
            if matched_job:
                console.print(
                    f"[cyan]  ↳ HR主动沟通: {conv.get('hr_name', '')}（{conv.get('company', '')}）"
                    f"已纳入监测[/cyan]"
                )
            else:
                console.print(
                    f"[yellow]  ⚠ {conv.get('hr_name', '')}（{conv.get('company', '')}）"
                    f"有待核验消息但信息不全，无法纳入自动处理，请手动查看[/yellow]"
                )

        results.append({
            "job": matched_job,
            "conversation": conv,
        })

    if not results:
        console.print("[dim]暂无新回复[/dim]")

    db.close()
    return results


def _match_conversation_to_job(conv: dict, jobs: list[dict]) -> dict | None:
    """Match a chat conversation to a job record."""
    conv_hr = conv.get("hr_name", "").strip()
    conv_company = conv.get("company", "").strip()

    if not conv_hr:
        return None

    # Exact match first (hr_name + company)
    for job in jobs:
        job_hr = (job.get("hr_name") or "").strip()
        job_company = (job.get("company") or "").strip()
        if conv_hr == job_hr and conv_company == job_company:
            return job

    # Fuzzy match by hr_name + partial company
    for job in jobs:
        job_hr = (job.get("hr_name") or "").strip()
        job_company = (job.get("company") or "").strip()
        if conv_hr == job_hr and job_company and (
            conv_company in job_company or job_company in conv_company
        ):
            return job

    # Fallback: job has no hr_name, match by company name only
    for job in jobs:
        job_hr = (job.get("hr_name") or "").strip()
        job_company = (job.get("company") or "").strip()
        if not job_hr and job_company and (
            conv_company == job_company
            or conv_company in job_company
            or job_company in conv_company
        ):
            return job

    return None


def _manual_job_id(hr_name: str, company: str, conversation_id: str = "") -> str:
    """生成 HR 主动会话的影子岗位 id（稳定、可重现）。

    优先用会话 id（唯一），提取不到则退化为 hr_name+company 的 hash。
    """
    if conversation_id:
        return "manual_" + conversation_id
    base = f"{hr_name}|{company}"
    return "manual_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _ensure_manual_job(db, conv: dict) -> dict | None:
    """为未跟踪的 HR 主动会话在 jobs 表建一条影子岗位记录。

    只有 HR 主动打招呼、不在采集投递跟踪列表里的会话才需要。
    影子岗位用 ``manual_`` 前缀 id 隔离，status='manual'、score=0，
    不会进入投递/评分/人工确认流程，但可被监测流程复用生成回复语。
    """
    hr_name = (conv.get("hr_name") or "").strip()
    company = (conv.get("company") or "").strip()
    if not hr_name and not company:
        return None

    conversation_id = str(conv.get("conversation_id") or "").strip()
    job_id = _manual_job_id(hr_name, company, conversation_id)

    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row:
        return dict(row)

    job = {
        "id": job_id,
        "title": "HR主动沟通",
        "company": company,
        "hr_name": hr_name,
        "hr_title": "",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "salary": "",
        "city": "",
        "experience": "",
        "jd": "",
        "url": "",
    }
    insert_job(db, job)
    update_job_status(db, job_id, "manual")
    if conversation_id:
        update_job_conversation_id(db, job_id, conversation_id)
    # 返回完整数据库行，让调用方（_handle_conversation）能取到 status 等字段。
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else job


def _handle_conversation(job: dict, config: dict) -> str:
    """Handle a single conversation that has an HR reply.

    Returns action taken: 'skipped_user_replied', 'rejected', 'reply_pending', 'auto_replied', 'failed'
    """
    console.print(f"\n  [bold]处理: {job['company']} - {job['title']}[/bold]")

    # Open the conversation
    target_id = _open_conversation(job, config)
    if not target_id:
        console.print("[yellow]    无法打开对话[/yellow]")
        return "failed"

    time.sleep(2)

    # Extract full conversation messages
    raw = evaluate(target_id, JS_EXTRACT_CONVERSATION)
    if not raw:
        close_tab(target_id)
        console.print("[yellow]    无法获取对话内容[/yellow]")
        return "failed"

    try:
        messages = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        close_tab(target_id)
        return "failed"
    messages = _deduplicate_conversation_messages(messages)

    # Check if I already replied after the last HR message
    if _check_if_i_already_replied(messages):
        console.print("[dim]    我已回复，跳过本轮[/dim]")
        close_tab(target_id)
        return "skipped_user_replied"

    if not _get_actionable_messages_after_last_reply(messages):
        # 列表候选只是“需要打开核验”，完整会话中没有待回复 HR 消息时不得调用 AI。
        console.print("[dim]    完整会话中没有待回复的 HR 消息，跳过[/dim]")
        close_tab(target_id)
        return "skipped_user_replied"

    # 到这里才确认：完整会话里确有 HR 发出、且我尚未回复的消息——这才是真“HR 回复”。
    console.print("[green]    ✓ 确认 HR 回复，待生成回复建议[/green]")

    # Check if HR is rejecting
    if _detect_rejection(messages):
        console.print("[dim]    HR已拒绝，标记并停止跟踪[/dim]")
        db = get_db()
        update_job_status(db, job["id"], "rejected")
        add_history(db, job["id"], "rejected", "HR回复拒绝")
        db.close()
        close_tab(target_id)
        return "rejected"

    db = get_db()
    dismissed_pending_reply = _has_dismissed_pending_reply(db, job["id"], messages)
    resolved_same_question = _has_resolved_same_question(db, job["id"], messages)
    pending_same_question = _has_pending_same_question(db, job["id"], messages)
    db.close()
    if dismissed_pending_reply:
        console.print("[dim]    该回复建议已放弃，跳过本轮[/dim]")
        close_tab(target_id)
        return "skipped_dismissed_reply"
    if resolved_same_question:
        # 同一 HR 问题已回复/放弃过，不再重复生成待回复建议。
        console.print("[dim]    该问题已回复过，跳过本轮[/dim]")
        close_tab(target_id)
        return "skipped_dismissed_reply"
    if pending_same_question:
        # 同一问题已有未确认的待回复建议，避免每次监测循环重复生成。
        console.print("[dim]    该问题已有待确认回复建议，跳过本轮[/dim]")
        close_tab(target_id)
        return "skipped_dismissed_reply"

    # 所有未回复的 HR 消息都走同一回复生成链路；简历请求不再抢占问题回复。
    console.print("[cyan]    生成回复建议...[/cyan]")
    reply = _generate_auto_reply(messages, job, config)
    if not reply:
        console.print("[yellow]    回复生成失败[/yellow]")
        close_tab(target_id)
        return "failed"

    console.print(f"[dim]    回复内容: {reply[:80]}...[/dim]")

    if not config.get("monitor", {}).get("auto_reply_hr_questions", False):
        console.print("[yellow]    已生成回复建议，等待监测执行中确认[/yellow]")
        db = get_db()
        add_history(db, job["id"], "reply_pending", _build_reply_detail(messages, reply))
        db.close()
        close_tab(target_id)
        return "reply_pending"

    # Send the reply
    if _send_message_in_chat(target_id, reply):
        console.print("[green]    ✓ 自动回复已发送[/green]")
        db = get_db()
        add_history(db, job["id"], "auto_replied", _build_reply_detail(messages, reply))
        db.close()
        close_tab(target_id)
        return "auto_replied"
    else:
        console.print("[yellow]    ! 发送失败[/yellow]")
        close_tab(target_id)
        return "failed"


def _row_text(row, key: str) -> str:
    """Read a text value from sqlite Row/dict-like objects defensively."""
    if row is None:
        return ""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def _has_generated_resume_for_job(db, job_id: str) -> bool:
    """Return true when this job already has a generated resume/request result."""
    row = db.execute(
        "SELECT status, resume_path FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    status = _row_text(row, "status")
    resume_path = _row_text(row, "resume_path").strip()
    # needs_resume 表示简历请求已处理（已生成待发送，或简历生成已关闭仅提示），
    # 视为已处理避免下一轮监测重复生成/重复提示。
    return status in ("resume_sent", "needs_resume") or bool(resume_path)


def _has_follow_up_history(db, job_id: str) -> bool:
    """Return true when a follow-up was already recorded for this job."""
    row = db.execute(
        "SELECT 1 FROM history WHERE job_id = ? AND action = 'follow_up_sent' LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None


def _has_dismissed_pending_reply(db, job_id: str, messages: list[dict] | None = None) -> bool:
    """Return true when the latest manual reply decision was to dismiss it."""
    row = db.execute(
        """
        SELECT action, detail
        FROM history
        WHERE job_id = ?
          AND action IN ('reply_pending', 'reply_dismissed', 'auto_replied', 'replied')
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if _row_text(row, "action") != "reply_dismissed":
        return False
    dismissed_fingerprint = _reply_fingerprint_from_detail(_row_text(row, "detail"))
    if not dismissed_fingerprint or messages is None:
        return True
    return dismissed_fingerprint == _reply_fingerprint_from_messages(messages)


def _has_resolved_same_question(db, job_id: str, messages: list[dict] | None = None) -> bool:
    """Return true when the pending HR question was already replied or dismissed.

    防止同一 HR 问题在用户确认回复/放弃后，被监测器重复生成 reply_pending。
    BOSS 会话里用户已回复的消息若未被正确识别（发送态/选择器误判），
    监测器会再次看到 HR 的旧消息并重新生成待回复建议——靠 fingerprint 比对
    已回复/已放弃的记录来跳过，避免"已回复的内容还在待处理"。
    """
    if messages is None:
        return False
    current_fp = _reply_fingerprint_from_messages(messages)
    if not current_fp:
        return False
    rows = db.execute(
        """
        SELECT action, detail
        FROM history
        WHERE job_id = ?
          AND action IN ('reply_dismissed', 'replied', 'auto_replied')
        ORDER BY id DESC
        LIMIT 10
        """,
        (job_id,),
    ).fetchall()
    for row in rows:
        action = _row_text(row, "action")
        if action not in ("reply_dismissed", "replied", "auto_replied"):
            continue
        # 监测器自动记录的纯文本 replied（"HR回复: ..."）无 fingerprint，
        # 提取为空字符串，不会与当前问题误匹配。
        if _reply_fingerprint_from_detail(_row_text(row, "detail")) == current_fp:
            return True
    return False


def _has_pending_same_question(db, job_id: str, messages: list[dict] | None = None) -> bool:
    """Return true when an identical HR question already has an open reply_pending.

    同一问题在生成 reply_pending 后（用户尚未确认），下一次监测循环会再次
    命中——若不跳过，会重复生成多条相同的待回复建议。影子岗位尤其容易触发，
    因为其状态始终为 manual、每次监测都会走进生成逻辑。
    """
    if messages is None:
        return False
    current_fp = _reply_fingerprint_from_messages(messages)
    if not current_fp:
        return False
    row = db.execute(
        """
        SELECT detail
        FROM history
        WHERE job_id = ?
          AND action = 'reply_pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    fp = _reply_fingerprint_from_detail(_row_text(row, "detail"))
    return bool(fp) and fp == current_fp


def _check_follow_ups(config: dict, throttle, replied_job_ids: set | None = None) -> int:
    """Send a follow-up message to jobs that have been in 'sent' status for configured hours with no HR reply.

    replied_job_ids: set of job IDs that already had HR replies this cycle — always skip these.
    Respects config: follow_up.enabled, follow_up.interval_hours, follow_up.max_times, follow_up.skip_weekends.
    Returns count of follow-ups sent.
    """
    from datetime import datetime, timedelta

    stop_event = config.get("_workbench_stop_event")
    if not isinstance(stop_event, Event):
        stop_event = None
    if stop_event and stop_event.is_set():
        return 0
    follow_up_cfg = config.get("follow_up", {})
    if not follow_up_cfg.get("enabled", False):
        return 0

    # Skip weekends if configured
    if follow_up_cfg.get("skip_weekends", True):
        today = datetime.now().weekday()
        if today >= 5:  # Saturday=5, Sunday=6
            return 0

    interval_hours = follow_up_cfg.get("interval_hours", 48)

    db = get_db()
    sent_jobs = get_jobs_by_status(db, "sent")

    if not sent_jobs:
        db.close()
        return 0

    cutoff = datetime.now() - timedelta(hours=interval_hours)
    stale_jobs = []
    for job in sent_jobs:
        # Skip any job that already had an HR reply this cycle
        if replied_job_ids and job["id"] in replied_job_ids:
            console.print(f"[dim]  跟进跳过（本轮已有HR回复）: {job['company']}[/dim]")
            continue
        updated = job.get("updated_at", "")
        if not updated:
            continue
        try:
            job_time = datetime.fromisoformat(updated.replace("Z", "+00:00")) if "T" in updated else datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if job_time < cutoff:
            stale_jobs.append(job)

    if not stale_jobs:
        db.close()
        return 0

    console.print(f"\n[cyan]发现 {len(stale_jobs)} 个超过{interval_hours}小时无回复的对话，尝试跟进...[/cyan]")

    count = 0
    for job in stale_jobs:
        if stop_event and stop_event.is_set():
            break
        if _has_follow_up_history(db, job["id"]):
            console.print(f"[dim]  跟进跳过（已记录过跟进）: {job['company']}[/dim]")
            continue

        follow_up_msg = _generate_follow_up(job, config)
        if not follow_up_msg:
            continue

        target_id = _open_conversation(job, config)
        if not target_id:
            continue

        time.sleep(2)

        if _send_message_in_chat(target_id, follow_up_msg):
            console.print(f"[green]  ✓ 跟进: {job['company']} - {job['title']}[/green]")
            update_job_status(db, job["id"], "follow_up_sent")
            add_history(db, job["id"], "follow_up_sent", follow_up_msg[:100])
            count += 1
        else:
            console.print(f"[yellow]  ! 跟进失败: {job['company']}[/yellow]")

        close_tab(target_id)
        if throttle.wait(stop_event):
            break

    db.close()
    return count


def _generate_follow_up(job: dict, config: dict) -> str | None:
    """Generate a short follow-up message via AI."""
    interval_hours = config.get("follow_up", {}).get("interval_hours", 48)
    prompt = f"""你是一位求职者，之前在BOSS直聘上给HR发了招呼消息，但{interval_hours}小时没收到回复。请生成一条简短的跟进消息。

## 岗位信息
- 职位：{job.get('title', '')}
- 公司：{job.get('company', '')}

## 要求
1. 字数控制在20-40字
2. 语气自然，不卑不亢，像真人发的
3. 不要太正式，不要用"您好"开头
4. 表达还在关注这个岗位，期待有机会沟通
5. 不要直接复述"48小时没回复"这种话

请直接输出跟进消息文本，不要加任何标记。"""

    return _call_claude(prompt, config)


def monitor_and_send_resumes(config: dict) -> dict:
    """Full monitoring cycle:
    1. Detect HR replies
    2. For each reply: check if user already replied → skip or handle
    3. Generate one reply covering every intent in the latest HR messages
    4. Save it for manual confirmation, or send it when auto-reply is enabled

    Returns summary dict with counts.
    """
    throttle_config = config.get("throttle", {})
    stop_event = config.get("_workbench_stop_event")
    if not isinstance(stop_event, Event):
        stop_event = None
    if stop_event and stop_event.is_set():
        return {"skipped": 0, "pending": 0, "replied": 0, "rejected": 0, "failed": 0}

    # Time window check (09:00-16:00)
    window_checker = SendWindowChecker(throttle_config.get("send_windows", ["09:00-16:00"]))
    if not window_checker.is_active():
        console.print("[yellow]当前不在工作时间窗口内 (09:00-16:00)[/yellow]")
        return {"skipped": 0, "pending": 0, "replied": 0, "rejected": 0, "failed": 0}

    throttle = RequestThrottle(
        throttle_config.get("interval_min", 60),
        throttle_config.get("interval_max", 180),
    )

    # Step 1: Check for HR replies — MUST run first
    console.print("\n[bold cyan]═══ 第一步：处理HR回复 ═══[/bold cyan]")
    replied_conversations = check_replies(config)

    summary = {
        "skipped": 0,
        "pending": 0,
        "replied": 0,
        "rejected": 0,
        "failed": 0,
        "unmatched": 0,
        "unmatched_convs": [],
    }

    # Collect all job IDs that had HR replies this cycle (used to block follow-up)
    replied_job_ids: set = set()

    if replied_conversations:
        console.print(f"[green]检测到 {len(replied_conversations)} 个需要处理的对话[/green]")

        # Step 2: Handle each reply conversation
        for item in replied_conversations:
            if stop_event and stop_event.is_set():
                break
            job = item["job"]
            if job is None:
                # 信息不全无法建影子岗位的极端情况：只提示用户手动查看
                conv = item["conversation"] or {}
                summary["unmatched"] += 1
                summary["unmatched_convs"].append({
                    "hr_name": conv.get("hr_name", ""),
                    "company": conv.get("company", ""),
                })
                console.print(
                    f"[yellow]  ⚠ 会话「{conv.get('hr_name', '')} / {conv.get('company', '')}」"
                    f"有新回复但信息不全，无法纳入自动处理，请手动查看[/yellow]"
                )
                continue
            replied_job_ids.add(job["id"])
            prior_status = str(job.get("status") or "").strip()
            action = _handle_conversation(job, config)

            # 只有打开完整会话后确认“确有 HR 待回复消息”（reply_pending / auto_replied）
            # 才把 sent 推进到 replied。最后一条是我自己发的（skipped_user_replied）
            # 或已处理过（skipped_dismissed_reply）一律不推进——这正是“我发的消息被
            # 误判成 HR 回复”的根因，必须把状态推进放到确认之后。
            # 注意：这里不写假的 “HR回复: 预览文本” 历史。reply_pending/auto_replied
            # 各自已经写入带 fingerprint 的结构化历史，足以支撑漏斗统计与去重。
            if action in ("reply_pending", "auto_replied") and prior_status == "sent":
                db = get_db()
                try:
                    update_job_status(db, job["id"], "replied")
                finally:
                    db.close()

            if action in ("skipped_user_replied", "skipped_existing_resume", "skipped_dismissed_reply"):
                summary["skipped"] += 1
            elif action == "reply_pending":
                summary["pending"] += 1
            elif action == "auto_replied":
                summary["replied"] += 1
            elif action == "rejected":
                summary["rejected"] += 1
            else:
                summary["failed"] += 1

            if throttle.wait(stop_event):
                break

        console.print("\n[bold green]═══ 回复处理完成 ═══[/bold green]")
        console.print(f"  跳过(已手动回复): {summary['skipped']}")
        console.print(f"  待确认回复建议: {summary['pending']}")
        console.print(f"  自动回复: {summary['replied']}")
        if summary["rejected"]:
            console.print(f"  [dim]拒绝: {summary['rejected']}[/dim]")
        if summary["failed"]:
            console.print(f"  [yellow]失败: {summary['failed']}[/yellow]")
        if summary.get("unmatched"):
            console.print(f"  [yellow]未跟踪会话回复(请手动查看): {summary['unmatched']}[/yellow]")
    else:
        console.print("[dim]本轮无新HR回复[/dim]")

    # Step 3: Follow up ONLY on jobs with absolutely no HR reply
    # Pass replied_job_ids so follow-up skips any job touched this cycle
    console.print("\n[bold cyan]═══ 第二步：跟进无回复岗位 ═══[/bold cyan]")
    follow_up_count = _check_follow_ups(config, throttle, replied_job_ids=replied_job_ids)
    if follow_up_count:
        console.print(f"  二次跟进: {follow_up_count}")
        summary["follow_up"] = follow_up_count

    return summary
