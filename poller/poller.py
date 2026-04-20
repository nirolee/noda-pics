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
                "SELECT id, prompt, style, reference_image_url, mode FROM jobs "
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
    """Text-to-image workflow (原始流程)"""
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


def build_pulid_workflow(prompt_text: str, ref_filename: str, pulid_strength: float = 1.0) -> dict:
    """PuLID-Flux2 workflow（角色面部硬锁 · 纯 text-to-image + 模型端注入身份）

    流程：参考图 → InsightFace 提取人脸 → EVA-CLIP 嵌入 → PuLID 把身份注入到 UNet
    → 纯 text-to-image 生成全新场景（构图不受参考图约束）

    strength 控制身份强度：
      - 0.8-1.0：标准（推荐 1.0）
      - 1.2-1.5：强（脸更像，但场景自由度略降）
      - 0.5-0.8：弱（脸只是"暗示"）
    """
    seed = int(time.time() * 1000) % (2 ** 31)
    return {
        # 基础 Flux 模型
        "1": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "flux-2-klein-4b-Q5_K_M.gguf"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "flux2-vae.safetensors"}},
        # PuLID 组件
        "20": {"class_type": "PuLIDInsightFaceLoader",
               "inputs": {"provider": "CPU"}},  # CPU 够用,避开 onnxruntime-gpu + Python 3.13 兼容坑
        "21": {"class_type": "PuLIDEVACLIPLoader",
               "inputs": {}},
        "22": {"class_type": "PuLIDModelLoader",
               "inputs": {"pulid_file": "pulid_flux2_klein_v2.safetensors"}},
        "23": {"class_type": "LoadImage",
               "inputs": {"image": ref_filename}},
        # 把 PuLID 身份注入到基础模型
        "24": {"class_type": "ApplyPuLIDFlux2",
               "inputs": {
                   "model": ["1", 0],
                   "pulid_model": ["22", 0],
                   "strength": pulid_strength,
                   "eva_clip": ["21", 0],
                   "face_analysis": ["20", 0],
                   "image": ["23", 0],
               }},
        # 文本编码
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt_text, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["2", 0]}},
        # 空 latent（纯 text-to-image，不用参考图 VAE encode）
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": IMG_W, "height": IMG_H, "batch_size": 1}},
        # Sampler 用 PuLID-modified 模型
        "7": {"class_type": "KSampler",
              "inputs": {
                  "model": ["24", 0],  # ← 关键：用 ApplyPuLIDFlux2 输出的模型
                  "positive": ["4", 0], "negative": ["5", 0],
                  "latent_image": ["6", 0],
                  "seed": seed, "control_after_generate": "fixed",
                  "steps": 4, "cfg": 1.0,
                  "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
              }},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "noda_pulid"}},
    }


def build_ccdb_workflow(prompt_text: str, ref_filename: str,
                        width: int = 1216, height: int = 832) -> dict:
    """CCDB workflow（角色风格化一致性 · ReferenceLatent 路径）

    流程：参考图 → VAE 编码成 latent → 作为 ReferenceLatent 注入条件 →
    sampler 生成时保持角色整体外观（脸+发型+服装+画风）

    vs PuLID：PuLID 只锁人脸 ID，对二次元/插画风失败。CCDB 锁整张图的
    "latent 特征"，对 Ghibli / cel-shaded / anime 这类风格化人物效果好。
    """
    seed = int(time.time() * 1000) % (2 ** 63)
    return {
        # 模型加载
        "123": {"class_type": "VAELoader",
                "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "246": {"class_type": "UnetLoaderGGUF",
                "inputs": {"unet_name": "flux-2-klein-4b-Q5_K_M.gguf"}},
        "255": {"class_type": "CLIPLoader",
                "inputs": {"clip_name": "qwen_3_4b.safetensors",
                           "type": "flux2", "device": "default"}},
        "81":  {"class_type": "ModelPassThrough",
                "inputs": {"model": ["246", 0]}},
        # 参考图 → VAE 编码
        "166": {"class_type": "LoadImage", "inputs": {"image": ref_filename}},
        "263": {"class_type": "ImageScaleToTotalPixels",
                "inputs": {"upscale_method": "nearest-exact",
                           "megapixels": 1, "resolution_steps": 1,
                           "image": ["166", 0]}},
        "266:204": {"class_type": "VAEEncode",
                    "inputs": {"pixels": ["263", 0], "vae": ["123", 0]}},
        # 文本条件
        "252": {"class_type": "CLIPTextEncode",
                "inputs": {"text": prompt_text, "clip": ["255", 0]}},
        "251": {"class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": ["252", 0]}},
        # ReferenceLatent：把参考图 latent 作为条件
        "266:264": {"class_type": "ReferenceLatent",
                    "inputs": {"conditioning": ["252", 0],
                               "latent": ["266:204", 0]}},
        "266:265": {"class_type": "ReferenceLatent",
                    "inputs": {"conditioning": ["251", 0],
                               "latent": ["266:204", 0]}},
        # 空 latent（实际生成用）
        "262": {"class_type": "SetImageSize",
                "inputs": {"width": width, "height": height}},
        "256": {"class_type": "EmptyFlux2LatentImage",
                "inputs": {"width": ["262", 0], "height": ["262", 1], "batch_size": 1}},
        # 采样器
        "257": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "258": {"class_type": "CFGGuider",
                "inputs": {"cfg": 1, "model": ["81", 0],
                           "positive": ["266:264", 0], "negative": ["266:265", 0]}},
        "259": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "260": {"class_type": "Flux2Scheduler",
                "inputs": {"steps": 4, "width": ["262", 0], "height": ["262", 1]}},
        "261": {"class_type": "SamplerCustomAdvanced",
                "inputs": {"noise": ["257", 0], "guider": ["258", 0],
                           "sampler": ["259", 0], "sigmas": ["260", 0],
                           "latent_image": ["256", 0]}},
        "145": {"class_type": "VAEDecode",
                "inputs": {"samples": ["261", 0], "vae": ["123", 0]}},
        # 保存（用原版 SaveImage 而非 Image Saver Simple，避免子目录问题）
        "9":   {"class_type": "SaveImage",
                "inputs": {"images": ["145", 0], "filename_prefix": "noda_ccdb"}},
    }


def upload_ref_to_comfy(image_url: str, filename: str) -> str:
    """下载远程参考图 → 上传到 ComfyUI 的 input 目录 → 返回内部文件名"""
    log.info(f"  ↓ 下载 reference: {image_url}")
    # Cloudflare / R2 会屏蔽默认 User-Agent,要伪装成浏览器
    ref_req = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
    )
    img_bytes = urllib.request.urlopen(ref_req, timeout=30).read()
    log.info(f"    {len(img_bytes)//1024} KB")

    # 用 multipart/form-data 上传
    boundary = f"----noda{uuid.uuid4().hex[:16]}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + img_bytes + f"\r\n--{boundary}\r\n".encode() + (
        f'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
        f"--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    log.info(f"  ↑ uploaded to ComfyUI: {result.get('name', filename)}")
    return result.get("name", filename)


def comfy_generate(prompt_text: str, reference_image_url: str | None = None,
                   mode: str = "txt2img") -> bytes:
    """提交任务到 ComfyUI，等待完成，返回图片字节

    mode:
      - txt2img: 纯文生图（无参考图）
      - pulid:   PuLID-Flux2，锁人脸 ID，适合真实人像照片
      - ccdb:    CCDB ReferenceLatent，锁整体风格+身份，适合二次元/插画风

    若 reference_image_url 非空而 mode 未指定，默认走 pulid（兼容旧行为）。
    """
    client_id = str(uuid.uuid4())

    if reference_image_url:
        ref_filename = f"noda_ref_{uuid.uuid4().hex[:8]}.png"
        comfy_filename = upload_ref_to_comfy(reference_image_url, ref_filename)
        if mode == "ccdb":
            workflow = build_ccdb_workflow(prompt_text, comfy_filename)
            log.info(f"  🎨 CCDB ReferenceLatent workflow")
        else:
            workflow = build_pulid_workflow(prompt_text, comfy_filename, pulid_strength=1.0)
            log.info(f"  🔒 PuLID-Flux2 workflow (strength=1.0)")
    else:
        workflow = build_workflow(prompt_text)

    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()

    req = urllib.request.Request(
        f"{COMFY_URL}/prompt", data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        # 把 ComfyUI 返回的 error body 打出来，不然 400 是个黑盒
        body = e.read().decode("utf-8", errors="replace")
        log.error(f"  ComfyUI {e.code} response body: {body[:800]}")
        raise
    prompt_id = json.loads(resp.read())["prompt_id"]
    log.info(f"  → ComfyUI prompt_id: {prompt_id[:8]}...")

    for i in range(300):  # 最多等 10 分钟（PuLID 首次加载要下 InsightFace+EVA-CLIP 模型）
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
    ref_url = job.get("reference_image_url")
    mode = (job.get("mode") or "txt2img").strip() or "txt2img"
    log.info(f"▶ job {job_id[:8]}  mode={mode}  prompt: {prompt[:80]}")
    if ref_url:
        log.info(f"  🔒 reference: {ref_url}")

    tmp_file = None
    try:
        img_bytes = comfy_generate(prompt, reference_image_url=ref_url, mode=mode)

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
