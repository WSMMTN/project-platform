"""项目展示平台 —— FastAPI + Session 登录 + Cloudflare R2 浏览器直传。

路由一览：
- 页面：/register /login /logout /projects /projects/new /projects/{pid}
- 直传 API：POST /api/projects/{pid}/upload-url -> 浏览器 PUT R2 -> POST .../files
- 文件删除：DELETE /api/files/{fid}
- 分享：POST /projects/{pid}/toggle-share /regen-share /delete；GET /shared/{share_uuid}
- 健康检查：GET /healthz
"""
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid as uuidlib
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from database import Project, ProjectFile, User, create_db, get_db
from r2_client import (
    PRESIGN_PUT_EXPIRES,
    delete_object,
    head_object,
    presign_get_url,
    presign_put_url,
    r2_configured,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ALLOWED_EXT = {"mp4": "video", "ppt": "ppt", "pptx": "ppt", "pdf": "pdf"}
MAX_FILE_SIZE = 2 * 1024**3  # 2GB
USERNAME_RE = re.compile(r"^[\w.\-]{2,32}$", re.UNICODE)

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    # 本地首跑自动生成并写回 .env；Render 上由 render.yaml 的 generateValue 提供
    SECRET_KEY = secrets.token_hex(32)
    try:
        env_path = BASE_DIR / ".env"
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        env_path.write_text(existing + f'SECRET_KEY="{SECRET_KEY}"\n', encoding="utf-8")
        print("[warn] SECRET_KEY 未设置，已自动生成并写入 .env")
    except OSError:
        print("[warn] SECRET_KEY 未设置，使用临时密钥，重启后登录态会失效")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_db()
    yield


app = FastAPI(title="Project Platform", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------- 工具函数 ----------

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, hex_dk = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
    return hmac.compare_digest(dk.hex(), hex_dk)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def site_base_url(request: Request) -> str:
    env_base = os.getenv("SITE_BASE_URL", "")
    if env_base:
        return env_base.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


_login_attempts: dict = {}
RATE_LIMIT_WINDOW = 300  # 秒
RATE_LIMIT_MAX = 10


def login_rate_ok(ip: str) -> bool:
    """同一 IP 5 分钟内最多 10 次登录尝试。"""
    now = time.monotonic()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        _login_attempts[ip] = attempts
        return False
    attempts.append(now)
    _login_attempts[ip] = attempts
    return True


# ---------- 依赖 ----------

def current_user(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def require_user(user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_user_api(user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def own_project(db: Session, user: User, pid: int) -> Project:
    p = db.query(Project).filter(Project.id == pid, Project.user_id == user.id).first()
    if p is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return p


# ---------- 基础路由 ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index(user=Depends(current_user)):
    return RedirectResponse("/projects" if user else "/login", status_code=303)


# ---------- 注册 / 登录 / 退出 ----------

@app.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/projects", status_code=303)
    return templates.TemplateResponse(request, "register.html", {})


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    error = None
    if not USERNAME_RE.fullmatch(username):
        error = "用户名需 2~32 位，仅限中文、字母、数字、点、横线、下划线"
    elif len(password) < 6:
        error = "密码至少 6 位"
    elif db.query(User).filter(User.username == username).first():
        error = "用户名已存在"
    if error:
        return templates.TemplateResponse(
            request, "register.html", {"error": error, "username": username}, status_code=400
        )
    user = User(username=username, hashed_password=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["uid"] = user.id
    return RedirectResponse("/projects", status_code=303)


@app.get("/login")
def login_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/projects", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    if not login_rate_ok(ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": "尝试过于频繁，请 5 分钟后再试"}, status_code=429
        )
    user = db.query(User).filter(User.username == username.strip()).first()
    if user is None or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "用户名或密码错误"}, status_code=400
        )
    _login_attempts.pop(ip, None)
    request.session["uid"] = user.id
    return RedirectResponse("/projects", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- 项目管理（需登录） ----------

@app.get("/projects")
def projects_page(
    request: Request, user=Depends(require_user), db: Session = Depends(get_db)
):
    items = (
        db.query(Project)
        .filter(Project.user_id == user.id)
        .order_by(Project.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "project_list.html", {"user": user, "projects": items}
    )


@app.get("/projects/new")
def new_project_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(request, "project_new.html", {"user": user})


@app.post("/projects/new")
def create_project(
    user=Depends(require_user),
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(""),
    tech_stack: str = Form(""),
):
    p = Project(
        title=title.strip()[:200] or "未命名项目",
        description=description.strip(),
        tech_stack=tech_stack.strip()[:500],
        share_uuid=uuidlib.uuid4().hex,
        user_id=user.id,
    )
    db.add(p)
    db.commit()
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@app.get("/projects/{pid}")
def project_page(
    pid: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    p = own_project(db, user, pid)
    share_url = f"{site_base_url(request)}/shared/{p.share_uuid}"
    return templates.TemplateResponse(
        request,
        "project_edit.html",
        {"user": user, "p": p, "share_url": share_url, "r2_ready": r2_configured()},
    )


@app.post("/projects/{pid}/edit")
def edit_project(
    pid: int,
    user=Depends(require_user),
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(""),
    tech_stack: str = Form(""),
):
    p = own_project(db, user, pid)
    p.title = title.strip()[:200] or p.title
    p.description = description.strip()
    p.tech_stack = tech_stack.strip()[:500]
    db.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/toggle-share")
def toggle_share(pid: int, user=Depends(require_user), db: Session = Depends(get_db)):
    p = own_project(db, user, pid)
    p.share_enabled = not p.share_enabled
    db.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/regen-share")
def regen_share(pid: int, user=Depends(require_user), db: Session = Depends(get_db)):
    p = own_project(db, user, pid)
    p.share_uuid = uuidlib.uuid4().hex
    db.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/delete")
def delete_project(pid: int, user=Depends(require_user), db: Session = Depends(get_db)):
    p = own_project(db, user, pid)
    for f in p.files:
        delete_object(f.r2_key)
    db.delete(p)  # 级联删除文件记录
    db.commit()
    return RedirectResponse("/projects", status_code=303)


# ---------- 浏览器直传 API ----------

class UploadUrlReq(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0


class ConfirmReq(BaseModel):
    key: str
    filename: str


def _ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


@app.post("/api/projects/{pid}/upload-url")
def api_upload_url(
    pid: int,
    body: UploadUrlReq,
    user=Depends(require_user_api),
    db: Session = Depends(get_db),
):
    p = own_project(db, user, pid)
    if not r2_configured():
        raise HTTPException(status_code=500, detail="对象存储未配置，请检查 R2 环境变量")
    ext = _ext_of(body.filename)
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="仅支持 mp4 / ppt / pptx / pdf")
    if body.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="文件超过 2GB 限制")
    key = f"user_{user.id}/project_{p.id}/{uuidlib.uuid4().hex}.{ext}"
    return {
        "upload_url": presign_put_url(key, body.content_type),
        "key": key,
        "expires_in": PRESIGN_PUT_EXPIRES,
    }


@app.post("/api/projects/{pid}/files")
def api_confirm_file(
    pid: int,
    body: ConfirmReq,
    user=Depends(require_user_api),
    db: Session = Depends(get_db),
):
    p = own_project(db, user, pid)
    prefix = f"user_{user.id}/project_{p.id}/"
    ext = _ext_of(body.key)
    if not body.key.startswith(prefix) or ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    info = head_object(body.key)  # 到 R2 核实文件确实上传成功
    if info is None:
        raise HTTPException(status_code=400, detail="R2 中未找到该文件，请重新上传")
    f = ProjectFile(
        file_name=body.filename.strip()[:255] or body.key.rsplit("/", 1)[-1],
        file_type=ALLOWED_EXT[ext],
        r2_key=body.key,
        size=info["size"],
        content_type=info["content_type"],
        project_id=p.id,
    )
    db.add(f)
    db.commit()
    return {"id": f.id, "file_name": f.file_name, "file_type": f.file_type, "size": f.size}


@app.delete("/api/files/{fid}")
def api_delete_file(fid: int, user=Depends(require_user_api), db: Session = Depends(get_db)):
    f = db.get(ProjectFile, fid)
    if f is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    p = db.get(Project, f.project_id)
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="文件不存在")
    delete_object(f.r2_key)
    db.delete(f)
    db.commit()
    return {"ok": True}


# ---------- 公开分享页（访客无需登录） ----------

@app.get("/shared/{share_uuid}", response_class=HTMLResponse)
def shared_view(share_uuid: str, request: Request, db: Session = Depends(get_db)):
    p = (
        db.query(Project)
        .filter(Project.share_uuid == share_uuid, Project.share_enabled.is_(True))
        .first()
    )
    if p is None:
        raise HTTPException(status_code=404, detail="链接不存在或分享已关闭")
    if p.files and not r2_configured():
        raise HTTPException(status_code=500, detail="对象存储未配置")
    files = [
        {
            "obj": f,
            "view_url": presign_get_url(f.r2_key),  # 内联：视频播放 / PDF 预览
            "download_url": presign_get_url(f.r2_key, f.file_name),  # 强制下载
        }
        for f in p.files
    ]
    return templates.TemplateResponse(request, "shared_view.html", {"project": p, "files": files})
