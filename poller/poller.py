#!/usr/bin/env python3
"""
noda.pics 本地 Poller
轮询 223 Job API → 调 ComfyUI 生成图片 → 上传结果
纯 stdlib，无需安装额外依赖
"""
import time
import json
import uuid
import urllib.request
import urllib.error
import logging
import sys

# ── 配置 ──────────────────────────────────────────────
API_BASE   = "http://YOUR_DB_HOST:8787"
API_KEY    = "8ea235c73b763162a61155c3c20146336d3dd46a464356d9"
COMFY_URL  = "http://127.0.0.1:8188"
POLL_EVERY = 5    # 没任务时的等待秒数
IMG_W      = 768
IMG_H      = 512
# ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("poller.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)


# ── 223 API helpers ───────────────────────────────────

def api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def api_put_image(path: str, img_bytes: bytes) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=img_bytes,
        method="PUT",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "image/png",
        }
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def api_report_error(job_id: str, msg: str):
    try:
        payload = json.dumps({"error": msg[:200]}).encode()
        req = urllib.request.Request(
            f"{API_BASE}/api/jobs/{job_id}/error",
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            }
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ── ComfyUI helpers ───────────────────────────────────

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
    workflow  = build_workflow(prompt_text)

    # 提交
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    prompt_id = json.loads(resp.read())["prompt_id"]
    log.info(f"  → ComfyUI prompt_id: {prompt_id[:8]}...")

    # 等待完成（最多 5 分钟）
    for i in range(150):
        time.sleep(2)
        hist = json.loads(
            urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10).read()
        )
        if prompt_id in hist:
            for node_out in hist[prompt_id].get("outputs", {}).values():
                if "images" in node_out:
                    img_info  = node_out["images"][0]
                    filename  = img_info["filename"]
                    subfolder = img_info.get("subfolder", "")
                    params    = f"filename={filename}&subfolder={subfolder}&type=output"
                    img_bytes = urllib.request.urlopen(
                        f"{COMFY_URL}/view?{params}", timeout=15
                    ).read()
                    log.info(f"  ← 生成完成，{len(img_bytes)//1024} KB")
                    return img_bytes

        if i % 5 == 4:
            log.info(f"  ... 等待 {(i+1)*2}s")

    raise TimeoutError("ComfyUI 超时（5分钟）")


# ── 主处理流程 ─────────────────────────────────────────

def process_job(job: dict):
    job_id = job["id"]
    prompt = job["prompt"]
    log.info(f"▶ job {job_id[:8]}  prompt: {prompt[:80]}")

    try:
        img_bytes = comfy_generate(prompt)

        log.info(f"  上传结果到 223...")
        result = api_put_image(f"/api/jobs/{job_id}/result", img_bytes)
        log.info(f"  ✓ {result.get('image_url', '')}")

    except Exception as e:
        log.error(f"  ✗ 失败: {e}")
        api_report_error(job_id, str(e))


# ── 入口 ──────────────────────────────────────────────

def main():
    log.info("=" * 48)
    log.info("noda.pics Poller 启动")
    log.info(f"  API   : {API_BASE}")
    log.info(f"  ComfyUI: {COMFY_URL}")
    log.info(f"  间隔  : {POLL_EVERY}s")
    log.info("=" * 48)

    while True:
        try:
            data = api_get("/api/jobs/next")
            job  = data.get("job")

            if job:
                process_job(job)
            else:
                time.sleep(POLL_EVERY)

        except urllib.error.URLError as e:
            log.warning(f"网络错误: {e} — 10s 后重试")
            time.sleep(10)

        except KeyboardInterrupt:
            log.info("Poller 已停止")
            break

        except Exception as e:
            log.error(f"未知错误: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
