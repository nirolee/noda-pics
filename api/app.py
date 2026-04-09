import os
import uuid
import time
import json
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import redis

app = Flask(__name__)

# ─── CORS：允许 noda.pics 和本地调试 ───
CORS(app, resources={r"/api/*": {"origins": [
    "https://noda.pics",
    "https://www.noda.pics",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "null",  # 本地 file:// 调试
]}})

# ─── 配置 ───
REDIS_URL   = os.getenv("REDIS_URL", "redis://:eRJSPCWCydrkP6f6@127.0.0.1:6379/0")
API_KEY     = os.getenv("API_KEY", "changeme")
IMAGE_DIR   = os.getenv("IMAGE_DIR", "/var/www/noda-pics/images")
IMAGE_BASE  = os.getenv("IMAGE_BASE", "https://noda.pics/images")
RATE_LIMIT  = int(os.getenv("RATE_LIMIT", "5"))    # 每 IP 每小时最多 N 个任务
QUEUE_MAX   = int(os.getenv("QUEUE_MAX", "20"))     # 队列上限
JOB_TTL     = int(os.getenv("JOB_TTL", "86400"))   # job 保留 24 小时

rdb = redis.from_url(REDIS_URL, decode_responses=True)
os.makedirs(IMAGE_DIR, exist_ok=True)


# ─── Auth 装饰器（内部接口专��）───
def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_KEY}":
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ─── 限流：IP 每小时最多 RATE_LIMIT 次 ───
def check_rate_limit(ip: str) -> bool:
    key = f"rl:{ip}:{int(time.time() // 3600)}"
    count = rdb.incr(key)
    if count == 1:
        rdb.expire(key, 3600)
    return count <= RATE_LIMIT


# ─── Job 工具函数 ───
def make_job(job_id: str, prompt: str, style: str) -> dict:
    return {
        "id":         job_id,
        "prompt":     prompt,
        "style":      style,
        "status":     "pending",
        "created_at": int(time.time()),
        "started_at": "",
        "done_at":    "",
        "image_url":  "",
        "error":      "",
    }

def save_job(job: dict):
    rdb.hset(f"job:{job['id']}", mapping=job)
    rdb.expire(f"job:{job['id']}", JOB_TTL)

def get_job(job_id: str) -> dict | None:
    data = rdb.hgetall(f"job:{job_id}")
    return data if data else None


# ═══════════════════════════════════════════
# 公开接口（前端调用）
# ═══════════════════════════════════════════

@app.post("/api/jobs")
def submit_job():
    """前端提交任务"""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

    if not check_rate_limit(ip):
        return jsonify({"error": "rate_limit", "message": "每小时最多提交 5 个任务"}), 429

    queue_len = rdb.llen("jobs:queue")
    if queue_len >= QUEUE_MAX:
        return jsonify({"error": "queue_full", "message": "队列已满，请稍后再试"}), 503

    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    style  = (body.get("style") or "default").strip()

    if not prompt:
        return jsonify({"error": "missing_prompt"}), 400
    if len(prompt) > 500:
        return jsonify({"error": "prompt_too_long"}), 400

    job_id = str(uuid.uuid4())
    job = make_job(job_id, prompt, style)
    save_job(job)
    rdb.lpush("jobs:queue", job_id)

    return jsonify({
        "job_id":   job_id,
        "status":   "pending",
        "queue_pos": queue_len + 1,
    }), 201


@app.get("/api/jobs/<job_id>")
def get_job_status(job_id: str):
    """前端轮询任务状态"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404

    resp = {
        "job_id":    job["id"],
        "status":    job["status"],
        "image_url": job["image_url"] or None,
        "error":     job["error"] or None,
    }
    if job["status"] == "pending":
        # 计算大概排队位置
        try:
            queue = rdb.lrange("jobs:queue", 0, -1)
            pos = queue.index(job_id) + 1 if job_id in queue else 0
            resp["queue_pos"] = pos
        except Exception:
            pass

    return jsonify(resp)


@app.get("/api/stats")
def stats():
    """队列状态（公开）"""
    return jsonify({
        "queue_len": rdb.llen("jobs:queue"),
        "queue_max": QUEUE_MAX,
    })


# ═══════════════════════════════════════════
# 内部接口（本地 poller 专用，需 API_KEY）
# ═══════════════════════════════════════════

@app.get("/api/jobs/next")
@require_api_key
def next_job():
    """本地 poller 拿下一个待处理任务"""
    job_id = rdb.rpop("jobs:queue")
    if not job_id:
        return jsonify({"job": None}), 200

    job = get_job(job_id)
    if not job:
        return jsonify({"job": None}), 200

    job["status"]     = "processing"
    job["started_at"] = str(int(time.time()))
    save_job(job)

    return jsonify({"job": job}), 200


@app.put("/api/jobs/<job_id>/result")
@require_api_key
def upload_result(job_id: str):
    """本地 poller 上传生成结果（二进制图片）"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404

    # 接受图片文件 or 直接 body 二进制
    if request.files.get("image"):
        img_data = request.files["image"].read()
    else:
        img_data = request.data

    if not img_data:
        return jsonify({"error": "no_image_data"}), 400

    filename = f"{job_id}.png"
    filepath = os.path.join(IMAGE_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(img_data)

    image_url = f"{IMAGE_BASE}/{filename}"
    job["status"]    = "done"
    job["done_at"]   = str(int(time.time()))
    job["image_url"] = image_url
    save_job(job)

    return jsonify({"ok": True, "image_url": image_url}), 200


@app.put("/api/jobs/<job_id>/error")
@require_api_key
def upload_error(job_id: str):
    """本地 poller 上报失败原因"""
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    job["status"]  = "failed"
    job["done_at"] = str(int(time.time()))
    job["error"]   = body.get("error", "unknown error")[:200]
    save_job(job)

    # 失败的 job_id 放回队列尾部重试一次（可选，暂时注释）
    # rdb.lpush("jobs:queue", job_id)

    return jsonify({"ok": True}), 200


# ─── 健康检查 ───
@app.get("/health")
def health():
    try:
        rdb.ping()
        return jsonify({"ok": True, "redis": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "redis": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8787, debug=False)
