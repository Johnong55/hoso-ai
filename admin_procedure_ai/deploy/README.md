# Production Deployment Guide

Stack: FastAPI + Celery + MySQL + Redis + nginx, tất cả qua Docker Compose
trên 1 VPS. Qdrant + Cloudflare AI là cloud services.

## Pre-requisites

- VPS Ubuntu 24.04 (≥1 GB RAM, 25 GB SSD).
- Domain trỏ A record về IP VPS:
  - `api.hosoai.com` → backend
  - `hosoai.com` → frontend (sẽ deploy trên Cloudflare Pages)
- Qdrant Cloud cluster (free tier OK).
- Cloudflare account (đã có sẵn cho Workers AI).

## 1. SSH vào VPS, cài Docker

```bash
ssh root@<IP>

apt update && apt upgrade -y
apt install -y docker.io docker-compose-v2 git certbot python3-certbot-nginx ufw

# Firewall
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# User non-root (tuỳ chọn)
adduser deploy && usermod -aG docker deploy
```

## 2. Clone repo + cấu hình env

```bash
mkdir -p /opt/hosoai && cd /opt/hosoai
git clone <repo-url> .

# Copy env mẫu, edit secrets thật
cp .env.prod.example .env.prod
nano .env.prod
# Generate secrets:
#   openssl rand -hex 32   # → JWT_SECRET_KEY
#   openssl rand -base64 24  # → DB_PASSWORD / MYSQL_ROOT_PASSWORD
```

## 3. Lấy SSL cert (lần đầu, trước khi start nginx)

Tạm thời chạy nginx-only để serve ACME challenge:

```bash
# Tạm tắt firewall block port 80 nếu có
docker run -d --rm \
  -p 80:80 \
  -v $PWD/deploy/nginx-init.conf:/etc/nginx/conf.d/default.conf:ro \
  -v /var/www/certbot:/var/www/certbot \
  --name nginx-init \
  nginx:alpine

certbot certonly --webroot -w /var/www/certbot \
  -d api.hosoai.com \
  --email your-email@example.com \
  --agree-tos --no-eff-email

docker stop nginx-init
```

(File `deploy/nginx-init.conf` — chỉ block ACME, tạo sẵn nếu cần.)

## 4. Build + start stack

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Theo dõi log
docker compose -f docker-compose.prod.yml logs -f api worker
```

Health check:
```bash
curl https://api.hosoai.com/health
curl https://api.hosoai.com/docs   # Swagger UI
```

## 5. Crawl data lần đầu

Sau khi backend chạy, login admin qua UI → crawl 1-2 cơ quan nhỏ test
(Tòa án D01, Văn phòng Chính phủ G22). Sau ổn mới crawl các bộ lớn.

## 6. Deploy frontend lên Cloudflare Pages

```bash
# Trên máy dev:
cd ../procedure_ui
bun run build

# Push code lên GitHub, kết nối repo qua Cloudflare Pages dashboard:
# - Framework: Vite
# - Build command: bun run build
# - Output: dist
# - Env: VITE_API_BASE_URL=https://api.hosoai.com/api/v1
```

Custom domain `hosoai.com` → Cloudflare Pages tự issue SSL.

## 7. Backup MySQL (cron daily)

```bash
# Trên VPS, tạo /opt/hosoai/backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d-%H%M)
docker compose -f /opt/hosoai/docker-compose.prod.yml exec -T db \
  mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" admin_procedure_ai \
  | gzip > /opt/hosoai/backups/db-$DATE.sql.gz
# Giữ 30 ngày
find /opt/hosoai/backups -name "db-*.sql.gz" -mtime +30 -delete

# Cron entry:
0 3 * * * bash /opt/hosoai/backup.sh
```

## 8. Update code mới (CI thủ công)

```bash
cd /opt/hosoai
git pull
docker compose -f docker-compose.prod.yml build api
docker compose -f docker-compose.prod.yml up -d api worker beat
# Migration tự chạy trong api command (alembic upgrade head)
```

## Monitoring tối thiểu

```bash
# Health check loop trên VPS
curl -sf https://api.hosoai.com/health || systemctl restart docker

# Disk usage
df -h
docker system df

# Container restarts
docker compose -f docker-compose.prod.yml ps
```

## Common issues

| Symptom | Fix |
|---|---|
| 502 Bad Gateway | API chưa start xong → đợi 30s, check `docker logs api` |
| CORS error trên FE | Update `ALLOWED_ORIGINS` trong `.env.prod`, restart api |
| Migration fail | `docker compose exec api alembic upgrade head` thủ công |
| Worker không pick task | Check `docker logs worker`, verify REDIS_URL connect được |
| Qdrant collection mismatch | Reset collection: chạy `python -m scripts.reset_all --confirm` |

## Chi phí ước tính

| Item | Cost/mo |
|---|---|
| Vultr HCM 1GB VPS | $6 |
| Qdrant Cloud free tier | $0 |
| Cloudflare Workers AI free | $0 |
| Cloudflare Pages | $0 |
| Domain (.com) | $1 amortized |
| **Total** | **~$7** |
