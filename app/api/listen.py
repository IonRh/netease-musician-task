"""加入听歌服务：由本地后端代为调用用户配置的 Server API。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import repository as repo

router = APIRouter(prefix="/api/listen", tags=["listen"])


class JoinListenRequest(BaseModel):
    account_id: int
    api_url: str = Field(..., min_length=1)
    netease_item_id: str = Field(..., min_length=1, max_length=128)

def _local_listen_status(account: dict, *, status: str | None = None, error: str | None = None) -> dict:
    return {
        "status": status or account.get("listen_status") or "unconfigured",
        "api_url": account.get("listen_api_url") or "",
        "item_id": account.get("listen_item_id") or "",
        "error": account.get("listen_error") or "" if error is None else error,
        "play_count": int(account.get("listen_play_count") or 0),
        "received_count": int(account.get("listen_received_count") or 0),
        "last_at": account.get("listen_last_at"),
    }

def _listen_progress(record: dict | None) -> dict:
    record = record or {}
    return {
        "today_listen_count": int(record.get("today_listen_count") or 0),
        "daily_listen_limit": int(record.get("daily_listen_limit") or 0),
        "monthly_listen_count": int(record.get("monthly_listen_count") or 0),
        "monthly_listen_limit": int(record.get("monthly_listen_limit") or 0),
        "today_listened_count": int(record.get("listened_count") or 0),
        "monthly_listened_count": int(record.get("monthly_listened_count") or 0),
    }


def _server_url(api_url: str, path: str) -> str:
    base = api_url.strip()
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, "API 地址必须是 http 或 https URL")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _account_md5(phone: str) -> str:
    return hashlib.md5(phone.strip().encode("utf-8")).hexdigest()


def _hourly_apikey(account_md5: str) -> str:
    time_part = datetime.now().strftime("%Y%m%d%H")
    return hashlib.md5(f"{account_md5}{time_part}".encode("utf-8")).hexdigest()


def _response_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("detail") or data.get("message") or data)
    except Exception:
        pass
    return resp.text[:300] or f"HTTP {resp.status_code}"


@router.post("/join")
def join_listen(body: JoinListenRequest) -> dict:
    account = repo.get_account(body.account_id)
    if not account:
        raise HTTPException(404, "账号不存在")

    api_url = body.api_url.strip().rstrip("/")
    item_id = body.netease_item_id.strip()
    repo.set_setting("listen_api_url", api_url)
    account_md5 = _account_md5(account["phone"])
    apikey = _hourly_apikey(account_md5)
    daily_limit = repo.get_setting_int("listen_daily_max", 1)
    monthly_limit = repo.get_setting_int("listen_monthly_max", 30)
    join_url = _server_url(body.api_url, "/api/join")
    update_url = _server_url(body.api_url, "/api/update")
    headers = {"X-API-Key": apikey, "Content-Type": "application/json"}

    try:
        join_resp = requests.post(
            join_url,
            json={
                "account_md5": account_md5,
                "apikey": apikey,
                "daily_listen_limit": max(0, daily_limit),
                "monthly_listen_limit": max(0, monthly_limit),
            },
            headers=headers,
            timeout=10,
        )
        if join_resp.status_code not in (200, 201, 409):
            raise HTTPException(
                join_resp.status_code,
                f"加入听歌失败：{_response_detail(join_resp)}",
            )

        update_resp = requests.post(
            update_url,
            json={
                "account_md5": account_md5,
                "netease_item_id": item_id,
                "daily_listen_limit": max(0, daily_limit),
                "monthly_listen_limit": max(0, monthly_limit),
            },
            headers=headers,
            timeout=10,
        )
        if not 200 <= update_resp.status_code < 300:
            raise HTTPException(
                update_resp.status_code,
                f"保存歌曲/专辑 ID 失败：{_response_detail(update_resp)}",
            )
        try:
            result = update_resp.json()
        except Exception:
            result = {"ok": True}
        remote = result if isinstance(result, dict) else {}
        repo.update_account(
            body.account_id,
            listen_api_url=api_url,
            listen_item_id=item_id,
            listen_status="normal",
            listen_error="",
            listen_received_count=int(remote.get("listened_count") or 0),
        )
        return {
            "ok": True,
            "account_md5": account_md5,
            "record": result,
            "progress": _listen_progress(result),
            "listen": _local_listen_status(repo.get_account(body.account_id), status="normal"),
        }
    except HTTPException:
        repo.update_account(body.account_id, listen_status="error", listen_error="加入听歌失败")
        raise
    except requests.RequestException as exc:
        repo.update_account(body.account_id, listen_status="error", listen_error=str(exc)[:500])
        raise HTTPException(502, f"连接听歌 API 失败：{exc}") from exc

@router.get("/status/{account_id}")
def listen_status(account_id: int) -> dict:
    account = repo.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    api_url = (repo.get_setting("listen_api_url", "") or account.get("listen_api_url") or "").strip()
    if not api_url:
        return {"ok": True, "listen": _local_listen_status(account)}

    account_md5 = _account_md5(account["phone"])
    headers = {"X-API-Key": _hourly_apikey(account_md5)}
    try:
        resp = requests.get(
            _server_url(api_url, f"/api/listen-records/{account_md5}"),
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            repo.update_account(
                account_id,
                listen_status="unconfigured",
                listen_error="",
                listen_item_id="",
                listen_received_count=0,
            )
            account = repo.get_account(account_id)
            return {
                "ok": True,
                "listen": _local_listen_status(account, status="unconfigured"),
                "progress": _listen_progress(None),
            }
        if not 200 <= resp.status_code < 300:
            detail = _response_detail(resp)
            repo.update_account(account_id, listen_status="error", listen_error=detail)
            account = repo.get_account(account_id)
            return {"ok": False, "listen": _local_listen_status(account, status="error", error=detail)}
        remote = resp.json() if resp.content else {}
        repo.update_account(
            account_id,
            listen_status="normal",
            listen_error="",
            listen_received_count=int((remote or {}).get("listened_count") or 0),
        )
        account = repo.get_account(account_id)
        return {
            "ok": True,
            "listen": _local_listen_status(account, status="normal"),
            "record": remote,
            "progress": _listen_progress(remote),
        }
    except requests.RequestException as exc:
        detail = str(exc)[:500]
        repo.update_account(account_id, listen_status="error", listen_error=detail)
        account = repo.get_account(account_id)
        return {"ok": False, "listen": _local_listen_status(account, status="error", error=detail)}

@router.delete("/leave/{account_id}")
def leave_listen(account_id: int) -> dict:
    account = repo.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    api_url = (repo.get_setting("listen_api_url", "") or account.get("listen_api_url") or "").strip()
    if not api_url:
        repo.update_account(
            account_id,
            listen_api_url="",
            listen_status="unconfigured",
            listen_error="",
            listen_item_id="",
            listen_received_count=0,
        )
        return {"ok": True}

    account_md5 = _account_md5(account["phone"])
    headers = {"X-API-Key": _hourly_apikey(account_md5)}
    try:
        resp = requests.delete(
            _server_url(api_url, f"/api/listen-records/{account_md5}"),
            headers=headers,
            timeout=10,
        )
        if resp.status_code not in (200, 204, 404):
            detail = _response_detail(resp)
            repo.update_account(account_id, listen_status="error", listen_error=detail)
            raise HTTPException(resp.status_code, f"退出听歌失败：{detail}")
        repo.update_account(
            account_id,
            listen_api_url="",
            listen_status="unconfigured",
            listen_error="",
            listen_item_id="",
            listen_received_count=0,
        )
        return {"ok": True}
    except HTTPException:
        raise
    except requests.RequestException as exc:
        detail = str(exc)[:500]
        repo.update_account(account_id, listen_status="error", listen_error=detail)
        raise HTTPException(502, f"连接听歌 API 失败：{exc}") from exc

@router.post("/sync")
def sync_listen_config() -> dict:
    """将全局听歌配置同步到所有已经加入听歌的账号。"""
    api_url = (repo.get_setting("listen_api_url", "") or "").strip()
    item_id = (repo.get_setting("listen_item_id", "") or "").strip()
    daily_limit = max(0, repo.get_setting_int("listen_daily_max", 1))
    monthly_limit = max(0, repo.get_setting_int("listen_monthly_max", 30))
    if not api_url or not item_id:
        return {"ok": True, "updated": 0, "failed": [], "message": "未配置完整听歌信息"}

    updated = 0
    failed: list[str] = []
    for account in repo.list_accounts():
        if account.get("listen_status") != "normal":
            continue
        account_id = int(account["id"])
        account_md5 = _account_md5(account["phone"])
        headers = {
            "X-API-Key": _hourly_apikey(account_md5),
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                _server_url(api_url, "/api/update"),
                json={
                    "account_md5": account_md5,
                    "netease_item_id": item_id,
                    "daily_listen_limit": daily_limit,
                    "monthly_listen_limit": monthly_limit,
                },
                headers=headers,
                timeout=10,
            )
            if not 200 <= resp.status_code < 300:
                detail = _response_detail(resp)
                repo.update_account(account_id, listen_status="error", listen_error=detail)
                failed.append(f"{account['phone']}: {detail}")
                continue
            remote = resp.json() if resp.content else {}
            repo.update_account(
                account_id,
                listen_api_url=api_url,
                listen_item_id=item_id,
                listen_status="normal",
                listen_error="",
                listen_received_count=int((remote or {}).get("listened_count") or 0),
            )
            updated += 1
        except requests.RequestException as exc:
            detail = str(exc)[:500]
            repo.update_account(account_id, listen_status="error", listen_error=detail)
            failed.append(f"{account['phone']}: {detail}")
    return {"ok": not failed, "updated": updated, "failed": failed}
