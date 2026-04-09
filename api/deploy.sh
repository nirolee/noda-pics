#!/bin/bash
# 部署脚本：在 223 服务器上运行
set -e

APP_DIR="/opt/noda-pics-api"
SERVICE="noda-pics-api"

echo "=== 部署 noda.pics Job API ==="

# 1. 创建目录
mkdir -p "$APP_DIR"
mkdir -p /var/www/noda-pics/images

# 2. 复制文件
cp app.py "$APP_DIR/"
cp requirements.txt "$APP_DIR/"
[ -f .env ] && cp .env "$APP_DIR/.env"

# 3. 安装依赖
cd "$APP_DIR"
pip3 install -r requirements.txt -q

# 4. 写 systemd 服务
cat > /etc/systemd/system/${SERVICE}.service << EOF
[Unit]
Description=noda.pics Job API
After=network.target

[Service]
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=/usr/bin/gunicorn -w 2 -b 0.0.0.0:8787 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 5. 启动服务
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo "✓ 服务启动完成"
systemctl status "$SERVICE" --no-pager

# 6. 配置 nginx（追加到现有配置）
echo ""
echo "=== nginx 配置（手动添加到 /etc/nginx/sites-available/ ）==="
cat << 'NGINX'
server {
    listen 80;
    server_name noda.pics www.noda.pics;

    # 图片静态文件
    location /images/ {
        alias /var/www/noda-pics/images/;
        expires 7d;
        add_header Cache-Control "public";
    }

    # Job API 反代
    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header X-Forwarded-For $remote_addr;
    }

    location /health {
        proxy_pass http://127.0.0.1:8787;
    }
}
NGINX
