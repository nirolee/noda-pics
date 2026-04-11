#!/usr/bin/env python3
"""
noda.pics 本地 Poller
直连 223 MySQL → 取 pending 任务 → ComfyUI 生图 → SCP 到 223 → 更新 DB
"""
import time
import json
import uuid
import subprocess
import logging
import sys
import os
import tempfile
import urllib.request

# ── 配置 ──────────────────────────────────────────────
DB_HOST     = "YOUR_DB_HOST"
DB_PORT     = 3306
DB_USER     = "root"
DB_PASS     = ""
DB_NAME     = "noda_pics"

COMFY_URL   = "http://127.0.0.1:8188"
POLL_EVERY  = 5      # 没任务时等待秒数
IMG_W       = 768
IMG_H       = 512
IMG_SERVER  = "root@YOUR_DB_HOST"
IMG_DIR_223 = "/var/www/noda-pics/images"
IMG_BASE    = "https://img.noda.pics"
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


def scp_to_223(local_path: str, filename: str) -> str:
    remote = f"{IMG_SERVER}:{IMG_DIR_223}/{filename}"
    result = subprocess.run(["scp", local_path, remote], capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SCP 失败: {result.stderr.decode()}")
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

        log.info("  SCP 图片到 223...")
        image_url = scp_to_223(tmp_file, filename)

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

    while True:
        try:
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
