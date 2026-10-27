#!/usr/bin/env python3
"""fnOS Magpie 更新器：仅允许检查官方版本和执行固定升级流程。"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any

APP_NAME = "magpie-updater"
CONTAINER = "magpie"
IMAGE_REPOSITORY = "ghcr.io/yetone/magpie"
RELEASE_URL = "https://api.github.com/repos/yetone/magpie-releases/releases/latest"
CONFIG_SOURCE = "/vol1/@appdata/magpie/config"
CONFIG_TARGET = "/config"
BIND_IP = "192.168.1.3"
PORTS = (3425, 3430)
REQUIRED_ENV = ("MAGPIE_ADDR", "MAGPIE_PUBLIC_URL", "MAGPIE_WEB_KEY", "TZ")
LOCK = threading.Lock()
STATE_DIR = ""
CACHE_PATH = ""
CACHE_LOCK = threading.Lock()
UPGRADE_PROGRESS: dict[str, Any] | None = None


def now() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def run_docker(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/docker", *args], text=True, capture_output=True, timeout=timeout, check=False)


def docker_json(args: list[str]) -> Any:
    completed = run_docker(args)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "Docker 命令失败")
    return json.loads(completed.stdout)


def clean_version(value: str) -> str:
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)", value.strip())
    if not match:
        raise RuntimeError("官方 Release 返回了无法识别的版本号。")
    return match.group(1)


def get_latest() -> dict[str, str]:
    request = urllib.request.Request(RELEASE_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "fnos-magpie-updater/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法获取 Magpie 官方 Release：{exc}") from exc
    if data.get("prerelease") or data.get("draft"):
        raise RuntimeError("官方 latest Release 不是稳定版本。")
    return {"version": clean_version(str(data.get("tag_name", ""))), "publishedAt": str(data.get("published_at", "")), "notes": str(data.get("body", ""))[:4000]}


def inspect_container() -> dict[str, Any]:
    data = docker_json(["inspect", CONTAINER])
    if not isinstance(data, list) or len(data) != 1:
        raise RuntimeError("未找到唯一的 Magpie 容器。")
    return data[0]


def current_version(info: dict[str, Any]) -> str:
    image = str(info.get("Config", {}).get("Image", ""))
    prefix = IMAGE_REPOSITORY + ":"
    if not image.startswith(prefix):
        raise RuntimeError("现有 magpie 容器未使用受支持的官方 Magpie 镜像。")
    return clean_version(image.removeprefix(prefix))


def safe_container_spec(info: dict[str, Any]) -> dict[str, Any]:
    mounts = info.get("Mounts", [])
    matches = [m for m in mounts if m.get("Type") == "bind" and m.get("Source") == CONFIG_SOURCE and m.get("Destination") == CONFIG_TARGET]
    if len(matches) != 1:
        raise RuntimeError("现有 Magpie 容器的数据卷与受保护配置不匹配，已拒绝升级。")
    env_map: dict[str, str] = {}
    for item in info.get("Config", {}).get("Env", []):
        if "=" in item:
            key, value = item.split("=", 1)
            if key in REQUIRED_ENV:
                env_map[key] = value
    missing = [key for key in REQUIRED_ENV if key not in env_map]
    if missing:
        raise RuntimeError("现有 Magpie 容器缺少必要环境变量：" + ", ".join(missing))
    cmd = info.get("Config", {}).get("Cmd") or []
    if cmd != ["web", "--addr", "0.0.0.0:3430", "--no-open"]:
        raise RuntimeError("现有 Magpie 容器启动命令不在受支持范围，已拒绝升级。")
    ports = info.get("HostConfig", {}).get("PortBindings") or {}
    expected = {f"{port}/tcp": [{"HostIp": BIND_IP, "HostPort": str(port)}] for port in PORTS}
    if ports != expected:
        raise RuntimeError("现有 Magpie 容器端口绑定与受支持范围不匹配，已拒绝升级。")
    return {"env": env_map}


def cache_state(result: dict[str, Any]) -> None:
    temporary = CACHE_PATH + ".tmp"
    with CACHE_LOCK:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, CACHE_PATH)


def read_cached_state() -> dict[str, Any] | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def refresh_state() -> dict[str, Any]:
    result: dict[str, Any] = {"checkedAt": now(), "container": CONTAINER}
    try:
        info = inspect_container()
        current = current_version(info)
        latest = get_latest()
        result.update({
            "ok": True,
            "currentVersion": current,
            "latestVersion": latest["version"],
            "updateAvailable": current != latest["version"],
            "health": info.get("State", {}).get("Health", {}).get("Status") or info.get("State", {}).get("Status", "unknown"),
            "releasePublishedAt": latest["publishedAt"],
            "releaseNotes": latest["notes"],
        })
    except Exception as exc:  # 将内部错误限缩为用户可处理信息
        result.update({"ok": False, "error": str(exc)})
    cache_state(result)
    return result


def state() -> dict[str, Any]:
    return refresh_state()


def scheduled_refresh() -> None:
    while True:
        refresh_state()
        time.sleep(6 * 60 * 60)


def assert_admin(headers: Any) -> None:
    # fnOS 统一网关会注入此 Header；缺失时默认拒绝，避免独立访问越权。
    value = str(headers.get("X-Trim-Isadmin", "")).lower()
    if value not in {"true", "1"}:
        raise PermissionError("只有 fnOS 管理员可执行升级。")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection((BIND_IP, port), timeout=2):
            return True
    except OSError:
        return False


def wait_healthy(seconds: int = 75) -> tuple[bool, str]:
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            info = inspect_container()
            health = info.get("State", {}).get("Health", {}).get("Status") or info.get("State", {}).get("Status", "unknown")
            last = str(health)
            if health == "healthy" and all(port_open(port) for port in PORTS):
                return True, health
            if health in {"exited", "dead"}:
                return False, health
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    return False, last or "健康检查超时"


def rollback(old_name: str) -> None:
    run_docker(["rm", "-f", CONTAINER], timeout=30)
    renamed = run_docker(["rename", old_name, CONTAINER], timeout=30)
    if renamed.returncode:
        raise RuntimeError("新容器失败且旧容器无法恢复：" + (renamed.stderr or renamed.stdout).strip())
    restarted = run_docker(["start", CONTAINER], timeout=60)
    if restarted.returncode:
        raise RuntimeError("旧容器已恢复名称，但启动失败：" + (restarted.stderr or restarted.stdout).strip())


def upgrade() -> dict[str, Any]:
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("已有升级任务正在运行，请等待其结束。")
    global UPGRADE_PROGRESS
    steps: list[dict[str, Any]] = []

    def report(step: str, detail: str = "", status: str = "running") -> None:
        entry = {"step": step, "detail": detail, "status": status, "at": now()}
        steps.append(entry)
        UPGRADE_PROGRESS = {"running": status == "running", "steps": steps, "current": entry}
        print(f"[upgrade] {step} {detail}", flush=True)

    try:
        report("校验容器结构", "读取当前容器配置")
        info = inspect_container()
        before = current_version(info)
        report("查询官方版本", "请求 GitHub Releases")
        latest = get_latest()
        target = latest["version"]
        if before == target:
            report("无需升级", f"当前 {before} 已是最新", "done")
            UPGRADE_PROGRESS = {"running": False, "steps": steps, "current": None}
            return {"ok": True, "changed": False, "message": "当前已是官方最新稳定版本。", "version": before}
        report("版本确认", f"{before} → {target}")
        spec = safe_container_spec(info)
        os.makedirs(os.path.join(STATE_DIR, "backups"), mode=0o700, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(STATE_DIR, "backups", f"magpie-inspect-{stamp}.json")
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False, indent=2)
        os.chmod(backup_path, 0o600)
        report("备份容器配置", os.path.basename(backup_path), "done")

        report("拉取官方镜像", f"{IMAGE_REPOSITORY}:{target}（可能需要几分钟）")
        pulled = run_docker(["pull", f"{IMAGE_REPOSITORY}:{target}"], timeout=900)
        if pulled.returncode:
            raise RuntimeError("拉取官方镜像失败：" + (pulled.stderr or pulled.stdout).strip())
        report("镜像拉取完成", target[:12], "done")
        old_name = f"magpie-before-{before.replace('.', '-')}-{stamp}"
        report("停止旧容器", CONTAINER)
        stopped = run_docker(["stop", CONTAINER], timeout=90)
        if stopped.returncode:
            raise RuntimeError("停止旧容器失败：" + (stopped.stderr or stopped.stdout).strip())
        renamed = run_docker(["rename", CONTAINER, old_name], timeout=30)
        if renamed.returncode:
            run_docker(["start", CONTAINER], timeout=60)
            raise RuntimeError("保留旧容器失败，已尝试恢复：" + (renamed.stderr or renamed.stdout).strip())
        report("旧容器已保留", old_name, "done")

        command = ["run", "-d", "--name", CONTAINER, "--restart", "unless-stopped"]
        for port in PORTS:
            command += ["-p", f"{BIND_IP}:{port}:{port}"]
        command += ["-v", f"{CONFIG_SOURCE}:{CONFIG_TARGET}"]
        for key in REQUIRED_ENV:
            command += ["-e", f"{key}={spec['env'][key]}"]
        command += [f"{IMAGE_REPOSITORY}:{target}", "web", "--addr", "0.0.0.0:3430", "--no-open"]
        report("创建新容器", f"镜像 {target}")
        created = run_docker(command, timeout=120)
        if created.returncode:
            report("创建失败，回滚中", "恢复旧容器", "error")
            rollback(old_name)
            raise RuntimeError("新容器创建失败，已回滚旧容器：" + (created.stderr or created.stdout).strip())
        report("等待健康检查", "最长 75 秒")
        healthy, detail = wait_healthy()
        if not healthy:
            report("健康检查未通过，回滚中", detail, "error")
            rollback(old_name)
            raise RuntimeError(f"新容器未通过健康检查（{detail}），已回滚旧容器。")
        report("新容器健康", f"Magpie {target} 运行中", "done")
        removed = run_docker(["rm", "-f", old_name], timeout=60)
        if removed.returncode:
            # 新版本已成功；旧容器残留不会影响运行，作为可见警告返回。
            warning = "新版本已成功启动，但旧容器未清理：" + (removed.stderr or removed.stdout).strip()
        else:
            warning = ""
        report("清理旧容器", old_name, "done")
        UPGRADE_PROGRESS = {"running": False, "steps": steps, "current": None}
        return {"ok": True, "changed": True, "from": before, "to": target, "backup": os.path.basename(backup_path), "warning": warning}
    except Exception:
        if UPGRADE_PROGRESS is not None and UPGRADE_PROGRESS.get("running"):
            UPGRADE_PROGRESS = {**UPGRADE_PROGRESS, "running": False}
        raise
    finally:
        LOCK.release()


PAGE = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Magpie 更新器</title><style>body{margin:0;background:#f5f7fb;color:#182230;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif}.wrap{max-width:760px;margin:36px auto;padding:0 18px}.card{background:white;border-radius:16px;padding:26px;box-shadow:0 6px 24px #21304d14}h1{margin:0 0 6px;font-size:25px}.muted{color:#64748b}.versions{display:flex;gap:12px;margin:22px 0}.version{flex:1;border:1px solid #e2e8f0;border-radius:12px;padding:14px}.version b{display:block;font-size:20px;margin-top:6px}.update{border-color:#ef4444;background:#fff5f5}.badge{display:inline-block;background:#dc2626;color:#fff;border-radius:99px;padding:3px 9px;font-size:12px;margin-left:8px}button{border:0;border-radius:9px;padding:11px 16px;font-size:15px;cursor:pointer;background:#2563eb;color:white}button:disabled{opacity:.5;cursor:wait}.secondary{background:#e2e8f0;color:#1e293b}.notes{white-space:pre-wrap;background:#f8fafc;padding:12px;border-radius:8px;max-height:190px;overflow:auto}.error{color:#b91c1c}.ok{color:#047857}.pstep{padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:14px}.pstep.done{color:#047857}.pstep.error{color:#b91c1c}.pstep.running{color:#1d4ed8}</style><main class='wrap'><section class='card'><h1>Magpie 更新器 <span id='badge'></span></h1><p class='muted'>只检查 Magpie 官方稳定版。升级前需要管理员明确确认；更新器不会执行任意命令。</p><div class='versions'><div class='version'><span class='muted'>当前版本</span><b id='current'>正在读取…</b></div><div class='version' id='latestBox'><span class='muted'>官方最新稳定版</span><b id='latest'>—</b></div></div><p id='status' class='muted'></p><div><button class='secondary' onclick='loadState()'>重新检查</button> <button id='upgrade' onclick='confirmUpgrade()' disabled>立即升级</button></div><h3>发行说明</h3><div class='notes' id='notes'>—</div><div id='progress' style='display:none'></div></section></main><script>const API='/app/magpie-updater/api';let data=null;const q=s=>document.querySelector(s);async function jfetch(url,opts){const r=await fetch(url,opts);const text=await r.text();let out=null;try{out=JSON.parse(text)}catch(e){throw Error(text.trim().slice(0,120)||('HTTP '+r.status))}return out}async function loadState(){q('#status').textContent='正在检查官方 Release…';q('#upgrade').disabled=true;try{const r=await jfetch(API+'/status');data=r;if(!data.ok)throw Error(data.error);q('#current').textContent='v'+data.currentVersion;q('#latest').textContent='v'+data.latestVersion;q('#notes').textContent=data.releaseNotes||'官方 Release 未提供说明。';const newer=data.updateAvailable;if(newer){q('#badge').textContent='1 个可用更新';q('#badge').className='badge';q('#latestBox').className='version update';q('#status').textContent='发现可升级版本。点击“立即升级”后会再次获取官方版本并执行受限升级流程。';q('#upgrade').disabled=false}else{q('#badge').textContent='已是最新';q('#badge').className='';q('#latestBox').className='version';q('#status').textContent='当前 Magpie 已是官方最新稳定版，容器健康状态：'+data.health+'。'}}catch(e){q('#status').textContent='检查失败：'+e.message;q('#status').className='error'}}async function renderProgress(p){const box=q('#progress');if(!box)return;if(!p||!p.steps||!p.steps.length){box.style.display='none';box.innerHTML='';return}box.style.display='';box.innerHTML='<h3 style="margin:18px 0 8px">升级进度</h3>'+p.steps.map(s=>{const icon=s.status==='done'?'✅':(s.status==='error'?'❌':'⏳');return '<div class="pstep '+s.status+'">'+icon+' <b>'+s.step+'</b>'+(s.detail?' — '+s.detail:'')+'</div>'}).join('')}async function pollProgress(){try{const p=await jfetch(API+'/progress');await renderProgress(p);if(p&&p.running){setTimeout(pollProgress,1500)}}catch(e){}}async function confirmUpgrade(){if(!data||!data.updateAvailable)return;if(!confirm('确认将 Magpie 从 v'+data.currentVersion+' 升级到官方最新稳定版？服务会短暂重启；若健康检查失败，更新器会自动回滚。'))return;q('#upgrade').disabled=true;q('#status').textContent='正在升级，进度见下方实时列表…';pollProgress();try{const r=await jfetch(API+'/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error(r.error);q('#status').textContent=r.changed?'升级完成：v'+r.from+' → v'+r.to+(r.warning?'；'+r.warning:'') : r.message;q('#status').className='ok';await pollProgress();await loadState()}catch(e){q('#status').textContent='升级失败：'+e.message;q('#status').className='error';q('#upgrade').disabled=false;await pollProgress()}}loadState();</script></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MagpieUpdater/1.0"

    def log_message(self, *_: Any) -> None:
        return

    def send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/api/status"):
            self.send_json(HTTPStatus.OK, state())
            return
        if path.endswith("/api/progress"):
            self.send_json(HTTPStatus.OK, UPGRADE_PROGRESS or {"running": False, "steps": [], "current": None})
            return
        if path in {"", "/app/magpie-updater"} or path.endswith("/app/magpie-updater"):
            content = PAGE.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未找到资源。"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if not path.endswith("/api/upgrade"):
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未找到资源。"})
            return
        try:
            assert_admin(self.headers)
            self.send_json(HTTPStatus.OK, upgrade())
        except PermissionError as exc:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def main() -> None:
    global STATE_DIR, CACHE_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    STATE_DIR = os.path.realpath(args.state_dir)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    CACHE_PATH = os.path.join(STATE_DIR, "update-status.json")
    threading.Thread(target=scheduled_refresh, daemon=True).start()
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    class Server(ThreadingMixIn, UnixStreamServer):
        daemon_threads = True
    with Server(args.socket, Handler) as httpd:
        os.chmod(args.socket, 0o660)
        httpd.serve_forever()

if __name__ == "__main__":
    main()
