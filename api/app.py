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

    allowed, used, limit = check_quota(ip, user)
    if not allowed:
        msg = f"今日已用 {used}/{limit} 次" if user else f"游客每天限 {RATE_GUEST} 次，注册后可获得 {RATE_USER} 次"
        return jsonify({"error": "quota_exceeded", "message": msg, "used": used, "limit": limit}), 429

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
            if cur.fetchone()["cnt"] >= QUEUE_MAX:
                return jsonify({"error": "queue_full", "message": "队列已满，请稍后再试"}), 503

    body   = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    style  = (body.get("style")  or "default").strip()

    if not prompt:
        return jsonify({"error": "missing_prompt"}), 400
    if len(prompt) > 500:
        return jsonify({"error": "prompt_too_long"}), 400

    job_id  = str(uuid.uuid4())
    user_id = user["id"] if user else None

    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, prompt, style, status, ip, user_id) "
                "VALUES (%s, %s, %s, 'pending', %s, %s)",
                (job_id, prompt, style, ip, user_id)
            )
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status='pending'")
            queue_len = cur.fetchone()["cnt"]

    return jsonify({
        "job_id": job_id, "status": "pending", "queue_pos": queue_len,
        "quota": {"used": used + 1, "limit": limit},
    }), 201


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

    if event_type in ("checkout.completed", "subscription.active"):
        meta    = data.get("metadata") or {}
        user_id = meta.get("user_id")
        sub_id  = data.get("subscription_id") or data.get("id", "")
        cust_id = (data.get("customer") or {}).get("id") or data.get("customer_id", "")
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

    elif event_type == "subscription.cancelled":
        pass  # 不立即降级，等 plan_expires_at 自然到期

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, debug=False)
