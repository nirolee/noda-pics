# noda.pics

免费 AI 图像生成站点，用家里闲置显卡跑 ComfyUI，完全 Serverless 前端 + 云数据库 + 本地算力的混合架构。

**在线体验：** [noda.pics](https://noda.pics)

---

## 架构

```
   浏览器
     │
     ▼
 ┌─────────────────┐        ┌──────────────────┐
 │  Flask API      │───────▶│  MySQL (公网)     │
 │  (Render 免费)   │        │   jobs 队列       │
 └─────────────────┘        └────────▲─────────┘
                                     │ 轮询
                                     │
                            ┌────────┴─────────┐
                            │  本地 Poller     │
                            │  (Windows + GPU) │
                            └────────┬─────────┘
                                     │
                          ┌──────────┴──────────┐
                          ▼                     ▼
                  ┌──────────────┐      ┌──────────────┐
                  │  ComfyUI     │      │ Cloudflare R2│
                  │  (本地 GPU)   │      │ (图片对象存储) │
                  └──────────────┘      └──────────────┘
```

**核心思路：** 用户请求只往数据库写一条 job，API 不处理生图；家里的 GPU 轮询 DB 领任务，生图完成后上传 R2，API 从 R2 返回图片 URL。

这样做的好处：
- Render 免费实例就够跑 API
- 没有 Serverless GPU 的按秒计费
- 图片进 R2（Cloudflare 出站免费）
- 本地 GPU 不用 24 小时在线，有任务才转

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML/CSS/JS（单页，零构建） |
| API | Flask + PyJWT + bcrypt |
| 数据库 | MySQL 8 |
| 生图 | ComfyUI + FLUX.2 Klein 4B (GGUF) |
| 存储 | Cloudflare R2 (S3 兼容) |
| 部署 | Render.com (API) + R2 自定义域名 (CDN) |
| 认证 | JWT + Google/GitHub OAuth |
| 支付 | Creem (订阅制 Pro) |

## 目录结构

```
noda-pics/
├── api/              # Flask API（部署到 Render）
│   ├── app.py        # 主应用，含认证/限流/支付/Webhook
│   ├── requirements.txt
│   └── .env.example
├── frontend/         # 静态前端（Flask 直接托管）
│   └── index.html
├── poller/           # 本地 Poller（Windows，跟 ComfyUI 同机）
│   ├── poller.py     # 轮询 → 生图 → 上传 R2 → 更新 DB
│   ├── requirements.txt
│   └── .env.example
└── render.yaml       # Render 部署配置
```

## 本地部署

### 前置

- MySQL 8（公网可访问，本地 Poller 要连）
- 一台带 NVIDIA GPU 的 Windows 机器（跑 ComfyUI）
- Cloudflare 账户（免费 R2 + 自定义域名）
- Render 账户（免费 Web Service）

### 1. 初始化数据库

```sql
CREATE DATABASE noda_pics CHARACTER SET utf8mb4;
-- 表结构见 api/app.py 中 SQL，手动建表或运行迁移
```

### 2. 部署 API 到 Render

```bash
# 1. Fork 本仓库到你的 GitHub
# 2. Render Dashboard → New Web Service → 选仓库
# 3. render.yaml 会自动识别
# 4. 在 Environment 里填入所有 sync:false 的变量（DB_PASS 等）
```

参考 [api/.env.example](api/.env.example) 了解所有环境变量。

### 3. 本地运行 Poller

```bash
cd poller
cp .env.example .env
# 编辑 .env 填入 DB 和 R2 凭据
pip install -r requirements.txt

# 启动 ComfyUI（另开一个窗口）
# 然后：
python poller.py
```

Windows 开机自启：双击 `start_poller.vbs` 创建快捷方式丢到 `shell:startup`。

### 4. ComfyUI 模型

Poller 默认使用 FLUX.2 Klein 4B (Q5_K_M)，需要下载：

- `flux-2-klein-4b-Q5_K_M.gguf` → `ComfyUI/models/unet/`
- `qwen_3_4b.safetensors` → `ComfyUI/models/clip/`
- `flux2-vae.safetensors` → `ComfyUI/models/vae/`

（或改 [poller/poller.py](poller/poller.py) 里的 `build_workflow` 换成任何你自己的 workflow。）

## 功能

- 游客每天 3 张 / 注册免费用户每天 10 张 / Pro 100 张（限额可配）
- Google / GitHub OAuth 登录
- Creem 订阅支付（Stripe 替代，对国内友好）
- R2 图片 48 小时自动过期清理（节省存储）
- 首页 Gallery 自动展示最近生成的 8 张
- 全局队列上限防过载

## License

MIT
