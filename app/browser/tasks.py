"""
任务执行：全部走浏览器（同源），绕开 requests 通道的 301 风控。

- 签到/日常签到：打开音乐人后台，DOM 点击签到按钮；用接口响应监听确认结果。
- 发布动态：#pubEvent → 配乐 → 分享 → 抓 event.id → 删除。
- VIP 领取：打开权益页 → 点续期/领取 → 监听 vip/info 拿 furtherVipGetTime。

必须在 browser worker 线程内调用。签到按钮的确切选择器可能需用有头浏览器现场核对，
这里提供候选选择器 + 接口监听确认的健壮实现。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from playwright.sync_api import Page

from app.browser import selectors as S
from app.browser.helpers import cookies_to_str, fetch_session_user, first_with_selector, scopes
from app.browser.manager import run_with_context
from app.event_bus import bus
from app.logging_conf import logger


def _emit(account_id: Optional[int], line: str, level: str = "info") -> None:
    if level == "error":
        logger.error(line)
    elif level == "warn":
        logger.warning(line)
    else:
        logger.info(line)
    bus.log(account_id, line, level=level)


def _validate_session(page: Page, account_id: Optional[int]) -> bool:
    """任务执行前向服务端确认登录态，避免仅凭本地 cookie 静默运行。"""
    _emit(account_id, "执行任务前校验服务端登录态...")
    uid, _nickname, error = fetch_session_user(page, S.MUSICIAN_HOME_URL)
    if uid:
        _emit(account_id, f"服务端登录态有效（uid={uid}）")
        return True
    _emit(account_id, f"Cookie 已失效或账号未登录：{error or '账号接口未返回 uid'}；请点击「登录」重新认证", "error")
    bus.status(account_id, "login_fail", "Cookie 已失效，请重新登录")
    return False


# ---------- 循环任务列表 ----------
def _capture_cycle_missions(page: Page, account_id: Optional[int], timeout_ms: int = 30000) -> list[dict]:
    """打开音乐人后台，监听 cycle/list 接口拿任务列表。"""
    def _is_target(resp) -> bool:
        try:
            return S.CYCLE_LIST_API in resp.url and resp.request.method == "POST"
        except Exception:
            return False

    _emit(account_id, "打开音乐人后台，等待任务列表接口...")
    try:
        with page.expect_response(_is_target, timeout=timeout_ms) as resp_info:
            page.goto(S.MUSICIAN_HOME_URL, wait_until="domcontentloaded")
        data = resp_info.value.json()
    except Exception as e:  # noqa: BLE001
        _emit(account_id, f"未捕获到 cycle/list 接口：{e}", "warn")
        return []

    if not isinstance(data, dict) or data.get("code") != 200:
        _emit(account_id, f"cycle/list 返回异常：{str(data)[:120]}", "warn")
        return []
    missions = (data.get("data") or {}).get("list") or []
    _emit(account_id, f"获取到 {len(missions)} 个循环任务")
    return missions


# ---------- 签到（页面级）----------
def _musician_signed_result(page: Page, account_id: Optional[int]) -> Optional[dict]:
    """识别音乐人中心已签到按钮状态。"""
    signed_selectors = [
        ".sign-in-btn.signed",
        "div[class~='sign-in-btn'][class~='signed']",
        "text=已签到, 明日继续",
        "text=已签到，明日继续",
    ]
    for scope in scopes(page):
        for sel in signed_selectors:
            try:
                loc = scope.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    text = (loc.first.inner_text(timeout=1000) or "已签到").strip()
                    _emit(account_id, f"音乐人签到：{text}")
                    return {"ok": True, "message": "今日已签到"}
            except Exception:
                continue
    return None


def _checkin_on_page(page: Page, account_id: Optional[int]) -> dict:
    """在已有页面上执行音乐人签到 + 日常签到。"""
    missions = _capture_cycle_missions(page, account_id)
    musician_result = _musician_signed_result(page, account_id)
    if musician_result is None:
        musician_result = {"ok": False, "message": "未发现可执行任务（可能已签到）"}
        for m in missions:
            desc = m.get("description") or ""
            if "签到" not in desc:
                continue
            _emit(account_id, f"发现签到任务：{desc}")
            current = _click_or_invoke_checkin(page, account_id, m)
            if current["ok"]:
                musician_result = current
                break
            musician_result = current
    if not musician_result["ok"]:
        _emit(account_id, musician_result["message"])
    daily_result = _do_daily_task(page, account_id)
    return {
        # 保留顶层字段兼容旧调用方；详细展示必须读取下面两个独立结果。
        "ok": bool(musician_result["ok"] and daily_result["ok"]),
        "message": "checkin done",
        "musician_checkin": musician_result,
        "daily_checkin": daily_result,
    }


def do_checkin(profile_dir: str, account_id: Optional[int] = None) -> dict:
    """独立入口：自己开浏览器执行签到（供手动单独触发）。"""
    bus.status(account_id, "running", "执行签到任务")
    with run_with_context(profile_dir, account_id=account_id, label="签到") as (context, page):
        if not _validate_session(page, account_id):
            return {"ok": False, "auth_valid": False, "cookie_str": "", "message": "cookie expired"}
        res = _checkin_on_page(page, account_id)
        res["auth_valid"] = True
        res["cookie_str"] = cookies_to_str(context.cookies("https://music.163.com"))
        bus.status(account_id, "done", "签到任务完成")
        return res


def _read_playback_state(page: Page) -> dict[str, float | str]:
    """从主页面和网易云 iframe 读取当前播放器状态。"""
    script = """
        () => {
            const p = window.player;
            try {
                if (p && typeof p.getDuration === 'function' && p.getDuration() > 0) {
                    let cur = Number(p.getPosition ? p.getPosition() : 0);
                    let dur = Number(p.getDuration());
                    if (dur < 10000) { cur *= 1000; dur *= 1000; }
                    return {cur, dur, state: String(p.getState ? p.getState() : '')};
                }
            } catch (e) {}
            const audio = document.querySelector('audio');
            if (!audio) {
                return {cur: 0, dur: 0, state: ''};
            }
            return {
                cur: Number.isFinite(audio.currentTime)
                    ? Math.max(0, audio.currentTime * 1000) : 0,
                dur: Number.isFinite(audio.duration) && audio.duration > 0
                    ? Math.max(0, audio.duration * 1000) : 0,
                state: audio.paused ? 'paused' : 'playing',
            };
        }
    """
    for frame in page.frames:
        try:
            state = frame.evaluate(script)
            if state and (
                float(state.get("dur", 0)) > 0
                or state.get("state") in {"paused", "playing", "play", "stop"}
            ):
                return state
        except Exception:
            continue

    # 网易云网页播放器有时不暴露 window.player，也没有可直接读取的 audio
    # 元素，但底部播放器的时间文本和进度条仍会正常更新。
    dom_script = """
        () => {
            const timeNodes = Array.from(document.querySelectorAll(
                '.m-playbar .time, .g-btmbar .time, .m-pbar .time'
            ));
            const toMs = (value) => {
                const parts = String(value || '').trim().split(':').map(Number);
                if (parts.length !== 2 || parts.some(Number.isNaN)) return 0;
                return (parts[0] * 60 + parts[1]) * 1000;
            };
            for (const node of timeNodes) {
                const text = (node.innerText || node.textContent || '').trim();
                const matches = text.match(/(\\d{1,3}:\\d{2})\\s*\\/\\s*(\\d{1,3}:\\d{2})/);
                if (!matches) continue;
                const cur = toMs(matches[1]);
                const dur = toMs(matches[2]);
                if (dur <= 0) continue;
                const button = document.querySelector('.m-playbar .ply, .g-btmbar .ply');
                const buttonText = button
                    ? `${button.className} ${button.title || ''} ${button.getAttribute('aria-label') || ''}`
                    : '';
                const state = /pause|暂停/i.test(buttonText) ? 'playing' : '';
                return {cur, dur, state};
            }
            return {cur: 0, dur: 0, state: ''};
        }
    """
    for frame in page.frames:
        try:
            state = frame.evaluate(dom_script)
            if state and float(state.get("dur", 0)) > 0:
                return state
        except Exception:
            continue
    return {"cur": 0, "dur": 0, "state": ""}


def _click_play_controls(page: Page, *, force: bool = False) -> bool:
    """网易云播放按钮是 toggle；状态未知时不要反复点击。"""
    if not force:
        state = _read_playback_state(page).get("state")
        if state not in {"paused", "stop"}:
            return False

    for selector in [".m-playbar .ply", ".g-btmbar .ply"]:
        try:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=1500)
                return True
        except Exception:
            continue
    for frame in page.frames:
        for selector in [".u-btni-play", '[data-res-action="play"]', "#playall", ".u-btn2-2"]:
            try:
                loc = frame.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500)
                    return True
            except Exception:
                continue
    return False


def do_listen_music(
    profile_dir: str,
    netease_item_id: str,
    account_id: Optional[int] = None,
    timeout_seconds: int = 1200,
) -> dict:
    """打开歌曲并完整播放，供互助服务的 /next 与 /play/finish 使用。"""
    item_id = str(netease_item_id or "").strip()
    item_kind, target_id = "song", item_id
    if item_id.startswith("album:"):
        item_kind, target_id = "album", item_id[6:].strip()
    if not target_id.isdigit():
        return {"ok": False, "message": f"无效的歌曲/专辑 ID：{item_id}"}

    bus.status(account_id, "running", f"播放听歌任务：{item_id}")
    with run_with_context(profile_dir, account_id=account_id, label="听歌") as (context, page):
        if not _validate_session(page, account_id):
            return {"ok": False, "auth_valid": False, "message": "cookie expired"}
        page.goto(
            f"https://music.163.com/#/{item_kind}?id={target_id}",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(3000)
        # 首次进入歌曲页只尝试一次。播放按钮是 toggle，不能在状态未知时
        # 周期性点击，否则会出现播放一下又暂停一下。
        _click_play_controls(page, force=True)
        deadline = time.time() + timeout_seconds
        last_progress = 0.0
        last_log = 0.0
        while time.time() < deadline:
            state = _read_playback_state(page)
            cur = float(state.get("cur", 0))
            dur = float(state.get("dur", 0))
            if dur > 0 and cur >= dur - 2500:
                _emit(account_id, f"听歌完成：{item_id}（{dur / 1000:.0f} 秒）")
                bus.status(account_id, "done", "听歌任务完成")
                return {
                    "ok": True,
                    "item_id": item_id,
                    "played_ms": int(cur),
                    "duration_ms": int(dur),
                    "cookie_str": cookies_to_str(context.cookies("https://music.163.com")),
                }
            if state.get("state") in {"paused", "stop"}:
                _click_play_controls(page)
            if cur > last_progress:
                last_progress = cur
            if time.time() - last_log >= 15:
                _emit(account_id, f"听歌播放中：{item_id}，进度 {cur / 1000:.0f}/{dur / 1000:.0f} 秒")
                last_log = time.time()
            time.sleep(2)
        _emit(account_id, f"听歌超时：{item_id}", "warn")
        return {"ok": False, "message": "播放超时"}


def _listen_local_item_on_page(
    page: Page,
    item_id: str,
    account_id: Optional[int],
    play_percent: int,
    timeout_seconds: int,
) -> dict:
    item_kind, target_id = "song", str(item_id).strip()
    if target_id.startswith("album:"):
        item_kind, target_id = "album", target_id[6:].strip()
    if not target_id.isdigit():
        return {"ok": False, "item_id": item_id, "message": "无效的歌曲/专辑 ID"}
    page.goto(f"https://music.163.com/#/{item_kind}?id={target_id}", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    if not _click_play_controls(page, force=True):
        return {"ok": False, "item_id": item_id, "message": "未找到可用的播放按钮"}
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    while time.time() < deadline:
        from app.browser import registry

        if page.is_closed() or not registry.is_current_task(account_id):
            return {"ok": False, "stopped": True, "item_id": item_id, "message": "任务已停止"}
        state = _read_playback_state(page)
        cur, dur = float(state.get("cur", 0)), float(state.get("dur", 0))
        required = min(dur, dur * max(34, min(100, play_percent)) / 100 + 5000) if dur > 0 else 0
        if required > 0 and cur >= required:
            _emit(account_id, f"本地互助计次完成：{item_id}（已播放 {cur / 1000:.0f}/{dur / 1000:.0f} 秒）")
            return {"ok": True, "item_id": item_id, "played_ms": int(cur), "duration_ms": int(dur)}
        if state.get("state") in {"paused", "stop"}:
            _click_play_controls(page)
        if time.time() - last_log >= 15:
            _emit(account_id, f"本地互助播放中：{item_id}，进度 {cur / 1000:.0f}/{dur / 1000:.0f} 秒")
            last_log = time.time()
        time.sleep(2)
    return {"ok": False, "item_id": item_id, "message": "播放超时"}


def do_local_listen_music_batch(
    profile_dir: str,
    netease_item_ids: list[str],
    account_id: Optional[int] = None,
    play_percent: int = 34,
    timeout_seconds: int = 1200,
) -> dict:
    """在同一个浏览器会话中依次播放本地互助目标。"""
    item_ids = [str(item).strip() for item in netease_item_ids if str(item).strip()]
    if not item_ids:
        return {"ok": False, "results": [], "message": "没有可播放的歌曲"}
    bus.status(account_id, "running", f"本地互助批量播放：{len(item_ids)} 首")
    results: list[dict] = []
    with run_with_context(profile_dir, account_id=account_id, label="本地互助听歌") as (context, page):
        if not _validate_session(page, account_id):
            return {"ok": False, "auth_valid": False, "results": [], "message": "cookie expired"}
        current_page = page
        for index, item_id in enumerate(item_ids):
            if index:
                next_page = context.new_page()
                try:
                    current_page.close()
                except Exception:
                    pass
                current_page = next_page
            _emit(account_id, f"本地互助第 {index + 1}/{len(item_ids)} 首：{item_id}")
            result = _listen_local_item_on_page(current_page, item_id, account_id, play_percent, timeout_seconds)
            results.append(result)
            if result.get("stopped"):
                break
    completed = sum(1 for result in results if result.get("ok"))
    bus.status(account_id, "done", f"本地互助完成：{completed}/{len(item_ids)} 首")
    return {"ok": completed == len(item_ids), "auth_valid": True, "results": results, "message": f"完成 {completed}/{len(item_ids)} 首"}

# ---------- 每日整体流程（签到 +（可选）间隔任务，同一浏览器不关闭）----------
def do_daily_run(
    profile_dir: str,
    account_id: Optional[int],
    *,
    run_checkin: bool = True,
    run_interval: bool = False,
    interval_kind: str = "publish",   # 'publish' 或 'vip'
    publish_msg: str = "",
    search_keyword: str = "你好",
) -> dict:
    """
    在**同一浏览器会话**内按需执行签到 / 间隔任务，中途不关闭浏览器：
    - 签到在主标签页；
    - 间隔任务（发布动态或 VIP 领取）在新标签页；
    - 最后统一关闭。
    返回 {checkin, interval, cookie_str}。
    """
    bus.status(account_id, "running", "开始执行任务")
    result: dict[str, Any] = {"checkin": None, "interval": None}

    with run_with_context(profile_dir, account_id=account_id, label="每日任务") as (context, page):
        if not _validate_session(page, account_id):
            result.update({"ok": False, "auth_valid": False, "message": "cookie expired", "cookie_str": ""})
            return result
        result["auth_valid"] = True
        # 1) 签到（主标签页）
        if run_checkin:
            result["checkin"] = _checkin_on_page(page, account_id)

        # 2) 间隔任务（新标签页，浏览器不关）
        if run_interval:
            _emit(account_id, f"执行间隔任务（{interval_kind}），新建标签页")
            tab = context.new_page()
            try:
                from app.config import BROWSER_TIMEOUT_MS

                tab.set_default_timeout(BROWSER_TIMEOUT_MS)
                if interval_kind == "vip":
                    result["interval"] = _vip_claim_on_page(context, tab, account_id)
                else:
                    msg = publish_msg or "分享音乐"
                    result["interval"] = _publish_on_page(tab, account_id, msg, search_keyword)
            finally:
                try:
                    tab.close()
                except Exception:
                    pass

        result["cookie_str"] = cookies_to_str(context.cookies("https://music.163.com"))

    bus.status(account_id, "done", "任务完成")
    return result


def _click_or_invoke_checkin(page: Page, account_id: Optional[int], mission: dict) -> dict:
    """
    优先在页面同源上下文调用签到接口（走页面自带的加密请求管线），
    监听 reward/obtain 响应确认。DOM 按钮点击作为候选路径。
    """
    def _is_reward(resp) -> bool:
        try:
            return S.REWARD_OBTAIN_API in resp.url and resp.request.method == "POST"
        except Exception:
            return False

    user_mission_id = mission.get("userMissionId")
    period = mission.get("period")

    # 已签到时按钮不会消失，而是变成：
    # <div class="sign-in-btn signed">已签到, 明日继续</div>
    signed_result = _musician_signed_result(page, account_id)
    if signed_result is not None:
        return signed_result

    # 路径 A：DOM 点击签到按钮（候选选择器；确切选择器建议有头浏览器现场核对）
    checkin_selectors = [
        "button:has-text('签到')",
        "a:has-text('签到')",
        "div[class*='sign']:has-text('签到')",
        "text=立即签到",
        "text=签到",
    ]
    for scope in scopes(page):
        for sel in checkin_selectors:
            try:
                loc = scope.locator(sel)
                if loc.count() == 0:
                    continue
                _emit(account_id, f"尝试点击签到按钮：{sel}")
                try:
                    with page.expect_response(_is_reward, timeout=8000) as resp_info:
                        loc.first.click(force=True)
                    data = resp_info.value.json()
                    if isinstance(data, dict) and data.get("code") == 200:
                        _emit(account_id, "签到成功（DOM 点击）")
                        return {"ok": True, "message": "签到成功"}
                    _emit(account_id, f"签到接口返回：{str(data)[:120]}", "warn")
                except Exception:
                    # 点击了但没监听到接口，继续尝试其他选择器
                    continue
            except Exception:
                continue

    _emit(
        account_id,
        f"未能通过 DOM 点击完成签到（userMissionId={user_mission_id}, period={period}）；"
        "该账号页面签到按钮选择器可能需现场核对",
        "warn",
    )
    return {"ok": False, "message": "未能完成签到，页面按钮结构可能已变化"}


def _do_daily_task(page: Page, account_id: Optional[int]) -> dict:
    """日常签到：打开首页，点击侧边栏签到按钮（data-action='checkin'），监听 dailyTask 确认。"""
    def _is_daily(resp) -> bool:
        try:
            return S.DAILY_TASK_API in resp.url and resp.request.method == "POST"
        except Exception:
            return False

    _emit(account_id, "打开首页，准备日常签到...")
    try:
        page.goto(S.DAILY_HOME_URL, wait_until="domcontentloaded")
    except Exception as e:  # noqa: BLE001
        _emit(account_id, f"打开首页失败：{e}", "warn")
        return {"ok": False, "message": f"打开首页失败：{e}"}

    # 等待侧边栏签到按钮出现（按稳定属性定位，不依赖文案）
    btn_loc = None
    deadline = time.time() + 10
    while time.time() < deadline and btn_loc is None:
        for scope in scopes(page):
            try:
                loc = scope.locator(S.SEL_DAILY_SIGN_BTN)
                if loc.count() > 0:
                    btn_loc = loc.first
                    break
            except Exception:
                continue
        if btn_loc is None:
            page.wait_for_timeout(300)

    if btn_loc is None:
        # 检查是否已签到（已签到时按钮变为 u-btn2-dis，无 data-action='checkin'）
        already_signed = False
        for scope in scopes(page):
            try:
                if scope.locator("a.u-btn2-dis").count() > 0:
                    already_signed = True
                    break
            except Exception:
                continue
        if already_signed:
            _emit(account_id, "日常签到：今日已签到")
            return {"ok": True, "message": "今日已签到"}
        else:
            _emit(account_id, "日常签到：未找到签到按钮（可能未登录或页面结构变化）", "warn")
            return {"ok": False, "message": "未找到签到按钮（可能未登录或页面结构变化）"}

    # 判断是否已签到：data-action='checkin' 存在即视为可签，点击并监听接口确认。
    try:
        with page.expect_response(_is_daily, timeout=8000) as resp_info:
            try:
                btn_loc.click(force=True)
            except Exception:
                btn_loc.evaluate("el => el.click()")
        data = resp_info.value.json()
        code = data.get("code") if isinstance(data, dict) else None
        if code == 200:
            _emit(account_id, "日常签到成功")
            return {"ok": True, "message": "签到成功"}
        elif code == -2:
            _emit(account_id, "日常签到：今日已签到")
            return {"ok": True, "message": "今日已签到"}
        else:
            _emit(account_id, f"日常签到返回：{str(data)[:100]}", "warn")
            return {"ok": False, "message": f"接口返回异常：{str(data)[:100]}"}
    except Exception as e:  # noqa: BLE001
        _emit(account_id, "日常签到：点击后未捕获 dailyTask 接口（可能已完成）")
        return {"ok": False, "message": f"未捕获签到接口：{e}"}


# ---------- 发布动态（页面级）----------
def _publish_on_page(page: Page, account_id: Optional[int], msg: str, search_keyword: str = "你好") -> dict:
    """在给定页面（可为新标签页）上发布配乐笔记并删除。"""
    def _is_share(resp) -> bool:
        try:
            return S.SHARE_API in resp.url and resp.request.method == "POST"
        except Exception:
            return False

    _emit(account_id, "打开动态页，准备发布笔记...")
    page.goto(S.FRIEND_URL, wait_until="networkidle")

    scope = first_with_selector(page, S.SEL_PUB_EVENT)
    if scope.locator(S.SEL_PUB_EVENT).count() == 0:
        _emit(account_id, "未找到发笔记按钮，疑似未登录", "warn")
        return {"ok": False, "event_id": None, "message": "not logged in"}

    try:
        scope.locator(S.SEL_PUB_EVENT).first.click()
        _emit(account_id, "已点击发笔记")
        scope.locator(S.SEL_NOTE_TEXTAREA).first.fill(msg)
        _emit(account_id, f"已输入文案：{msg[:20]}...")

        scope.get_by_text(S.SEL_ADD_MUSIC, exact=True).first.click()
        search = first_with_selector(page, S.SEL_MUSIC_SEARCH)
        search.locator(S.SEL_MUSIC_SEARCH).first.fill(search_keyword)
        search.locator(S.SEL_MUSIC_SEARCH).first.press("Enter")
        time.sleep(2)
        result_scope = first_with_selector(page, S.SEL_SEARCH_RESULT)
        result_scope.locator(S.SEL_SEARCH_RESULT).first.click()
        _emit(account_id, "已选择配乐")

        share_scope = first_with_selector(page, S.SEL_SHARE_BTN)
        with page.expect_response(_is_share, timeout=20000) as resp_info:
            share_scope.locator(S.SEL_SHARE_BTN).first.click()
        data = resp_info.value.json()
    except Exception as e:  # noqa: BLE001
        _emit(account_id, f"发布流程异常：{e}", "error")
        return {"ok": False, "event_id": None, "message": str(e)}

    event_id = None
    if isinstance(data, dict):
        event_id = (data.get("event") or {}).get("id")
    if not event_id:
        _emit(account_id, f"分享未返回 event.id：{str(data)[:120]}", "warn")
        return {"ok": False, "event_id": None, "message": "no event id"}

    _emit(account_id, f"发布成功，event_id={event_id}，10 秒后删除")
    time.sleep(10)
    _delete_event(page, account_id, event_id)
    return {"ok": True, "event_id": event_id, "message": "published"}


def do_publish_note(
    profile_dir: str,
    msg: str,
    account_id: Optional[int] = None,
    search_keyword: str = "你好",
) -> dict:
    """独立入口：自己开浏览器发布动态（供手动单独触发）。"""
    bus.status(account_id, "running", "发布动态")
    with run_with_context(profile_dir, account_id=account_id, label="发布动态") as (context, page):
        res = _publish_on_page(page, account_id, msg, search_keyword)
        res["cookie_str"] = cookies_to_str(context.cookies("https://music.163.com"))
        bus.status(account_id, "done", "发布动态完成")
        return res


def _delete_event(page: Page, account_id: Optional[int], event_id: str) -> None:
    """同源删除动态。"""
    def _is_delete(resp) -> bool:
        try:
            return S.EVENT_DELETE_API in resp.url and resp.request.method == "POST"
        except Exception:
            return False

    try:
        # 真实结构：删除项在 <ul class="mng f-hide"> 内，默认隐藏。
        # 需先点「笔记管理」展开箭头 [data-action='unfold']，再点 [data-action='delete']，
        # 最后点确认弹窗的 [data-action='ok']。
        for scope in scopes(page):
            del_btn = scope.locator("[data-action='delete']")
            if del_btn.count() == 0:
                continue

            # 1) 展开管理菜单
            try:
                unfold = scope.locator("[data-action='unfold']")
                if unfold.count() > 0:
                    unfold.first.click(timeout=2000)
                    page.wait_for_timeout(300)
            except Exception:
                pass

            try:
                with page.expect_response(_is_delete, timeout=8000) as resp_info:
                    try:
                        del_btn.first.click(timeout=2000)
                    except Exception:
                        # 兜底：JS 原生 click，绕过可见性检查
                        del_btn.first.evaluate("el => el.click()")
                    # 确认弹窗：优先 data-action='ok'，兜底文本
                    page.wait_for_timeout(300)
                    ok = scope.locator("[data-action='ok']")
                    try:
                        if ok.count() > 0:
                            ok.first.click(timeout=2000)
                        else:
                            scope.get_by_text("确定", exact=True).first.click(timeout=2000)
                    except Exception:
                        pass
                data = resp_info.value.json()
                _emit(account_id, f"删除动态结果：{str(data)[:80]}")
                return
            except Exception as e:  # noqa: BLE001
                _emit(account_id, f"该 scope 删除尝试失败：{e}", "warn")
                continue

        _emit(account_id, f"未找到删除入口，event_id={event_id} 未删除", "warn")
    except Exception as e:  # noqa: BLE001
        _emit(account_id, f"删除动态失败：{e}", "warn")


# ---------- VIP 领取（页面级）----------
def _vip_claim_on_page(context, page: Page, account_id: Optional[int], timeout_ms: int = 30000) -> dict:
    """在给定页面（可为新标签页）上领取 VIP 并解析 furtherVipGetTime。"""
    def _is_vip(resp) -> bool:
        try:
            return (
                S.VIP_INFO_URL_SUBSTR in resp.url
                and "interface.music.163.com" in resp.url
                and resp.request.method == "POST"
            )
        except Exception:
            return False

    page.goto(S.VIP_RIGHT_URL, wait_until="domcontentloaded")

    deadline = time.time() + timeout_ms / 1000
    renew_btn = None
    while time.time() < deadline and renew_btn is None:
        for scope in scopes(page):
            try:
                container = scope.locator(S.SEL_VIP_CONTAINER)
                if container.count() == 0:
                    continue
                btn = container.locator(f"div.link-wrapper {S.SEL_VIP_CHECK}")
                if btn.count() == 0:
                    btn = container.locator(S.SEL_VIP_CHECK)
                if btn.count() > 0:
                    renew_btn = btn.first
                    break
            except Exception:
                continue
        if renew_btn is None:
            page.wait_for_timeout(500)

    further_time = None
    if renew_btn is not None:
        _emit(account_id, "找到 VIP 续期/领取按钮，点击并监听 vip/info")
        try:
            renew_btn.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            with context.expect_event("response", predicate=_is_vip, timeout=timeout_ms) as resp_info:
                try:
                    with context.expect_page(timeout=5000) as new_page_info:
                        renew_btn.click(force=True)
                    np = new_page_info.value
                    np.wait_for_load_state("domcontentloaded", timeout=10000)
                    np.wait_for_timeout(3000)
                except Exception:
                    pass
            further_time = _parse_vip(resp_info.value.json(), account_id)
        except Exception as e:  # noqa: BLE001
            _emit(account_id, f"点击 VIP 按钮/解析失败：{e}", "warn")
    else:
        _emit(account_id, "未找到 VIP 按钮，尝试 reload 监听 vip/info", "warn")
        try:
            with context.expect_event("response", predicate=_is_vip, timeout=timeout_ms) as resp_info:
                page.reload(wait_until="domcontentloaded")
            further_time = _parse_vip(resp_info.value.json(), account_id)
        except Exception as e:  # noqa: BLE001
            _emit(account_id, f"监听 vip/info 失败：{e}", "warn")

    return {"ok": further_time is not None, "further_vip_get_time": further_time, "message": "vip done"}


def do_vip_claim(profile_dir: str, account_id: Optional[int] = None, timeout_ms: int = 30000) -> dict:
    """独立入口：自己开浏览器领取 VIP（供手动单独触发）。"""
    bus.status(account_id, "running", "领取 VIP 权益")
    with run_with_context(profile_dir, account_id=account_id, label="VIP领取") as (context, page):
        res = _vip_claim_on_page(context, page, account_id, timeout_ms)
        bus.status(account_id, "done", "VIP 任务完成")
        return res


def _parse_vip(data: Any, account_id: Optional[int]) -> Optional[int]:
    try:
        t = (data or {}).get("data", {}).get("furtherVipGetTime")
        if isinstance(t, str) and t.isdigit():
            t = int(t)
        elif isinstance(t, (int, float)):
            t = int(t)
        else:
            t = None
        if t:
            readable = datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M:%S")
            _emit(account_id, f"下次可领取 VIP 时间：{readable}")
        else:
            _emit(account_id, "未解析到 furtherVipGetTime", "warn")
        # 任务进度日志
        further = (data or {}).get("data", {}).get("furtherTask", {})
        for child in (further.get("children") or []):
            if isinstance(child, dict) and (child.get("description") == S.VIP_TASK_NAME):
                _emit(account_id, f"任务「{S.VIP_TASK_NAME}」进度：{child.get('progressRate')}")
                break
        return t
    except Exception as e:  # noqa: BLE001
        _emit(account_id, f"解析 vip/info 出错：{e}", "warn")
        return None
