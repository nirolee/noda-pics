import os
import uuid
import time
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
import pymysql
import pymysql.cursors

app = Flask(__name__, static_folder="../frontend", static_url_path="")

# ─── CORS ───
CORS(app, resources={r"/api/*": {"origins": [
    "https://noda.pics",
    "https://www.noda.pics",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "null",
]}})

# ─── 配置 ───
DB_HOST     = os.getenv("DB_HOST",     "YOUR_DB_HOST")
DB_PORT     = int(os.getenv("DB_PORT", "3306"))
DB_USER     = os.getenv("DB_USER",     "root")
DB_PASS     = os.getenv("DB_PASS",     "")
DB_NAME     = os.getenv("DB_NAME",     "noda_pics")
RATE_LIMIT  = int(os.getenv("RATE_LIMIT", "5"))   # 每 IP 每小时最多 N 个任务
QUEUE_MAX   = int(os.getenv("QUEUE_MAX",  "20"))   # 排队上限（pending 数量）


def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=True,
    )


# ─── 限流：IP 每小时最多 RATE_LIMIT 次 ───
def check_rate_limit(ip: str) -> bool:
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM jobs "
                "WHERE ip = %s AND created_at >= NOW() - INTERVAL 1 HOUR",
                (ip,)
            )
            return cur.fetchone()["cnt"] < RATE_LIMIT


# ═══════════════════════════════════════════
# 公开接口（前端调用）
# ═══════════════════════════════════════════

@app.post("/api/jobs")
def submit_job():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

    if not check_rate_limit(ip):
        return jsonify({"error": "rate_limit", "message": "每小时最多提交 5 个任务"}), 429

    body   = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    style  = (body.get("style")  or "default").strip()

    if not prompt:
        return jsonify({"error": "missing_prompt"}), 400
    if len(prompt) > 500:
        return jsonify({"error": "prompt_too_long"}), 400

    # 检查队列上限
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'pending'")
            queue_len = cur.fetchone()["cnt"]

    if queue_len >= QUEUE_MAX:
        return jsonify({"error": "queue_full", "message": "队列已满，请稍后再试"}), 503

    job_id = str(uuid.uuid4())
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (id, prompt, style, status, ip) VALUES (%s, %s, %s, 'pending', %s)",
                (job_id, prompt, style, ip)
            )

    return jsonify({
        "job_id":    job_id,
        "status":    "pending",
        "queue_pos": queue_len + 1,
    }), 201


@app.get("/api/jobs/<job_id>")
def get_job_status(job_id: str):
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()

    if not job:
        return jsonify({"error": "not_found"}), 404

    resp = {
        "job_id":    job["id"],
        "status":    job["status"],
        "image_url": job["image_url"],
        "error":     job["error"],
    }

    if job["status"] == "pending":
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS pos FROM jobs "
                    "WHERE status = 'pending' AND created_at <= %s",
                    (job["created_at"],)
                )
                resp["queue_pos"] = cur.fetchone()["pos"]

    return jsonify(resp)


@app.get("/api/stats")
def stats():
    with get_db() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'pending'")
            queue_len = cur.fetchone()["cnt"]
    return jsonify({"queue_len": queue_len, "queue_max": QUEUE_MAX})


# ─── 前端 ───
@app.get("/")
def index():
    return app.send_static_file("index.html")


# ─── 健康检查 ───
@app.get("/health")
def health():
    try:
        with get_db() as db:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, debug=False)
