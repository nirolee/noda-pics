import os
import uuid
import time
import json
import hmac
import hashlib
import bcrypt
import jwt
import requests
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, redirect, session
from flask_cors import CORS
import pymysql
import pymysql.cursors

try:
    import boto3
except ImportError:
    boto3 = None

app = Flask(__name__, static_folder="../frontend", static_url_path="")
app.secret_key = os.getenv("SECRET_KEY", "noda-secret-change-me")

CORS(app, resources={r"/api/*": {"origins": [
    "https://noda.pics", "https://www.noda.pics",
    "http://localhost:3000", "http://127.0.0.1:3000", "null",
]}}, supports_credentials=True)

# ─── 配置 ───
DB_HOST     = os.getenv("DB_HOST",     "127.0.0.1")
DB_PORT     = int(os.getenv("DB_PORT", "3306"))
DB_USER     = os.getenv("DB_USER",     "noda_pics")
DB_PASS     = os.getenv("DB_PASS",     "")
DB_NAME     = os.getenv("DB_NAME",     "noda_pics")
JWT_SECRET  = os.getenv("JWT_SECRET",  "noda-jwt-secret-change-me")
JWT_EXPIRE  = int(os.getenv("JWT_EXPIRE", str(60 * 24 * 30)))  # 30天（分钟）

RATE_GUEST  = int(os.getenv("RATE_GUEST",  "3"))    # 游客每天限额
RATE_USER   = int(os.getenv("RATE_USER",   "10"))   # 免费用户每天限额
RATE_PRO    = int(os.getenv("RATE_PRO",    "100"))  # Pro 用户每天限额
QUEUE_MAX   = int(os.getenv("QUEUE_MAX",   "20"))

# 合法的生成模式
VALID_MODES = {"txt2img", "pulid", "ccdb"}
# character_pack 一次最多多少张（防滥用）
BATCH_MAX_PROMPTS = int(os.getenv("BATCH_MAX_PROMPTS", "20"))

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID",     "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID",     "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
OAUTH_REDIRECT_BASE  = os.getenv("OAUTH_REDIRECT_BASE",  "https://noda-pics.onrender.com")

CREEM_API_KEY     = os.getenv("CREEM_API_KEY",      "")
CREEM_WEBHOOK_SEC = os.getenv("CREEM_WEBHOOK_SECRET","")
CREEM_PRODUCT_PRO = os.getenv("CREEM_PRODUCT_PRO",  "")
CREEM_TEST_MODE   = os.getenv("CREEM_TEST_MODE",     "true").lower() == "true"
CREEM_BASE        = "https://test-api.creem.io" if CREEM_TEST_MODE else "https://api.creem.io"

# Credit packs（由用户在 Creem dashboard 创建 one-time 产品后，把 product_id 填到 env）
# 每个 pack 由 product_id + 赠送 credit 数量组成
CREDIT_PACKS = {
    "small":  {"product_id": os.getenv("CREEM_PRODUCT_CREDITS_SMALL",  ""),
               "credits": int(os.getenv("CREDITS_SMALL_AMOUNT",  "60"))},
    "medium": {"product_id": os.getenv("CREEM_PRODUCT_CREDITS_MEDIUM", ""),
               "credits": int(os.getenv("CREDITS_MEDIUM_AMOUNT", "250"))},
    "large":  {"product_id": os.getenv("CREEM_PRODUCT_CREDITS_LARGE",  ""),
               "credits": int(os.getenv("CREDITS_LARGE_AMOUNT",  "500"))},
}

# Pro 订阅附送 credits：$4.99/月 → 200 credits，累积不清零
PRO_MONTHLY_CREDITS = int(os.getenv("PRO_MONTHLY_CREDITS", "200"))

# Cloudflare R2（参考图上传用）
R2_ENDPOINT    = os.getenv("R2_ENDPOINT",   "")
R2_ACCESS_KEY  = os.getenv("R2_ACCESS_KEY", "")
R2_SECRET_KEY  = os.getenv("R2_SECRET_KEY", "")
R2_BUCKET      = os.getenv("R2_BUCKET",     "noda-pics")
IMG_BASE       = os.getenv("IMG_BASE",      "https://img.noda.pics")
UPLOAD_MAX_MB  = int(os.getenv("UPLOAD_MAX_MB", "8"))
# 参考图在 R2 保留时长（小时）；poller 已有 48h 清理，这里只影响新上传的 key 前缀
REF_TTL_HOURS  = int(os.getenv("REF_TTL_HOURS", "72"))


def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS, database=DB_NAME,
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5, autocommit=True,
    )


# ─── JWT 工具 ───
def make_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),   # PyJWT 2.x 要求 sub 为字符串
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])   # 转回 int
    except Exception:
        return None


def current_user() -> dict | None:
    """从 Authorization header 或 cookie 取当前用户"""
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        candidate = auth[7:]
        if decode_token(candidate):   # 只有能解码才用，否则回退到 cookie
            token = candidate
    if not token:
        token = request.cookies.get("token")
    if not token:
        return None
    user_id = decode_token(token)
    if not user_id:
        return None
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            return cur.fetchone()


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        request.user = user
        return f(*args, **kwargs)
    return wrapper


# ─── 限流（游客按IP/用户按user_id，每天重置）───
def is_pro_active(user: dict) -> bool:
    if user["plan"] != "pro":
        return False
    exp = user.get("plan_expires_at")
    if exp is None:
        return True  # 没有过期时间视为永久（手动赠送）
    if isinstance(exp, str):
        exp = datetime.fromisoformat(exp)
    return exp > datetime.now()


def _ledger_insert(cur, user_id: int, delta: int, balance_after: int,
                   reason: str, ref_id: str | None):
    cur.execute(
        "INSERT INTO credit_ledger (user_id, delta, balance_after, reason, ref_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, delta, balance_after, reason, ref_id)
    )


def add_credits(user_id: int, amount: int, reason: str, ref_id: str | None = None) -> int:
    """给用户加 credits，返回新余额。amount 可正可负。"""
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE users SET credits_balance = credits_balance + %s WHERE id = %s",
                (amount, user_id)
            )
            cur.execute("SELECT credits_balance FROM users WHERE id = %s", (user_id,))
            new_balance = cur.fetchone()["credits_balance"]
            _ledger_insert(cur, user_id, amount, new_balance, reason, ref_id)
            return new_balance


def spend_credits_atomic(user_id: int, amount: int, reason: str, ref_id: str | None) -> int | None:
    """原子扣 amount credits，余额不足返回 None，成功返回新余额。"""
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE users SET credits_balance = credits_balance - %s "
                "WHERE id = %s AND credits_balance >= %s",
                (amount, user_id, amount)
            )
            if cur.rowcount == 0:
                return None
            cur.execute("SELECT credits_balance FROM users WHERE id = %s", (user_id,))
            new_balance = cur.fetchone()["credits_balance"]
            _ledger_insert(cur, user_id, -amount, new_balance, reason, ref_id)
            return new_balance


def check_quota(ip: str, user: dict | None) -> tuple[bool, int, int]:
    """返回 (allowed, used, limit)"""
    with get_db() as db:
        with db.cursor() as cur:
            if user:
                limit = RATE_PRO if is_pro_active(user) else RATE_USER
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE user_id = %s "
                    "AND created_at >= CURDATE()",
                    (user["id"],)
                )
            else:
                limit = RATE_GUEST
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM jobs WHERE ip = %s "
                    "AND user_id IS NULL AND created_at >= CURDATE()",
                    (ip,)
                )
            used = cur.fetchone()["cnt"]
    return used < limit, used, limit


# ═══════════════════════════════════════════
# 用户认证接口
# ═══════════════════════════════════════════

@app.post("/api/auth/register")
def register():
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    name  = (body.get("name")  or "").strip()
    pwd   = (body.get("password") or "").strip()

    if not email or not pwd:
        return jsonify({"error": "email and password required"}), 400
    if len(pwd) < 6:
        return jsonify({"error": "password too short"}), 400

    pw_hash = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
    try:
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s)",
                    (email, name or email.split("@")[0], pw_hash)
                )
                user_id = cur.lastrowid
    except pymysql.err.IntegrityError:
        return jsonify({"error": "email already registered"}), 409

    token = make_token(user_id)
    return jsonify({"token": token, "user": {"id": user_id, "email": email, "name": name}}), 201


@app.post("/api/auth/login")
def login():
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pwd   = (body.get("password") or "").strip()

    if not email or not pwd:
        return jsonify({"error": "email and password required"}), 400

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cur.fetchone()

    if not user or not user["password_hash"]:
        return jsonify({"error": "invalid credentials"}), 401
    if not bcrypt.checkpw(pwd.encode(), user["password_hash"].encode()):
        return jsonify({"error": "invalid credentials"}), 401

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user["id"],))

    token = make_token(user["id"])
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "email": user["email"],
                 "name": user["name"], "avatar_url": user["avatar_url"]},
    })


@app.get("/api/auth/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": {
        "id":             user["id"],
        "email":          user["email"],
        "name":           user["name"],
        "avatar_url":     user["avatar_url"],
        "plan":           user["plan"],
        "plan_active":    is_pro_active(user) if user["plan"] == "pro" else False,
        "plan_expires_at": str(user["plan_expires_at"]) if user.get("plan_expires_at") else None,
        "credits_balance": int(user.get("credits_balance") or 0),
    }})


# ─── Google OAuth ───
@app.get("/api/auth/google")
def google_login():
    redirect_uri = f"{OAUTH_REDIRECT_BASE}/api/auth/google/callback"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&response_type=code"
        "&scope=openid email profile"
    )
    return redirect(url)


@app.get("/api/auth/google/callback")
def google_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?error=oauth_failed")

    redirect_uri = f"{OAUTH_REDIRECT_BASE}/api/auth/google/callback"
    # 换 token
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    })
    if not r.ok:
        return redirect("/?error=oauth_failed")

    id_token_str = r.json().get("id_token", "")
    # 解 Google id_token（不验签，仅取 payload）
    parts   = id_token_str.split(".")
    payload = json.loads(__import__("base64").b64decode(parts[1] + "=="))
    email   = payload.get("email", "")
    name    = payload.get("name", "")
    avatar  = payload.get("picture", "")
    g_id    = payload.get("sub", "")

    token = _upsert_oauth_user(email, name, avatar, "google", g_id)
    resp  = redirect("/?login=ok")
    resp.set_cookie("token", token, max_age=60 * 60 * 24 * 30,
                    secure=True, httponly=True, samesite="Lax")
    return resp


# ─── GitHub OAuth ───
@app.get("/api/auth/github")
def github_login():
    redirect_uri = f"{OAUTH_REDIRECT_BASE}/api/auth/github/callback"
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=user:email"
    )
    return redirect(url)


@app.get("/api/auth/github/callback")
def github_callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?error=oauth_failed")

    r = requests.post("https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
        }
    )
    access_token = r.json().get("access_token", "")
    if not access_token:
        return redirect("/?error=oauth_failed")

    user_info = requests.get("https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}"}
    ).json()

    email = user_info.get("email") or ""
    if not email:
        emails = requests.get("https://api.github.com/user/emails",
            headers={"Authorization": f"Bearer {access_token}"}
        ).json()
        primary = next((e for e in emails if e.get("primary")), None)
        email = primary["email"] if primary else ""

    name   = user_info.get("name") or user_info.get("login", "")
    avatar = user_info.get("avatar_url", "")
    gh_id  = str(user_info.get("id", ""))

    token = _upsert_oauth_user(email, name, avatar, "github", gh_id)
    resp  = redirect("/?login=ok")
    resp.set_cookie("token", token, max_age=60 * 60 * 24 * 30,
                    secure=True, httponly=True, samesite="Lax")
    return resp


def _upsert_oauth_user(email, name, avatar, provider, provider_id) -> str:
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE users SET name=%s, avatar_url=%s, provider=%s, "
                    "provider_id=%s, last_login=NOW() WHERE id=%s",
                    (name, avatar, provider, provider_id, row["id"])
                )
                return make_token(row["id"])
            else:
                cur.execute(
                    "INSERT INTO users (email, name, avatar_url, provider, provider_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (email, name, avatar, provider, provider_id)
                )
                return make_token(cur.lastrowid)


# ═══════════════════════════════════════════
# 生图接口（更新限流逻辑）
# ═══════════════════════════════════════════

@app.post("/api/jobs")
def submit_job():
    ip   = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    user = current_user()

    # 游客仍走 IP 每日配额；已登录用户走 credits 系统（下面扣费）
    if not user:
        allowed, used, limit = check_quota(ip, None)
        if not allowed:
            return jsonify({
                "error": "quota_exceeded",
                "message": f"游客每天限 {RATE_GUEST} 次，注册后可获得 {RATE_USER} 免费 credits",
                "used": used, "limit": limit,
            }), 429
    else:
        # 已登录：credits 必须 ≥ 1
        if int(user.get("credits_balance") or 0) < 1:
            return jsonify({
                "error": "insufficient_credits",
                "message": "Credits 不足，请购买",
                "balance": int(user.get("credits_balance") or 0),
            }), 402

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
            if cur.fetchone()["cnt"] >= QUEUE_MAX:
                return jsonify({"error": "queue_full", "message": "队列已满，请稍后再试"}), 503

    body    = request.get_json(silent=True) or {}
    prompt  = (body.get("prompt") or "").strip()
    style   = (body.get("style")  or "default").strip()
    ref_url = (body.get("reference_image_url") or "").strip() or None
    mode    = (body.get("mode") or "").strip().lower() or None

    if not prompt:
        return jsonify({"error": "missing_prompt"}), 400
    if len(prompt) > 500:
        return jsonify({"error": "prompt_too_long"}), 400
    if ref_url and not (ref_url.startswith("http://") or ref_url.startswith("https://")):
        return jsonify({"error": "invalid_reference_url"}), 400
    if ref_url and len(ref_url) > 500:
        return jsonify({"error": "reference_url_too_long"}), 400
    if mode and mode not in VALID_MODES:
        return jsonify({"error": "invalid_mode", "valid": list(VALID_MODES)}), 400

    # 默认 mode：有 ref → pulid；无 ref → txt2img（保持旧行为兼容）
    if mode is None:
        mode = "pulid" if ref_url else "txt2img"
    if mode in ("pulid", "ccdb") and not ref_url:
        return jsonify({"error": "mode_requires_reference_image"}), 400

    job_id  = str(uuid.uuid4())
    user_id = user["id"] if user else None

    # 已登录用户先原子扣 1 credit，再入队（任一失败不计费）
    new_balance = None
    if user:
        new_balance = spend_credits_atomic(user_id, 1, "job_spend", job_id)
        if new_balance is None:
            return jsonify({"error": "insufficient_credits"}), 402

    try:
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (id, prompt, style, reference_image_url, mode, status, ip, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)",
                    (job_id, prompt, style, ref_url, mode, ip, user_id)
                )
                cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
                queue_len = cur.fetchone()["cnt"]
    except Exception:
        # 入队失败就退还 credit
        if user:
            add_credits(user_id, 1, "refund", job_id)
        raise

    resp = {"job_id": job_id, "status": "pending", "queue_pos": queue_len, "mode": mode}
    if user:
        resp["credits_balance"] = new_balance
    else:
        resp["quota"] = {"used": used + 1, "limit": limit}
    return jsonify(resp), 201


@app.post("/api/uploads")
@require_auth
def upload_file():
    """用户上传参考图 → R2 → 返回可访问 URL

    限制：单文件 ≤ UPLOAD_MAX_MB（默认 8MB），仅 image/* mimetype
    认证：必须登录（避免被滥用消耗带宽）
    """
    if boto3 is None:
        return jsonify({"error": "upload_not_configured", "message": "server missing boto3"}), 503
    if not R2_ENDPOINT or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return jsonify({"error": "upload_not_configured", "message": "R2 not configured"}), 503

    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "missing_file"}), 400
    mimetype = (f.mimetype or "").lower()
    if not mimetype.startswith("image/"):
        return jsonify({"error": "invalid_mimetype", "got": mimetype}), 400

    # 大小校验
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > UPLOAD_MAX_MB * 1024 * 1024:
        return jsonify({"error": "file_too_large", "max_mb": UPLOAD_MAX_MB}), 413
    if size < 200:
        return jsonify({"error": "file_too_small"}), 400

    # 存到 refs/ 子目录（便于单独清理）
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mimetype, "png")
    key = f"refs/{request.user['id']}_{uuid.uuid4().hex}.{ext}"

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )
    try:
        s3.upload_fileobj(f.stream, R2_BUCKET, key,
                          ExtraArgs={"ContentType": mimetype, "CacheControl": "public, max-age=86400"})
    except Exception as e:
        app.logger.error(f"[upload] R2 error: {e}")
        return jsonify({"error": "upload_failed"}), 500

    return jsonify({
        "url": f"{IMG_BASE}/{key}",
        "size": size,
        "key": key,
    }), 201


@app.post("/api/jobs/batch")
@require_auth
def submit_batch_job():
    """Character Pack：1 张参考图 + N 条 prompt → 生成 N 张一致性图片

    Body: {
      "reference_image_url": "https://...",
      "mode": "ccdb",  // 可选，默认 ccdb
      "prompts": ["prompt1", "prompt2", ...]  // 每条一张图
    }
    返回 batch_id + job_ids[]，前端轮询 GET /api/batches/<id>
    """
    ip   = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    user = request.user

    body    = request.get_json(silent=True) or {}
    ref_url = (body.get("reference_image_url") or "").strip()
    mode    = (body.get("mode") or "ccdb").strip().lower()
    prompts = body.get("prompts") or []

    if not ref_url:
        return jsonify({"error": "missing_reference_image_url"}), 400
    if not (ref_url.startswith("http://") or ref_url.startswith("https://")):
        return jsonify({"error": "invalid_reference_url"}), 400
    if len(ref_url) > 500:
        return jsonify({"error": "reference_url_too_long"}), 400
    if mode not in VALID_MODES:
        return jsonify({"error": "invalid_mode", "valid": list(VALID_MODES)}), 400
    if mode == "txt2img":
        return jsonify({"error": "batch_requires_reference_mode"}), 400
    if not isinstance(prompts, list) or not prompts:
        return jsonify({"error": "missing_prompts"}), 400
    if len(prompts) > BATCH_MAX_PROMPTS:
        return jsonify({"error": "too_many_prompts", "max": BATCH_MAX_PROMPTS}), 400

    prompts = [str(p).strip() for p in prompts]
    if any(not p for p in prompts):
        return jsonify({"error": "empty_prompt_in_batch"}), 400
    if any(len(p) > 500 for p in prompts):
        return jsonify({"error": "prompt_too_long"}), 400

    # Credits 预检（批量要求 N 个）
    balance = int(user.get("credits_balance") or 0)
    if balance < len(prompts):
        return jsonify({
            "error": "insufficient_credits",
            "message": f"本批需 {len(prompts)} credits，当前余额 {balance}",
            "required": len(prompts), "balance": balance,
        }), 402

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
            if cur.fetchone()["cnt"] + len(prompts) > QUEUE_MAX:
                return jsonify({"error": "queue_full"}), 503

    batch_id = str(uuid.uuid4())

    # 原子扣 N credits
    new_balance = spend_credits_atomic(user["id"], len(prompts), "batch_spend", batch_id)
    if new_balance is None:
        return jsonify({"error": "insufficient_credits"}), 402

    job_ids = []
    try:
        with get_db() as db:
            with db.cursor() as cur:
                for p in prompts:
                    jid = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO jobs (id, batch_id, prompt, style, reference_image_url, mode, "
                        "status, ip, user_id) VALUES (%s, %s, %s, 'default', %s, %s, 'pending', %s, %s)",
                        (jid, batch_id, p, ref_url, mode, ip, user["id"])
                    )
                    job_ids.append(jid)
    except Exception:
        add_credits(user["id"], len(prompts), "refund", batch_id)
        raise

    return jsonify({
        "batch_id": batch_id,
        "job_ids": job_ids,
        "count": len(job_ids),
        "mode": mode,
        "credits_balance": new_balance,
    }), 201


@app.get("/api/batches/<batch_id>")
def get_batch_status(batch_id: str):
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, prompt, status, image_url, error FROM jobs "
                "WHERE batch_id = %s ORDER BY created_at",
                (batch_id,)
            )
            jobs = cur.fetchall()
    if not jobs:
        return jsonify({"error": "not_found"}), 404
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    return jsonify({
        "batch_id": batch_id,
        "total": len(jobs),
        "counts": counts,
        "all_done": counts["pending"] == 0 and counts["processing"] == 0,
        "jobs": [{"id": j["id"], "status": j["status"],
                  "image_url": j["image_url"], "prompt": j["prompt"],
                  "error": j["error"]} for j in jobs],
    })


@app.get("/api/jobs/<job_id>")
def get_job_status(job_id: str):
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
    if not job:
        return jsonify({"error": "not_found"}), 404

    resp = {"job_id": job["id"], "status": job["status"],
            "image_url": job["image_url"], "error": job["error"]}
    if job["status"] == "pending":
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS pos FROM jobs WHERE status='pending' AND created_at <= %s",
                    (job["created_at"],)
                )
                resp["queue_pos"] = cur.fetchone()["pos"]
    return jsonify(resp)


@app.get("/api/gallery")
def gallery():
    """返回最近完成的图片供首页展示"""
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT image_url, prompt, style FROM jobs "
                "WHERE status='done' AND image_url IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 8"
            )
            rows = cur.fetchall()
    return jsonify({"images": [
        {"url": r["image_url"], "prompt": r["prompt"], "style": r["style"]}
        for r in rows
    ]})


@app.get("/api/stats")
def stats():
    user = current_user()
    ip   = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    _, used, limit = check_quota(ip, user)
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
            queue_len = cur.fetchone()["cnt"]
    return jsonify({"queue_len": queue_len, "queue_max": QUEUE_MAX,
                    "quota": {"used": used, "limit": limit}})


# ─── 静态文件 & 健康检查 ───
@app.get("/")
def index():
    return app.send_static_file("index.html")


# Legal pages — required for AdSense, GDPR, basic compliance.
# Each is a standalone HTML in frontend/legal/ with shared style.css.
@app.get("/about")
def about_page():
    return app.send_static_file("legal/about.html")


@app.get("/privacy")
def privacy_page():
    return app.send_static_file("legal/privacy.html")


@app.get("/terms")
def terms_page():
    return app.send_static_file("legal/terms.html")


@app.get("/contact")
def contact_page():
    return app.send_static_file("legal/contact.html")


@app.get("/disclaimer")
def disclaimer_page():
    return app.send_static_file("legal/disclaimer.html")


@app.get("/ads.txt")
def ads_txt():
    return app.send_static_file("ads.txt")


@app.get("/health")
def health():
    try:
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


# ═══════════════════════════════════════════
# 支付接口（Creem）
# ═══════════════════════════════════════════

@app.get("/api/credits/packs")
def list_credit_packs():
    """前端拉 pack 列表用"""
    return jsonify({"packs": [
        {"id": k, "credits": v["credits"], "available": bool(v["product_id"])}
        for k, v in CREDIT_PACKS.items()
    ]})


@app.post("/api/credits/checkout")
@require_auth
def credits_checkout():
    """买 credit 包：{pack: "small"|"medium"|"large"} → 返回 Creem checkout_url"""
    if not CREEM_API_KEY:
        return jsonify({"error": "payment_not_configured"}), 503

    body = request.get_json(silent=True) or {}
    pack_id = (body.get("pack") or "").strip().lower()
    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        return jsonify({"error": "invalid_pack", "valid": list(CREDIT_PACKS)}), 400
    if not pack["product_id"]:
        return jsonify({"error": "pack_not_configured"}), 503

    user = request.user
    r = requests.post(
        f"{CREEM_BASE}/v1/checkouts",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"},
        json={
            "product_id":  pack["product_id"],
            "success_url": f"{OAUTH_REDIRECT_BASE}/api/payment/success",
            "customer":    {"email": user["email"]},
            "metadata": {
                "user_id": user["id"],
                "type": "credits",
                "pack": pack_id,
                "credits": pack["credits"],
            },
        },
        timeout=15,
    )
    if not r.ok:
        return jsonify({"error": "checkout_failed", "detail": r.text}), 500
    return jsonify({
        "checkout_url": r.json().get("checkout_url"),
        "pack": pack_id,
        "credits": pack["credits"],
    })


@app.get("/api/credits/history")
@require_auth
def credits_history():
    """最近 50 条流水"""
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT delta, balance_after, reason, ref_id, created_at FROM credit_ledger "
                "WHERE user_id = %s ORDER BY id DESC LIMIT 50",
                (request.user["id"],)
            )
            rows = cur.fetchall()
    return jsonify({"history": [
        {"delta": r["delta"], "balance_after": r["balance_after"],
         "reason": r["reason"], "ref_id": r["ref_id"],
         "created_at": str(r["created_at"])}
        for r in rows
    ]})


@app.post("/api/payment/checkout")
@require_auth
def create_checkout():
    if not CREEM_API_KEY or not CREEM_PRODUCT_PRO:
        return jsonify({"error": "payment_not_configured"}), 503
    user = request.user
    r = requests.post(
        f"{CREEM_BASE}/v1/checkouts",
        headers={"x-api-key": CREEM_API_KEY, "Content-Type": "application/json"},
        json={
            "product_id":  CREEM_PRODUCT_PRO,
            "success_url": f"{OAUTH_REDIRECT_BASE}/api/payment/success",
            "customer":    {"email": user["email"]},
            "metadata":    {"user_id": user["id"]},
        },
        timeout=15,
    )
    if not r.ok:
        return jsonify({"error": "checkout_failed", "detail": r.text}), 500
    return jsonify({"checkout_url": r.json().get("checkout_url")})


@app.get("/api/payment/success")
def payment_success():
    """Creem 付款成功后跳转到此，再跳回首页"""
    return redirect("/?payment=success")


@app.post("/api/payment/webhook")
def payment_webhook():
    payload = request.get_data()
    sig     = request.headers.get("creem-signature", "")

    if CREEM_WEBHOOK_SEC:
        expected = hmac.new(CREEM_WEBHOOK_SEC.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            app.logger.warning(f"[webhook] invalid_signature sig={sig!r} expected={expected!r}")
            return jsonify({"error": "invalid_signature"}), 401

    event      = request.get_json(silent=True) or {}
    # Creem 有两种格式：{ type, data } 或 { eventType, object }
    event_type = event.get("type") or event.get("eventType", "")
    data       = event.get("data") or event.get("object") or {}

    def _activate_pro(user_id, sub_id, cust_id):
        expires_at = datetime.now() + timedelta(days=31)
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET plan='pro', plan_expires_at=%s, "
                    "creem_subscription_id=%s, creem_customer_id=%s WHERE id=%s",
                    (expires_at, sub_id or "", cust_id or "", user_id)
                )
        _grant_pro_monthly_credits(int(user_id), sub_id or "")

    def _grant_pro_monthly_credits(user_id: int, sub_id: str):
        """Pro 订阅每月发 PRO_MONTHLY_CREDITS，按 YYYYMM 幂等（同月不重复）"""
        if PRO_MONTHLY_CREDITS <= 0:
            return
        ym = datetime.now().strftime("%Y%m")
        ref = f"pro_monthly_{sub_id or user_id}_{ym}"
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id FROM credit_ledger WHERE ref_id = %s AND reason = 'pro_monthly' LIMIT 1",
                    (ref,)
                )
                if cur.fetchone():
                    app.logger.info(f"[webhook] pro_monthly dup skip ref={ref}")
                    return
        add_credits(user_id, PRO_MONTHLY_CREDITS, "pro_monthly", ref)
        app.logger.info(f"[webhook] +{PRO_MONTHLY_CREDITS} pro_monthly user={user_id} ref={ref}")

    if event_type in ("checkout.completed", "subscription.active"):
        meta    = data.get("metadata") or {}
        user_id = meta.get("user_id")
        sub_id  = data.get("subscription_id") or data.get("id", "")
        cust_id = (data.get("customer") or {}).get("id") or data.get("customer_id", "")
        checkout_id = data.get("id", "")

        # 一次性 credit 包：type=credits，直接加 credits 不激活 Pro
        if meta.get("type") == "credits" and user_id:
            credits = int(meta.get("credits") or 0)
            if credits > 0:
                # 幂等：同一 checkout_id 不重复加
                with get_db() as db:
                    with db.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM credit_ledger WHERE ref_id = %s AND reason = 'purchase' LIMIT 1",
                            (checkout_id,)
                        )
                        if not cur.fetchone():
                            add_credits(int(user_id), credits, "purchase", checkout_id)
                            app.logger.info(f"[webhook] +{credits} credits user={user_id} ref={checkout_id}")
                        else:
                            app.logger.info(f"[webhook] dup ignored ref={checkout_id}")
            return jsonify({"status": "ok"})

        # Pro 订阅激活
        if user_id:
            _activate_pro(user_id, sub_id, cust_id)

    elif event_type == "subscription.update":
        # status=active 时激活 Pro
        status  = data.get("status", "")
        meta    = data.get("metadata") or {}
        user_id = meta.get("user_id")
        sub_id  = data.get("id", "")
        cust_id = (data.get("customer") or {}).get("id", "")
        if status == "active" and user_id:
            _activate_pro(user_id, sub_id, cust_id)
        elif status in ("active",) and not user_id:
            # 按 subscription_id 查用户
            with get_db() as db:
                with db.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE creem_subscription_id=%s", (sub_id,))
                    row = cur.fetchone()
            if row:
                _activate_pro(row["id"], sub_id, cust_id)

    elif event_type in ("subscription.renewed", "subscription.updated"):
        sub_id = data.get("subscription_id") or data.get("id", "")
        if sub_id:
            expires_at = datetime.now() + timedelta(days=31)
            with get_db() as db:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET plan_expires_at=%s WHERE creem_subscription_id=%s",
                        (expires_at, sub_id)
                    )
                    cur.execute(
                        "SELECT id FROM users WHERE creem_subscription_id=%s",
                        (sub_id,)
                    )
                    row = cur.fetchone()
            if row:
                _grant_pro_monthly_credits(row["id"], sub_id)

    elif event_type == "subscription.cancelled":
        pass  # 不立即降级，等 plan_expires_at 自然到期

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, debug=False)
