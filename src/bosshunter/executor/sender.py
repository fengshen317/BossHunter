"""Sender module - Auto-send greetings with throttle control."""

import time
import json
import random
from threading import Event
from urllib.parse import parse_qs, urljoin, urlparse
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from bosshunter.browser import new_tab, close_tab, evaluate, click_at, get_page_targets, RUNTIME_NOT_READY
from bosshunter.db import (
    add_history,
    add_risk_event,
    get_db,
    get_jobs_ready_to_send,
    recover_send_errors,
    update_job_conversation_id,
    update_job_hr_active,
    update_job_status,
    update_job_status_with_retry,
)
from bosshunter.throttle import RequestThrottle, SendWindowChecker, ProgressiveBackoff, should_take_day_off

console = Console()

CHAT_BUTTON_SELECTOR = (
    'a[redirect-url*="/web/geek/chat"], '
    'a[data-url*="/friend/add"], '
    'a.btn-startchat, '
    '[ka="job_detail_chat"], '
    '[ka^="go_chat"], '
    '[ka*="gochat"], '
    '.op-btn-chat, '
    '.btn-startchat-wrap'
)

CHAT_BUTTON_SCRIPT_FOR_TESTS = """
(() => {
    const selectors = [
        'a[redirect-url*="/web/geek/chat"]',
        'a[data-url*="/friend/add"]',
        'a.btn-startchat',
        '[ka="job_detail_chat"]',
        '[ka^="go_chat"]',
        '[ka*="gochat"]',
        '.op-btn-chat',
        '.btn-startchat-wrap'
    ];
    const candidates = selectors.flatMap((selector, priority) =>
        Array.from(document.querySelectorAll(selector)).map((el) => ({el, selector, priority}))
    );
    const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const score = (item) => {
        const el = item.el;
        const text = (el.innerText || el.textContent || '').trim();
        const ka = el.getAttribute('ka') || '';
        const redirectUrl = el.getAttribute('redirect-url') || '';
        const dataUrl = el.getAttribute('data-url') || '';
        const tagName = String(el.tagName || '').toLowerCase();
        let value = 0;
        if (isVisible(el)) value += 1000;
        if (tagName === 'a') value += 200;
        if (redirectUrl.includes('/web/geek/chat')) value += 300;
        if (dataUrl.includes('/friend/add')) value += 250;
        if (el.classList && el.classList.contains('btn-startchat')) value += 120;
        if (text.includes('沟通')) value += 80;
        if (ka === 'job_detail_chat' || ka.includes('go_chat') || ka.includes('gochat')) value += 60;
        if (el.classList && el.classList.contains('btn-startchat-wrap')) value -= 100;
        return value - item.priority;
    };
    const matches = candidates
        .filter((item) => {
            const el = item.el;
            const text = (el.innerText || el.textContent || '').trim();
            const ka = el.getAttribute('ka') || '';
            const redirectUrl = el.getAttribute('redirect-url') || '';
            const dataUrl = el.getAttribute('data-url') || '';
            return (
                text.includes('沟通') ||
                redirectUrl.includes('/web/geek/chat') ||
                dataUrl.includes('/friend/add') ||
                ka === 'job_detail_chat' ||
                ka.includes('go_chat') ||
                ka.includes('gochat')
            );
        })
        .sort((a, b) => score(b) - score(a));
    const btn = matches[0] && matches[0].el;
    if (!btn) return JSON.stringify({
        success: false,
        error: 'no_chat_button',
        candidates: candidates.map((item) => {
            const el = item.el;
            const text = (el.innerText || el.textContent || '').trim();
            return {
                text,
                ka: el.getAttribute('ka'),
                className: String(el.className || ''),
                tagName: el.tagName,
                redirectUrl: el.getAttribute('redirect-url'),
                dataUrl: el.getAttribute('data-url'),
                visible: isVisible(el)
            };
        })
    });
    btn.scrollIntoView({block: 'center', inline: 'center'});
    const buttonText = (btn.innerText || btn.textContent || '').trim();
    const redirectUrl = btn.getAttribute('redirect-url') || '';
    // 发送必须从新打开的岗位页触发平台原生沟通入口，直达聊天页在部分会话中不稳定。
    btn.click();
    return JSON.stringify({
        success: true,
        action: 'clicked',
        button_text: buttonText,
        ka: btn.getAttribute('ka'),
        className: String(btn.className || ''),
        tagName: btn.tagName,
        redirectUrl,
        dataUrl: btn.getAttribute('data-url'),
        visible: isVisible(btn)
    });
})()
"""


def _parse_js_result(result) -> dict:
    """Normalise a Browser Runtime evaluate() return into a result dict.

    ``RUNTIME_NOT_READY`` (the local runtime could not be reached) is mapped to
    a distinct ``runtime_not_ready`` error so callers can retry infrastructure
    hiccups separately from genuine JS/business failures.
    """
    if result is RUNTIME_NOT_READY:
        return {"success": False, "error": "runtime_not_ready"}
    if not result:
        return {"success": False, "error": "no_response"}
    if isinstance(result, dict):
        return result
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": "parse_error"}


# Errors considered a transient infrastructure/page-timing hiccup: retryable
# within the same job (with a short backoff) and not counted toward the
# progressive-backoff circuit breaker.
_RETRYABLE_ERRORS = frozenset({
    "runtime_not_ready",
    "no_response",
    "open_page_failed",
    "chat_navigation_timeout",
    "no_chat_button",
    "no_chat_input",
    "no_send_button",
})

# Permanent failures: do not retry inside the job and skip progressive backoff
# (they reflect a real page/business state, not a flaky environment).
# send_verify_failed 是"消息可能已发出但无法验证"（选择器漂移/渲染慢会误判），
# 重发必然导致同一条招呼语重复发给同一个 HR，因此必须按永久失败处理，
# 交由工作台显式重试（重试前会先检查 conversation_id 是否已存在，见
# _send_greeting_once）。
_PERMANENT_ERRORS = frozenset({
    "job_closed",
    "job_page_unavailable",
    "send_button_disabled",
    "captcha",
    "rate_limit",
    "blocked",
    "send_verify_failed",
})


def _classify_error(error: str | None) -> str:
    """Return ``"retryable"``/``"permanent"``/``"stopped"`` for an error code."""
    if error == "stopped":
        return "stopped"
    if error in _PERMANENT_ERRORS:
        return "permanent"
    return "retryable"


def _stop_requested(stop_event) -> bool:
    return bool(stop_event and stop_event.is_set())


def _sleep_or_stop(seconds: float, stop_event) -> bool:
    if stop_event:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False


def _confirm_greet_popup(target_id: str) -> dict:
    click_confirm_js = """
    (() => {
        const popup = document.querySelector('.greet-boss-pop, .greet-pop, .dialog-wrap');
        if (popup) {
            const confirmBtn = popup.querySelector('[ka="dialog_confirm"], .btn-sure');
            if (confirmBtn) { confirmBtn.click(); return JSON.stringify({success: true, action: 'confirmed'}); }
        }
        return JSON.stringify({success: true, action: 'no_popup'});
    })()
    """
    return _parse_js_result(evaluate(target_id, click_confirm_js))


CHAT_INPUT_SELECTOR = (
    '#chat-input, .chat-input textarea, .chat-input [contenteditable="true"], '
    '.input-area textarea, [contenteditable="true"][role="textbox"], '
    'textarea.input-area'
)


def _chat_target_ids() -> set[str]:
    """获取当前页面 ID，供识别点击后新开的聊天页使用。"""
    return {
        str(target_id)
        for target in get_page_targets()
        if (target_id := target.get("targetId") or target.get("id"))
    }


def _resolve_chat_redirect_url(redirect_url: object, job_id: str) -> str | None:
    """校验并补全岗位按钮提供的精确聊天地址。

    Args:
        redirect_url: 岗位页按钮的 redirect-url 属性。
        job_id: 当前发送任务对应的岗位 ID。

    Returns:
        校验通过的完整 BOSS 聊天地址，否则返回 None。
    """
    if not isinstance(redirect_url, str) or not redirect_url.startswith("/web/geek/chat?"):
        return None

    parsed = urlparse(redirect_url)
    query = parse_qs(parsed.query)
    if query.get("jobId", [""])[0] != job_id or not query.get("id", [""])[0]:
        return None

    return urljoin("https://www.zhipin.com", redirect_url)


def _extract_conversation_id(redirect_url: object, job_id: str) -> str | None:
    """从经过岗位绑定校验的聊天地址中提取稳定会话标识。"""
    resolved_url = _resolve_chat_redirect_url(redirect_url, job_id)
    if not resolved_url:
        return None
    return parse_qs(urlparse(resolved_url).query).get("id", [None])[0]


def _wait_for_chat_page(
    target_id: str,
    stop_event,
    attempts: int = 20,
    existing_target_ids: set[str] | None = None,
) -> dict:
    """等待聊天会话可用，兼容同页跳转、内嵌聊天框和新标签页。

    BOSS 直聘点击"立即沟通"后常以 SPA 同页路由切换到 ``/web/geek/chat``，
    并不新开浏览器 target，因此新标签页检测只能作为兜底；主判定仍以"输入框
    可见"为准，并对 SPA 路由已切换但输入框尚未渲染的情况给予额外等待。
    """
    page_state_js = f"""
    (() => {{
        const input = document.querySelector({json.dumps(CHAT_INPUT_SELECTOR)});
        const visible = !!input && !!(
            input.offsetWidth ||
            input.offsetHeight ||
            input.getClientRects().length
        );
        return JSON.stringify({{
            success: visible,
            path: location.pathname,
            hasInput: !!input
        }});
    }})()
    """

    def _check_page(tid: str) -> dict:
        return _parse_js_result(evaluate(tid, page_state_js))

    # 跟踪是否已观测到 SPA 路由切换到聊天域，便于在超时信息中区分
    # "按钮点击无效" 与 "已进入聊天页但渲染未完成"。
    observed_chat_path = False

    for _ in range(attempts):
        if _sleep_or_stop(0.5, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}
        page_state = _check_page(target_id)
        if "/web/geek/chat" in str(page_state.get("path", "")):
            observed_chat_path = True
        if page_state.get("success"):
            return {"success": True, "target_id": target_id}

        if existing_target_ids is not None:
            for target in get_page_targets():
                candidate_id = str(target.get("targetId") or target.get("id") or "")
                candidate_url = str(target.get("url") or "")
                if candidate_id and candidate_id not in existing_target_ids and "/web/geek/chat" in candidate_url:
                    candidate_state = _check_page(candidate_id)
                    if candidate_state.get("success"):
                        return {"success": True, "target_id": candidate_id}

    if observed_chat_path:
        return {
            "success": False,
            "error": "chat_navigation_timeout",
            "history_detail": "已进入聊天页但输入框渲染超时，可能页面加载过慢",
            "skip_backoff": False,
        }
    return {"success": False, "error": "chat_navigation_timeout"}


def _click_chat_button(target_id: str, stop_event, attempts: int = 6) -> dict:
    click_chat_js = CHAT_BUTTON_SCRIPT_FOR_TESTS

    last_result: dict = {"success": False, "error": "no_chat_button"}
    for attempt in range(max(1, attempts)):
        if _stop_requested(stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}
        last_result = _parse_js_result(evaluate(target_id, click_chat_js))
        if last_result.get("success"):
            return last_result
        if last_result.get("error") != "no_chat_button":
            return last_result
        if attempt < attempts - 1 and _sleep_or_stop(1, stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}

    return last_result


def _describe_no_chat_button(result: dict) -> str:
    """Build a human-readable history detail from the chat-button probe result.

    The chat-button JS already collects every candidate element's text/ka/class.
    Surfacing that here makes selector drift diagnosable without re-running.
    """
    candidates = result.get("candidates") if isinstance(result, dict) else None
    if not candidates:
        return "无法找到沟通按钮（页面未渲染任何候选元素，可能加载未完成或被风控）"
    summaries = []
    for item in candidates[:8]:
        parts = [f"text={item.get('text', '')!r}"]
        if item.get("ka"):
            parts.append(f"ka={item['ka']}")
        if item.get("className"):
            parts.append(f"class={str(item['className'])[:60]}")
        if item.get("redirectUrl"):
            parts.append(f"redirect={item['redirectUrl']}")
        parts.append(f"visible={item.get('visible')}")
        summaries.append(" | ".join(parts))
    detail = "无法找到沟通按钮，候选元素: " + " ;; ".join(summaries)
    return detail[:300]


# JS: 在岗位详情页点击"立即沟通"并发送招呼语（占位保留，实际逻辑由
# CHAT_BUTTON_SCRIPT_FOR_TESTS + send_msg_js 分步执行）。


def _verify_message_sent(
    target_id: str,
    stop_event,
    attempts: int = 12,
) -> dict:
    """Verify a greeting actually reached the chat by polling the message list.

    ``sendBtn.click()`` returns immediately and cannot prove the message was
    delivered — clicking a disabled/locked button looks identical to success.
    BOSS renders sent messages as children inside ``.message-list`` (or a
    similar container), so we poll until a new message bubble appears.

    Returns ``{success: True}`` once the list grows, or
    ``{success: False, error: 'send_verify_failed'}`` after timing out.
    """
    verify_js = """
    (() => {
        const list = document.querySelector(
            '.message-list, .chat-record, .message-content, [class*="message-list"]'
        );
        if (!list) return JSON.stringify({found: false, count: -1});
        return JSON.stringify({found: true, count: list.children.length});
    })()
    """
    for _ in range(attempts):
        if _sleep_or_stop(0.5, stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}
        state = _parse_js_result(evaluate(target_id, verify_js))
        if state.get("found") and int(state.get("count", -1)) > 0:
            return {"success": True, "message_count": state["count"]}
    return {
        "success": False,
        "error": "send_verify_failed",
        "history_detail": "点击发送后消息未出现在聊天列表，可能被风控拦截或发送请求失败",
    }


def _send_greeting_once(job: dict, greeting: str, throttle_config: dict) -> tuple[dict, str | None]:
    stop_event = throttle_config.get("_workbench_stop_event")

    # 幂等防重：该岗位若已建立过稳定会话（conversation_id），说明此前已成功
    # 发送过招呼语（发送成功的记账是写 conversation_id 的），无论当前状态如何
    # 都不再重发，避免同一 HR 收到重复招呼。此检查同时兜底"发送成功但记账未
    # 完成即崩溃"的窗口：下次重试会先在这里被拦下。
    existing_conversation = str(job.get("conversation_id") or "").strip()
    if existing_conversation:
        return {
            "success": False,
            "error": "already_sent",
            "history_detail": f"该岗位已有会话记录（{existing_conversation}），已跳过重复发送",
            "skip_backoff": True,
        }, None

    target_id = new_tab(job["url"])
    if not target_id:
        return {"success": False, "error": "open_page_failed", "history_detail": "无法打开页面", "skip_backoff": True}, None

    if _stop_requested(stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None

    browse_min = throttle_config.get("browse_duration_min", 15)
    browse_max = throttle_config.get("browse_duration_max", 30)
    if throttle_config.get("browse_before_greet", True):
        browse_time = random.uniform(browse_min, browse_max)
        if _sleep_or_stop(browse_time, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None

    page_check_js = """
    (() => {
        const text = document.body ? document.body.innerText : '';
        const title = document.title || '';
        const closedSignals = [
            '该职位已关闭', '职位已关闭', '岗位已关闭',
            '该职位已下线', '职位已下线', '招聘已结束'
        ];
        const closedSignal = closedSignals.find((signal) => text.includes(signal));
        if (closedSignal) {
            return JSON.stringify({
                success: false,
                error: 'job_closed',
                history_detail: `发送前检测到岗位已关闭（${closedSignal}）`,
                skip_backoff: true
            });
        }
        if (
            title.includes('访问的页面不存在') ||
            text.includes('您访问的页面不存在') ||
            text.includes('Oops!')
        ) {
            return JSON.stringify({
                success: false,
                error: 'job_page_unavailable',
                history_detail: '岗位页面不存在或已下架',
                skip_backoff: true
            });
        }
        const activitySelectors = [
            '.boss-active-time',
            '[class*="boss-active"]',
            '[class*="active-time"]',
            '[class*="recruiter-active"]'
        ];
        const activityTexts = activitySelectors.flatMap((selector) =>
            Array.from(document.querySelectorAll(selector)).map((el) =>
                (el.innerText || el.textContent || '').trim()
            )
        ).filter(Boolean);
        const recruiterText = Array.from(document.querySelectorAll(
            '.job-boss-info, .boss-info, [class*="boss-info"], [class*="recruiter"]'
        )).map((el) => (el.innerText || el.textContent || '').trim()).join(' ');
        const activityMatch = recruiterText.match(/(在线|刚刚活跃|\\d+分钟前|\\d+小时内活跃|今日活跃|本周活跃)/);
        const hrActive = activityTexts[0] || (activityMatch ? activityMatch[1] : '');
        return JSON.stringify({
            success: true,
            hr_active: hrActive,
            hr_online: hrActive === '在线' || hrActive === '刚刚活跃'
        });
    })()
    """
    page_check = _parse_js_result(evaluate(target_id, page_check_js))
    if not page_check.get("success"):
        close_tab(target_id)
        return page_check, None
    if throttle_config.get("only_send_when_hr_online", False) and not page_check.get("hr_online"):
        hr_active = str(page_check.get("hr_active") or "未识别到活跃状态").strip()
        close_tab(target_id)
        return {
            "success": False,
            "error": "hr_offline",
            "history_detail": f"发送前 HR 状态为：{hr_active}，已按在线发送规则跳过",
            "hr_active": hr_active,
            "preserve_status": True,
            "skip_backoff": True,
        }, None

    existing_target_ids = _chat_target_ids()
    existing_target_ids.add(target_id)
    chat_button_attempts = int(throttle_config.get("_chat_button_attempts", 6))
    result1a = _click_chat_button(target_id, stop_event, chat_button_attempts)
    if not result1a.get("success"):
        close_tab(target_id)
        # Preserve the candidate diagnostics collected by the chat-button
        # script so selector drift can be triaged from the history log instead
        # of being hidden behind a generic "无法找到沟通按钮" message.
        history_detail = _describe_no_chat_button(result1a)
        return {
            "success": False,
            "error": "no_chat_button",
            "history_detail": history_detail,
            "skip_backoff": True,
        }, None

    if _sleep_or_stop(4, stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None

    _confirm_greet_popup(target_id)

    navigation_attempts = int(throttle_config.get("_chat_navigation_attempts", 20))
    chat_ready = _wait_for_chat_page(target_id, stop_event, navigation_attempts, existing_target_ids)
    if chat_ready.get("error") == "stopped":
        return chat_ready, None
    if not chat_ready.get("success"):
        console.print("[yellow]    ! 沟通按钮未跳转聊天页，尝试真实点击兜底[/yellow]")
        if click_at(target_id, CHAT_BUTTON_SELECTOR):
            if _sleep_or_stop(1, stop_event):
                close_tab(target_id)
                return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
            _confirm_greet_popup(target_id)
            chat_ready = _wait_for_chat_page(target_id, stop_event, navigation_attempts, existing_target_ids)
            if chat_ready.get("error") == "stopped":
                return chat_ready, None

    if not chat_ready.get("success"):
        return {
            "success": False,
            "error": "no_chat_input",
            "history_detail": "发送失败: 未进入具体聊天会话，可能是BOSS继续沟通跳转失败",
            "skip_backoff": True,
        }, target_id

    chat_target_id = str(chat_ready.get("target_id") or target_id)
    if chat_target_id != target_id:
        close_tab(target_id)

    greeting_escaped = json.dumps(greeting)
    input_selector_escaped = json.dumps(CHAT_INPUT_SELECTOR)
    # 拆分为两步：① 输入招呼语并点击发送 ② 轮询消息列表验证消息真的发出。
    # BOSS 直聘的发送是异步的，仅 click() 返回不能证明成功——实测点击禁用
    # 按钮也会"成功"返回。必须以"消息列表里出现新消息气泡"为准。
    # 注意：BOSS 用 class 名里的 disable/disabled 表示禁用（不是 disabled
    # 属性也不是 aria-disabled="true"），所以禁用态检测必须查 class。
    send_msg_js = f"""
    (async () => {{
        const input = document.querySelector({input_selector_escaped});
        if (!input) return JSON.stringify({{success: false, error: 'no_chat_input'}});

        input.focus();
        if (input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement) {{
            const prototype = input instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, {greeting_escaped});
        }} else {{
            input.innerText = {greeting_escaped};
            input.textContent = {greeting_escaped};
        }}
        input.dispatchEvent(new InputEvent('input', {{
            bubbles: true,
            inputType: 'insertText',
            data: {greeting_escaped}
        }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        input.dispatchEvent(new KeyboardEvent('keydown', {{ bubbles: true, key: ' ' }}));
        input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: ' ' }}));
        // 等待 Vue/React 根据输入事件刷新发送按钮的 disabled class。
        await new Promise(resolve => setTimeout(resolve, 150));

        const sendSelectors = [
            '[ka="chat_send"]',
            '[ka^="chat_send"]',
            '.send-message',
            '.btn-send',
            '.send-btn',
            '.chat-send',
            '.chat-send-btn'
        ];
        const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const isDisabled = (el) => {{
            if (el.disabled) return true;
            if (el.getAttribute('aria-disabled') === 'true') return true;
            // BOSS 用 class 名里的 disable/disabled 标记禁用态（实测）。
            const cls = String(el.className || '').toLowerCase();
            // 排除 "disabled-wrap" 之类的容器，只匹配独立的 disable/disabled 词。
            return /(^|[\\s_-])disable(d)?([\\s_-]|$)/.test(cls);
        }};
        const sendCandidates = sendSelectors.flatMap((sel) =>
            Array.from(document.querySelectorAll(sel)).map((el) => ({{el, sel}}))
        );
        const scoreBtn = (item) => {{
            const el = item.el;
            const text = (el.innerText || el.textContent || '').trim();
            let v = 0;
            if (isVisible(el)) v += 1000;
            if (item.sel === '[ka="chat_send"]') v += 200;
            if (item.sel === '.send-message') v += 120;
            if (text === '发送') v += 80;
            if (text.includes('发送')) v += 30;
            if (el.classList && el.classList.contains('btn-send')) v += 40;
            return v;
        }};
        sendCandidates.sort((a, b) => scoreBtn(b) - scoreBtn(a));
        const activeBtn = sendCandidates.find((item) => scoreBtn(item) > 0 && !isDisabled(item.el));
        if (!activeBtn) {{
            // 区分"没找到按钮"和"按钮存在但禁用"，便于诊断输入是否生效。
            const anyBtn = sendCandidates.find((item) => scoreBtn(item) > 0);
            return JSON.stringify(anyBtn
                ? {{success: false, error: 'send_button_disabled', buttonClass: String(anyBtn.el.className||'')}}
                : {{success: false, error: 'no_send_button'}});
        }}
        // 记录点击前的消息数量，用于后续验证。
        const msgList = document.querySelector('.message-list, .chat-record, .message-content, [class*="message-list"]');
        const beforeCount = msgList ? msgList.children.length : -1;
        activeBtn.el.click();
        return JSON.stringify({{
            success: true,
            method: 'button',
            clicked: true,
            messageListBefore: beforeCount,
            messageListSelector: msgList ? null : null
        }});
    }})()
    """
    result_data = _parse_js_result(evaluate(chat_target_id, send_msg_js))

    # 点击成功后必须验证消息真的进入消息列表，否则会"假成功"。
    if result_data.get("success") and result_data.get("clicked"):
        verified = _verify_message_sent(chat_target_id, stop_event)
        if not verified.get("success"):
            # 把验证失败的诊断并入 result，覆盖"假成功"。
            result_data = {
                "success": False,
                "error": verified.get("error", "send_verify_failed"),
                "history_detail": verified.get(
                    "history_detail", "点击发送后消息未出现在聊天列表，可能被风控或发送失败"
                ),
                "skip_backoff": False,
            }

    if result_data.get("success"):
        hr_active = str(page_check.get("hr_active") or "").strip()
        if hr_active:
            result_data["hr_active"] = hr_active
            result_data["hr_online"] = bool(page_check.get("hr_online"))
        conversation_id = _extract_conversation_id(result1a.get("redirectUrl"), str(job["id"]))
        if conversation_id:
            result_data["conversation_id"] = conversation_id
        close_tab(chat_target_id)
        return result_data, None
    return result_data, chat_target_id


def _send_greeting_with_retry(
    job: dict,
    greeting: str,
    throttle_config: dict,
) -> tuple[dict, str | None]:
    """Wrap ``_send_greeting_once`` with in-job retries for transient failures.

    Transient errors (runtime hiccup, slow page load, selector drift) are
    retried up to ``send_max_retries`` times (default 2) with a short random
    backoff between attempts. Permanent failures (page taken down, send button
    disabled, risk control) return immediately. The user stop event is honoured
    between attempts.

    Returns the final ``(result, failed_target_id)`` from the last attempt.
    ``failed_target_id`` is always ``None`` here because each retry opens (and
    closes on failure) its own tab.
    """
    max_retries = max(0, int(throttle_config.get("send_max_retries", 2)))
    backoff_min = float(throttle_config.get("send_retry_backoff_min", 5))
    backoff_max = float(throttle_config.get("send_retry_backoff_max", 10))
    stop_event = throttle_config.get("_workbench_stop_event")

    last_result, last_target_id = _send_greeting_once(job, greeting, throttle_config)

    for attempt in range(max_retries):
        error = last_result.get("error")
        if last_result.get("success") or _classify_error(error) != "retryable":
            break
        # Transient failure: brief backoff then reopen the job page fresh.
        backoff = random.uniform(backoff_min, backoff_max)
        if _sleep_or_stop(backoff, stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
        console.print(
            f"[yellow]    ! 临时性失败({error})，{attempt + 1}/{max_retries} 次重试...[/yellow]"
        )
        last_result, last_target_id = _send_greeting_once(job, greeting, throttle_config)

    return last_result, last_target_id


def send_greetings(config: dict, force: bool = False) -> int:
    """Send generated greetings. Returns count of successfully sent."""
    db = get_db()
    throttle_config = config.get("throttle", {})
    stop_event = config.get("_workbench_stop_event")
    if isinstance(stop_event, Event):
        throttle_config = dict(throttle_config)
        throttle_config["_workbench_stop_event"] = stop_event

    # Anti-ban: random day off (可通过 --force 跳过)
    day_off_prob = throttle_config.get("day_off_probability", 0.05)
    if not force and should_take_day_off(day_off_prob):
        console.print("[yellow]🎲 今日随机休息（防检测），跳过发送[/yellow]")
        add_risk_event(db, "day_off", "随机休息日")
        db.close()
        return 0

    # Anti-ban: send window check (可通过 --force 跳过)
    send_windows = throttle_config.get("send_windows", [])
    window_checker = SendWindowChecker(send_windows)
    if not force and not window_checker.is_active():
        info = window_checker.next_window_info()
        console.print("[yellow]⏰ 当前不在发送时间窗口内，暂不发送[/yellow]")
        console.print(f"[dim]  {info}[/dim]")
        add_risk_event(db, "outside_window", info)
        db.close()
        return 0

    # 退避窗口到期的临时性失败自动重回 ready 队列，避免一次抖动就让岗位
    # 永久消失（仍受 send_error_max_retries 上限约束）。可通过配置关闭。
    if throttle_config.get("auto_recover_send_errors", True):
        recover_after = int(throttle_config.get("send_error_recover_seconds", 600))
        max_retries = int(throttle_config.get("send_error_max_retries", 3))
        recovered = recover_send_errors(db, recover_after, max_retries)
        if recovered:
            console.print(f"[green]♻ 已自动恢复 {recovered} 个退避到期的失败岗位[/green]")

    # 失败记录只能由工作台显式选中后重试，避免常规批量发送自动反复尝试。
    jobs = get_jobs_ready_to_send(
        db,
        include_send_errors=bool(config.get("_workbench_include_send_errors", False)),
    )
    _workbench_job_ids = {str(job_id) for job_id in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [job for job in jobs if str(job["id"]) in _workbench_job_ids]

    if not jobs:
        console.print("[yellow]没有已生成招呼语的待发送岗位，请先运行 bosshunter greet[/yellow]")
        db.close()
        return 0

    # Check daily limit
    daily_limit = throttle_config.get("daily_limit", 30)
    interval_min = throttle_config.get("interval_min", 60)
    interval_max = throttle_config.get("interval_max", 180)

    # Count today's sent
    today_sent = db.execute(
        "SELECT COUNT(*) as cnt FROM history WHERE action='sent' AND date(created_at)=date('now')"
    ).fetchone()
    already_sent = today_sent["cnt"] if today_sent else 0

    remaining_quota = daily_limit - already_sent
    if remaining_quota <= 0:
        console.print(f"[yellow]今日已达发送上限 ({daily_limit})[/yellow]")
        db.close()
        return 0

    jobs_to_send = jobs[:remaining_quota]
    throttle = RequestThrottle(delay_min=interval_min, delay_max=interval_max)
    backoff = ProgressiveBackoff()
    sent_count = 0

    console.print(f"[bold]准备发送 {len(jobs_to_send)} 条招呼语[/bold] (今日已发 {already_sent}/{daily_limit})")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console
    ) as progress:
        task = progress.add_task("发送中", total=len(jobs_to_send))

        for job in jobs_to_send:
            if _stop_requested(stop_event):
                console.print("[yellow]已请求停止，结束发送[/yellow]")
                break

            greeting = job.get("greeting", "")
            if not greeting:
                update_job_status(db, job["id"], "error")
                progress.update(task, advance=1)
                continue

            # Wait between sends (except first)
            if sent_count > 0:
                progress.update(task, description="等待间隔...")
                if throttle.wait(stop_event):
                    console.print("[yellow]已请求停止，结束发送[/yellow]")
                    break

            progress.update(task, description=f"发送: {job['company'][:10]} - {job['title'][:15]}")

            # 岗位内统一重试：临时性失败（runtime 抖动、页面慢、选择器漂移）
            # 由 _send_greeting_with_retry 自带短退避重试消化；此处拿到的
            # 是最终结果。外层不再做 no_chat_input 的二次重试。
            result_data, failed_target_id = _send_greeting_with_retry(job, greeting, throttle_config)
            if result_data.get("error") == "stopped":
                break
            if failed_target_id:
                close_tab(failed_target_id)

            if not result_data.get("success"):
                console.print(f"[yellow]    ! 发送失败: {result_data.get('error', 'unknown')}[/yellow]")

            if result_data.get("success"):
                throttle.mark()
                # 记账顺序：先写 history(action='sent')，再更新 status 与
                # conversation_id。每日限额按 history.action='sent' 计数
                # （见 send_greetings 开头），若在更新 status 后、写历史前崩溃，
                # 下次运行会因历史缺失而重复计入一条未真实发送的配额，
                # 导致"今日已发"虚高、漏发真实岗位。先写历史可收窄该窗口；
                # conversation_id 则在发送后立即落库，配合发送前的
                # conversation_id 检查形成"已发送即不再重发"的幂等闭环。
                hr_active = str(result_data.get("hr_active") or "").strip()
                history_detail = greeting[:50]
                if hr_active:
                    history_detail = f"{history_detail}（发送前 HR：{hr_active}）"
                add_history(db, job["id"], "sent", history_detail)
                conversation_id = result_data.get("conversation_id")
                if conversation_id:
                    update_job_conversation_id(db, job["id"], str(conversation_id))
                update_job_status(db, job["id"], "sent")
                if hr_active:
                    update_job_hr_active(db, job["id"], hr_active)
                sent_count += 1
                backoff.record_success()
            else:
                error = result_data.get("error", "unknown")
                if error in ("job_closed", "job_page_unavailable"):
                    detail = result_data.get("history_detail", "发送前检测到岗位已关闭或下架")
                    update_job_status_with_retry(db, job["id"], "closed")
                    add_history(db, job["id"], "closed", detail)
                    console.print(f"[yellow]    ! 岗位已关闭，已停止发送和后续跟踪: {job['company']} - {job['title']}[/yellow]")
                    progress.update(task, advance=1)
                    continue
                if result_data.get("preserve_status"):
                    hr_active = str(result_data.get("hr_active") or "").strip()
                    if hr_active:
                        update_job_hr_active(db, job["id"], hr_active)
                    add_history(db, job["id"], "skipped", result_data.get("history_detail", "已按发送规则跳过"))
                    progress.update(task, advance=1)
                    continue
                if error == "already_sent":
                    # 幂等守卫拦截：该岗位已有会话记录（此前已成功发送），
                    # 不再重发，也不会计入失败状态或退避。若状态仍是 approved
                    # （说明上次成功记账未完成），这里把它收敛为 sent。
                    update_job_status(db, job["id"], "sent")
                    add_history(db, job["id"], "sent", result_data.get("history_detail", "检测到已有会话记录，标记为已发送"))
                    progress.update(task, advance=1)
                    continue
                update_job_status_with_retry(db, job["id"], "error")
                add_history(db, job["id"], "error", result_data.get("history_detail", f"发送失败: {error}"))

                # runtime 抖动属于基础设施问题，不污染渐进退避/风控计数。
                # 这些错误已由岗位内重试消化，仍失败说明 runtime 不可用，
                # 直接跳过退避计数，但记录一条 risk_event 便于观测。
                if error in ("runtime_not_ready", "no_response"):
                    add_risk_event(db, "runtime_unavailable", f"运行时不可用: {error}")
                    progress.update(task, advance=1)
                    continue

                if result_data.get("skip_backoff"):
                    progress.update(task, advance=1)
                    continue

                throttle.mark()

                # Progressive backoff on errors
                pause_duration = backoff.record_error()
                add_risk_event(db, "send_error", f"{error} (连续{backoff._consecutive_errors}次)")

                # If we encounter rate limiting, stop immediately
                if error in ["captcha", "rate_limit", "blocked"]:
                    console.print(f"\n[red]⚠ 检测到风控信号: {error}，安全暂停[/red]")
                    add_risk_event(db, error, f"触发风控: {error}")
                    break

                # If too many consecutive errors, pause
                if backoff.should_pause_long:
                    console.print(f"\n[red]⚠ 连续错误过多，暂停 {int(pause_duration/60)} 分钟[/red]")
                    add_risk_event(db, "backoff_pause", f"暂停{int(pause_duration)}秒")
                    break
                elif pause_duration > 0:
                    console.print(f"\n[yellow]  错误退避: 额外等待 {int(pause_duration)}秒[/yellow]")
                    if _sleep_or_stop(pause_duration, stop_event):
                        break

            progress.update(task, advance=1)

    console.print(f"\n[green]✓ 成功发送 {sent_count} 条[/green]")
    db.close()
    return sent_count
