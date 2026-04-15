#!/usr/bin/env python3
"""
noda.pics 本地 Poller
直连 223 MySQL → 取 pending 任务 → ComfyUI 生图 → 上传 R2 → 更新 DB
"""
import time
import json
import uuid
import logging
import sys
import os
import tempfile
import urllib.request

try:
    import boto3
except ImportError:
    print("请先安装 boto3: pip install boto3")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv 未安装时直接读系统环境变量


def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        sys.exit(f"❌ 缺少环境变量：{key}，请在 poller/.env 中配置")
    return val


# ── 配置 ──────────────────────────────────────────────
DB_HOST     = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_USER     = os.environ.get("DB_USER", "noda_pics")
DB_PASS     = _require("DB_PASS")
DB_NAME     = os.environ.get("DB_NAME", "noda_pics")

# Cloudflare R2
R2_ENDPOINT    = _require("R2_ENDPOINT")
R2_ACCESS_KEY  = _require("R2_ACCESS_KEY")
R2_SECRET_KEY  = _require("R2_SECRET_KEY")
R2_BUCKET      = os.environ.get("R2_BUCKET", "noda-pics")
IMG_BASE       = os.environ.get("IMG_BASE", "https://img.noda.pics")

COMFY_URL      = "http://127.0.0.1:8188"
POLL_EVERY     = 5      # 没任务时等待秒数
IMG_W          = 768
IMG_H          = 512
IMAGE_TTL_HOURS = 48   # 图片保留时长（小时）
CLEANUP_EVERY  = 3600  # 每小时清理一次（秒）
# ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("poller.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    log.error("请先安装 pymysql: pip install pymysql")
    sys.exit(1)


# ── DB 工具 ───────────────────────────────────────────

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


def fetch_next_job() -> dict | None:
    """原子性地取出一个 pending 任务并标记为 processing"""
    db = get_db()
    try:
        with db.cursor() as cur:
            # 用 UPDATE + SELECT 避免并发重复消费（本地单进程问题不大，但好习惯）
            cur.execute(
                "SELECT id, prompt, style FROM jobs "
                "WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            )
            job = cur.fetchone()
            if not job:
                return None
            cur.execute(
                "UPDATE jobs SET status = 'processing', started_at = NOW() WHERE id = %s AND status = 'pending'",
                (job["id"],)
            )
            if cur.rowcount == 0:
                return None  # 被别的进程抢走了（理论上不会）
            return job
    finally:
        db.close()


def mark_done(job_id: str, image_url: str):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'done', done_at = NOW(), image_url = %s WHERE id = %s",
                (image_url, job_id)
            )
    finally:
        db.close()


def mark_failed(job_id: str, error: str):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'failed', done_at = NOW(), error = %s WHERE id = %s",
                (error[:500], job_id)
            )
    finally:
        db.close()


# ── R2 清理 ───────────────────────────────────────────

def cleanup_expired_images():
    """删除超过 IMAGE_TTL_HOURS 小时的 R2 图片，DB 中 image_url 置空"""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, image_url FROM jobs "
                "WHERE status = 'done' AND image_url IS NOT NULL "
                "AND done_at < NOW() - INTERVAL %s HOUR",
                (IMAGE_TTL_HOURS,)
            )
            expired = cur.fetchall()
    finally:
        db.close()

    if not expired:
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

    deleted = 0
    for row in expired:
        filename = row["image_url"].split("/")[-1]
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=filename)
            db = get_db()
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET image_url = NULL WHERE id = %s",
                    (row["id"],)
                )
            db.close()
            deleted += 1
        except Exception as e:
            log.warning(f"  清理失败 {filename}: {e}")

    log.info(f"🗑  清理完成，删除 {deleted}/{len(expired)} 张过期图片")


# ── ComfyUI ───────────────────────────────────────────

def build_workflow(prompt_text: str) -> dict:
    seed = int(time.time() * 1000) % (2 ** 31)
    return {
        "1": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "flux-2-klein-4b-Q5_K_M.gguf"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt_text, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": IMG_W, "height": IMG_H, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {
                  "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                  "latent_image": ["6", 0],
                  "seed": seed, "control_after_generate": "fixed",
                  "steps": 4, "cfg": 1.0,
                  "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
              }},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "noda"}},
    }


def comfy_generate(prompt_text: str) -> bytes:
    """提交任务到 ComfyUI，等待完成，返回图片字节"""
    client_id = str(uuid.uuid4())
    payload   = json.dumps({"prompt": build_workflow(prompt_text), "client_id": client_id}).encode()

    req = urllib.request.Request(
        f"{COMFY_URL}/prompt", data=payload,
        headers={"Content-Type": "application/json"}
    )
    resp      = urllib.request.urlopen(req, timeout=15)
    prompt_id = json.loads(resp.read())["prompt_id"]
    log.info(f"  → ComfyUI prompt_id: {prompt_id[:8]}...")

    for i in range(150):  # 最多等 5 分钟
        time.sleep(2)
        hist = json.loads(
            urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10).read()
        )
        if prompt_id in hist:
            for node_out in hist[prompt_id].get("outputs", {}).values():
                if "images" in node_out:
                    info      = node_out["images"][0]
                    params    = f"filename={info['filename']}&subfolder={info.get('subfolder','')}&type=output"
                    img_bytes = urllib.request.urlopen(f"{COMFY_URL}/view?{params}", timeout=15).read()
                    log.info(f"  ← 生成完成，{len(img_bytes)//1024} KB")
                    return img_bytes
        if i % 5 == 4:
            log.info(f"  ... 等待 {(i+1)*2}s")

    raise TimeoutError("ComfyUI 超时（5分钟）")


def upload_to_r2(local_path: str, filename: str) -> str:
    import boto3
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )
    s3.upload_file(local_path, R2_BUCKET, filename, ExtraArgs={"ContentType": "image/png"})
    return f"{IMG_BASE}/{filename}"


# ── 主处理流程 ─────────────────────────────────────────

def process_job(job: dict):
    job_id = job["id"]
    prompt = job["prompt"]
    log.info(f"▶ job {job_id[:8]}  prompt: {prompt[:80]}")

    tmp_file = None
    try:
        img_bytes = comfy_generate(prompt)

        filename = f"{job_id}.png"
        tmp_file = os.path.join(tempfile.gettempdir(), filename)
        with open(tmp_file, "wb") as f:
            f.write(img_bytes)

        log.info("  上传到 R2...")
        image_url = upload_to_r2(tmp_file, filename)

        mark_done(job_id, image_url)
        log.info(f"  ✓ done → {image_url}")

    except Exception as e:
        log.error(f"  ✗ 失败: {e}")
        mark_failed(job_id, str(e))
    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.remove(tmp_file)


# ── 入口 ──────────────────────────────────────────────

def main():
    log.info("=" * 48)
    log.info("noda.pics Poller 启动")
    log.info(f"  DB    : {DB_HOST}/{DB_NAME}")
    log.info(f"  ComfyUI: {COMFY_URL}")
    log.info(f"  间隔  : {POLL_EVERY}s")
    log.info("=" * 48)

    last_cleanup = 0

    while True:
        try:
            # 每小时清理一次过期图片
            if time.time() - last_cleanup > CLEANUP_EVERY:
                cleanup_expired_images()
                last_cleanup = time.time()

            job = fetch_next_job()
            if job:
                process_job(job)
            else:
                time.sleep(POLL_EVERY)

        except pymysql.Error as e:
            log.warning(f"DB 错误: {e} — 10s 后重试")
            time.sleep(10)

        except KeyboardInterrupt:
            log.info("Poller 已停止")
            break

        except Exception as e:
            log.error(f"未知错误: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
