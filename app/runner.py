"""
账号任务编排：把 login/checkin/publish/vip 串起来，处理频率控制与 DB 状态更新。
所有浏览器操作通过 worker.submit 投递到 worker 线程串行执行。
"""

from __future__ import annotations

import time
import requests
from datetime import datetime
from typing import Optional

from app.browser.manager import worker
from app.event_bus import bus
from app.logging_conf import logger
from app.notify import send_configured_notification
from app.account_identity import account_label
from app import repository as repo


def _run_blocking(fn, *args, **kwargs):
    """把浏览器任务投递到 worker 线程并等待结果。"""
    return worker.submit(fn, *args, **kwargs).result()


def _interval_days() -> int:
    return repo.get_setting_int("execution_interval_days", 3)


def _max_monthly_sends() -> int:
    return repo.get_setting_int("max_monthly_sends", 4)


def _record_auth_state(account_id: int, result: dict) -> bool:
    """任务发现服务端会话失效时立即同步账号列表状态。"""
    if result.get("auth_valid") is False:
        repo.update_account(account_id, cookie_status="expired")
        repo.add_log(account_id, "auth", "fail", "cookie expired; login required")
        # 数据库落库后再广播一次，确保前端刷新时能读到 expired。
        bus.status(account_id, "login_fail", "Cookie 已失效，请重新登录")
        return False
    return True


# ---------- 登录 ----------
def run_login(account_id: int) -> dict:
    acc = repo.get_account(account_id)
    if not acc:
        return {"ok": False, "message": "account not found"}

    from app.browser.login import login_account

    repo.add_log(account_id, "login", "info", "开始登录")
    try:
        res = _run_blocking(login_account, acc["profile_dir"], acc["phone"], acc["password"], account_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("登录任务异常")
        repo.add_log(account_id, "login", "fail", str(e))
        return {"ok": False, "message": str(e)}

    if res.get("ok"):
        fields = {
            "cookie_status": "ok",
            "last_login_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if res.get("uid"):
            fields["uid"] = res["uid"]
        if res.get("nickname"):
            fields["nickname"] = res["nickname"]
        repo.update_account(account_id, **fields)
        repo.add_log(account_id, "login", "success", res.get("message", "ok"))
    else:
        repo.update_account(account_id, cookie_status="expired")
        repo.add_log(account_id, "login", "fail", res.get("message", "fail"))
    return res


# ---------- 每日签到 ----------
def run_checkin(account_id: int) -> dict:
    acc = repo.get_account(account_id)
    if not acc:
        return {"ok": False, "message": "account not found"}

    from app.browser.tasks import do_checkin

    repo.add_log(account_id, "checkin", "info", "开始签到")
    try:
        res = _run_blocking(do_checkin, acc["profile_dir"], account_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("签到任务异常")
        repo.add_log(account_id, "checkin", "fail", str(e))
        return {"ok": False, "message": str(e)}

    _record_auth_state(account_id, res)
    musician = res.get("musician_checkin") or {}
    daily = res.get("daily_checkin") or {}
    repo.add_log(account_id, "musician_checkin", "success" if musician.get("ok") else "info", musician.get("message", ""))
    repo.add_log(account_id, "daily_checkin", "success" if daily.get("ok") else "fail", daily.get("message", ""))
    return res


def run_listen(account_id: int) -> dict:
    acc = repo.get_account(account_id)
    if not acc:
        return {"ok": False, "message": "account not found"}
    api_url = (repo.get_setting("listen_api_url", "") or acc.get("listen_api_url") or "").strip()
    if not api_url:
        return {"ok": False, "message": "未配置听歌 API 地址，请先点击「加入听歌」"}

    from app.api.listen import _account_md5, _hourly_apikey, _server_url
    from app.browser.tasks import do_listen_music

    account_md5 = _account_md5(acc["phone"])
    apikey = _hourly_apikey(account_md5)
    headers = {"X-API-Key": apikey}
    try:
        next_resp = requests.get(
            _server_url(api_url, "/api/next"),
            headers=headers,
            timeout=10,
        )
        if next_resp.status_code == 404:
            return {"ok": False, "message": "听歌服务暂无可播放任务"}
        if not 200 <= next_resp.status_code < 300:
            return {"ok": False, "message": f"获取听歌任务失败：HTTP {next_resp.status_code}"}
        next_data = next_resp.json() or {}
        target = str(next_data.get("netease_item_id") or "").strip()
        if not target:
            return {"ok": False, "message": "听歌服务未返回歌曲 ID"}

        repo.add_log(account_id, "listen", "info", f"开始播放歌曲：{target}")
        result = _run_blocking(do_listen_music, acc["profile_dir"], target, account_id)
        if not result.get("ok"):
            repo.update_account(
                account_id,
                listen_status="error",
                listen_error=result.get("message", "播放失败"),
            )
            repo.add_log(account_id, "listen", "fail", result.get("message", "播放失败"))
            return result

        finish_resp = requests.post(
            _server_url(api_url, "/api/play/finish"),
            json={"account_md5": account_md5, "netease_item_id": target},
            headers=headers,
            timeout=10,
        )
        if not 200 <= finish_resp.status_code < 300:
            message = f"提交听歌结果失败：HTTP {finish_resp.status_code}"
            repo.add_log(account_id, "listen", "fail", message)
            repo.update_account(account_id, listen_status="error", listen_error=message)
            return {"ok": False, "message": message}
        repo.update_account(
            account_id,
            listen_status="normal",
            listen_error="",
            listen_play_count=(acc.get("listen_play_count") or 0) + 1,
            listen_last_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        repo.add_log(account_id, "listen", "success", f"听歌完成：{target}")
        return {"ok": True, "message": f"听歌完成：{target}"}
    except requests.RequestException as exc:
        message = f"连接听歌 API 失败：{exc}"
        repo.update_account(account_id, listen_status="error", listen_error=message)
        repo.add_log(account_id, "listen", "fail", message)
        return {"ok": False, "message": message}

def run_auto_listen_for_account(account_id: int) -> None:
    """在全局听歌开始时间执行已加入账号的每日听歌任务。"""
    acc = repo.get_account(account_id)
    if not acc or not acc["enabled"] or acc.get("listen_status") != "normal":
        return
    if not (repo.get_setting("listen_api_url", "") or "").strip():
        return
    if not (repo.get_setting("listen_item_id", "") or "").strip():
        return

    daily_max = max(0, repo.get_setting_int("listen_daily_max", 1))
    monthly_max = max(0, repo.get_setting_int("listen_monthly_max", 30))
    today_count = repo.count_success_logs_today(account_id, "listen")
    month_count = repo.count_success_logs_this_month(account_id, "listen")
    completed = 0
    while (
        today_count + completed < daily_max
        and month_count + completed < monthly_max
    ):
        result = run_listen(account_id)
        if not result.get("ok"):
            break
        completed += 1
    if completed:
        _emit_run(
            account_id,
            f"自动听歌完成：{completed} 首"
            f"（今日 {today_count + completed}/{daily_max}，"
            f"本月 {month_count + completed}/{monthly_max}）",
        )

# ---------- 间隔任务（发布动态 / VIP 领取）----------
def _month_tag() -> str:
    return datetime.now().strftime("%Y-%m")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _decide_interval(acc: dict) -> dict:
    """
    判断今天是否该执行间隔任务、执行哪种。
    返回 {run: bool, kind: 'vip'|'publish', reason, monthly, month_tag}。
    规则：
      - VIP 可领取日（now >= further_vip_get_time）→ 领 VIP（优先）。
      - 否则按 execution_interval_days：距上次发布 >= 间隔天数，且本月未超上限 → 发布动态。
    """
    month_tag = _month_tag()
    monthly = acc["monthly_sends"] or 0
    if acc["month_tag"] != month_tag:
        monthly = 0

    now_ms = int(time.time() * 1000)
    further = acc["further_vip_get_time"]
    if further and now_ms >= int(further):
        return {"run": True, "kind": "vip", "reason": "VIP 可领取日", "monthly": monthly, "month_tag": month_tag}

    # 发布间隔判断
    last = acc["last_send_date"]
    days = _interval_days()
    due = True
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            due = (datetime.now() - last_dt).days >= days
        except Exception:
            due = True
    if not due:
        return {"run": False, "kind": "publish", "reason": f"距上次发布不足 {days} 天", "monthly": monthly, "month_tag": month_tag}
    max_sends = _max_monthly_sends()
    if monthly >= max_sends:
        return {"run": False, "kind": "publish", "reason": f"本月已达上限 {max_sends}", "monthly": monthly, "month_tag": month_tag}
    return {"run": True, "kind": "publish", "reason": "达到发布间隔", "monthly": monthly, "month_tag": month_tag}


def run_interval_task(account_id: int) -> dict:
    """手动单独触发间隔任务：强制执行一次（VIP 日则 VIP，否则发布）。"""
    acc = repo.get_account(account_id)
    if not acc:
        return {"ok": False, "message": "account not found"}

    decision = _decide_interval(acc)
    if decision["kind"] == "vip":
        from app.browser.tasks import do_vip_claim

        try:
            res = _run_blocking(do_vip_claim, acc["profile_dir"], account_id)
        except Exception as e:  # noqa: BLE001
            repo.add_log(account_id, "vip", "fail", str(e))
            return {"ok": False, "message": str(e)}
        if res.get("further_vip_get_time"):
            repo.update_account(account_id, further_vip_get_time=res["further_vip_get_time"])
        repo.add_log(account_id, "vip", "success" if res.get("ok") else "info", res.get("message", ""))
        return res

    from app.browser.tasks import do_publish_note

    msg = f"{datetime.now().strftime('%Y年%m月%d日 %H:%M')} 分享音乐"
    try:
        res = _run_blocking(do_publish_note, acc["profile_dir"], msg, account_id)
    except Exception as e:  # noqa: BLE001
        repo.add_log(account_id, "publish", "fail", str(e))
        return {"ok": False, "message": str(e)}
    if res.get("ok"):
        repo.update_account(
            account_id,
            monthly_sends=decision["monthly"] + 1,
            month_tag=decision["month_tag"],
            last_send_date=_today(),
        )
    repo.add_log(account_id, "publish", "success" if res.get("ok") else "fail", res.get("message", ""))
    return res


def _emit_run(account_id: int, line: str) -> None:
    logger.info(line)
    bus.log(account_id, line)


def _notify_manual_result(account_id: int, acc: dict, tasks: list[str], lines: list[str], *, ok: bool) -> None:
    """发送网页手动执行结果；通知失败不影响任务本身。"""
    task_names = {"checkin": "签到", "publish": "发布动态", "vip": "领取 VIP", "listen": "听歌"}
    selected = "、".join(task_names[t] for t in tasks if t in task_names)
    account_name = account_label(account_id, account=acc)
    content = "\n".join([
        f"账号：{account_name}",
        f"手动任务：{selected or '-'}",
        *lines,
    ])
    try:
        sent = send_configured_notification(
            content,
            title="网易音乐人手动任务",
            event="manual_result",
            extra={"account": account_name, "tasks": tasks, "ok": ok},
        )
        if not sent:
            _emit_run(account_id, "手动任务结果通知未发送（未配置通知渠道或发送失败）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"手动任务结果通知失败：{e}")


# ---------- 手动多选执行（网页「执行」弹窗）----------
def run_selected(account_id: int, tasks: list[str]) -> None:
    """
    手动执行选中的任务，全部在**同一浏览器会话**内完成（签到主标签页、间隔任务新标签页）。
    tasks 取值：'checkin'（签到）、'publish'（发布动态）、'vip'（领取 VIP）。
    发布与 VIP 互斥，若同时勾选以 VIP 优先（同一次只做一种间隔任务）。
    """
    acc = repo.get_account(account_id)
    if not acc:
        return

    _emit_run(account_id, f"手动执行已选择任务：{', '.join(tasks)}")

    want_checkin = "checkin" in tasks
    want_vip = "vip" in tasks
    want_publish = "publish" in tasks
    want_listen = "listen" in tasks
    run_interval = want_vip or want_publish
    interval_kind = "vip" if want_vip else "publish"

    from app.browser.tasks import do_daily_run

    decision_month = _month_tag()
    monthly = acc["monthly_sends"] or 0
    if acc["month_tag"] != decision_month:
        monthly = 0

    publish_msg = f"{datetime.now().strftime('%Y年%m月%d日 %H:%M')} 分享音乐"
    try:
        if want_checkin or run_interval:
            res = _run_blocking(
                do_daily_run,
                acc["profile_dir"],
                account_id,
                run_checkin=want_checkin,
                run_interval=run_interval,
                interval_kind=interval_kind,
                publish_msg=publish_msg,
            )
        else:
            res = {"ok": True, "auth_valid": True}
    except Exception as e:  # noqa: BLE001
        logger.exception("手动执行任务异常")
        repo.add_log(account_id, "manual", "fail", str(e))
        _notify_manual_result(account_id, acc, tasks, [f"执行失败：{e}"], ok=False)
        return

    if not _record_auth_state(account_id, res):
        _notify_manual_result(account_id, acc, tasks, ["执行失败：Cookie 已失效，请重新登录"], ok=False)
        return

    result_lines: list[str] = []
    all_ok = True
    checkin = res.get("checkin")
    if want_checkin and checkin is not None:
        musician = checkin.get("musician_checkin") or {}
        daily = checkin.get("daily_checkin") or {}
        repo.add_log(account_id, "musician_checkin", "success" if musician.get("ok") else "info", musician.get("message", ""))
        repo.add_log(account_id, "daily_checkin", "success" if daily.get("ok") else "fail", daily.get("message", ""))
        musician_ok = bool(musician.get("ok"))
        daily_ok = bool(daily.get("ok"))
        all_ok = all_ok and musician_ok and daily_ok
        result_lines.append(f"音乐人签到：{musician.get('message') or ('成功' if musician_ok else '未完成')}")
        result_lines.append(f"日常签到：{daily.get('message') or ('成功' if daily_ok else '未完成')}")

    interval = res.get("interval")
    if run_interval and interval is not None:
        if interval_kind == "vip":
            if interval.get("further_vip_get_time"):
                repo.update_account(account_id, further_vip_get_time=interval["further_vip_get_time"])
            repo.add_log(account_id, "vip", "success" if interval.get("ok") else "info", interval.get("message", ""))
            interval_ok = bool(interval.get("ok"))
            all_ok = all_ok and interval_ok
            result_lines.append(f"VIP 领取：{'成功' if interval_ok else interval.get('message', '未完成')}")
        else:
            if interval.get("ok"):
                repo.update_account(
                    account_id,
                    monthly_sends=monthly + 1,
                    month_tag=decision_month,
                    last_send_date=_today(),
                )
            repo.add_log(account_id, "publish", "success" if interval.get("ok") else "fail", interval.get("message", ""))
            interval_ok = bool(interval.get("ok"))
            all_ok = all_ok and interval_ok
            result_lines.append(f"发布动态：{'成功' if interval_ok else interval.get('message', '失败')}")

    if want_checkin and checkin is None:
        all_ok = False
        result_lines.append("音乐人签到：未返回执行结果")
        result_lines.append("日常签到：未返回执行结果")
    if run_interval and interval is None:
        all_ok = False
        result_lines.append(f"{'VIP 领取' if interval_kind == 'vip' else '发布动态'}：未返回执行结果")

    if want_listen:
        listen_result = run_listen(account_id)
        listen_ok = bool(listen_result.get("ok"))
        all_ok = all_ok and listen_ok
        result_lines.append(f"听歌：{listen_result.get('message', '未完成')}")

    _notify_manual_result(account_id, acc, tasks, result_lines, ok=all_ok)


# ---------- 完整每日流程（供调度器调用）----------
def run_daily_for_account(account_id: int) -> None:
    """
    到运行时间执行：同一浏览器内先签到，再按判断决定是否执行间隔任务
    （VIP 可领取日领 VIP，否则达到间隔天数则发布动态），中途不关浏览器。
    """
    acc = repo.get_account(account_id)
    if not acc or not acc["enabled"]:
        return

    from app.browser.tasks import do_daily_run

    decision = _decide_interval(acc)
    _emit_run(account_id, f"每日任务开始；间隔任务判断：{decision['reason']}"
                          f"（{'执行' if decision['run'] else '跳过'}）")

    publish_msg = f"{datetime.now().strftime('%Y年%m月%d日 %H:%M')} 分享音乐"
    try:
        res = _run_blocking(
            do_daily_run,
            acc["profile_dir"],
            account_id,
            run_checkin=True,
            run_interval=decision["run"],
            interval_kind=decision["kind"],
            publish_msg=publish_msg,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("每日任务异常")
        repo.add_log(account_id, "daily", "fail", str(e))
        return

    if not _record_auth_state(account_id, res):
        return

    # 分别记录音乐人签到和日常签到结果
    checkin = res.get("checkin") or {}
    musician = checkin.get("musician_checkin") or {}
    daily = checkin.get("daily_checkin") or {}
    repo.add_log(account_id, "musician_checkin", "success" if musician.get("ok") else "info", musician.get("message", ""))
    repo.add_log(account_id, "daily_checkin", "success" if daily.get("ok") else "fail", daily.get("message", ""))

    # 记录并落库间隔任务结果
    interval = res.get("interval")
    lines = [f"账号 {account_label(account_id, account=acc)}："]
    lines.append(f"音乐人签到：{musician.get('message') or ('成功' if musician.get('ok') else '无签到任务')}")
    lines.append(f"日常签到：{daily.get('message') or ('成功' if daily.get('ok') else '未完成')}")

    if decision["run"] and interval is not None:
        if decision["kind"] == "vip":
            if interval.get("further_vip_get_time"):
                repo.update_account(account_id, further_vip_get_time=interval["further_vip_get_time"])
            repo.add_log(account_id, "vip", "success" if interval.get("ok") else "info", interval.get("message", ""))
            lines.append(f"VIP 领取：{'成功' if interval.get('ok') else '未完成'}")
        else:
            if interval.get("ok"):
                repo.update_account(
                    account_id,
                    monthly_sends=decision["monthly"] + 1,
                    month_tag=decision["month_tag"],
                    last_send_date=_today(),
                )
            repo.add_log(account_id, "publish", "success" if interval.get("ok") else "fail", interval.get("message", ""))
            lines.append(f"发布动态：{'成功' if interval.get('ok') else interval.get('message', '失败')}")
    else:
        lines.append(f"间隔任务：跳过（{decision['reason']}）")

    try:
        send_configured_notification("\n".join(lines), title="网易音乐人每日任务", event="daily_result")
    except Exception:
        pass
