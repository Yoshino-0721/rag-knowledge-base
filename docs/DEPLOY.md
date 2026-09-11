# 部署指南

从本机 Docker 到云服务器上线，按顺序做即可。

---

## 一、本地 Docker 验证（先确保能跑起来）

```bash
# 1. 准备配置（.env 不会被提交，必须在服务器上单独创建）
cp .env.example .env

# 2. 编辑 .env，至少填这两项：
#    ZHIPUAI_API_KEY=你的key
#    APP_PASSWORD=一个只有你知道的口令

# 3. 构建并启动
docker compose up -d --build

# 4. 看日志与状态
docker compose logs -f
docker compose ps
```

打开 <http://127.0.0.1:8000>，会先弹出登录框（因为设了 `APP_PASSWORD`），
输入任意用户名 + 你设的密码即可进入。

停止 / 重建：

```bash
docker compose down          # 停止并删除容器（data/ 与 storage/ 保留在宿主机）
docker compose up -d --build # 改完代码后重建
```

---

## 二、云服务器部署（腾讯云 / 阿里云学生机）

### 1. 买机器

| 项 | 建议 |
|---|---|
| 机型 | 轻量应用服务器 / 学生机，**2 核 2G** 起步（ChromaDB 常驻内存，1G 会吃紧） |
| 系统 | Ubuntu 22.04 LTS 或 24.04 LTS |
| 带宽 | 3 Mbps 够用 |

> 学生认证后腾讯云 / 阿里云都有很便宜的套餐，够跑这个项目。

### 2. 开放端口

在控制台的**安全组 / 防火墙**里放行：

- `22`（SSH，必须）
- `8000`（本应用；若用 Nginx 反代则放行 `80` 和 `443`）

### 3. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # 免 sudo 用 docker，重新登录后生效
```

### 4. 把代码搞上去

```bash
# 方式 A：从 GitHub 拉（推荐，后面更新只要 git pull）
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>

# 方式 B：本机直接传（不经 GitHub）
# scp -r D:/代码项目/* root@服务器IP:/opt/rag/
```

### 5. 配置并启动

```bash
cp .env.example .env
vim .env            # 填 ZHIPUAI_API_KEY 和 APP_PASSWORD
docker compose up -d --build
```

### 6. 验证

```bash
curl -i http://127.0.0.1:8000/health     # 应返回 401（说明口令保护生效）
docker compose logs --tail=50
```

手机浏览器访问 `http://服务器IP:8000`，输入口令，上传一份 PDF 试问一句。

---

## 三、进阶：Nginx 反代 + 域名 + HTTPS

直接暴露 `8000` 端口能用，但用域名 + HTTPS 更像成品。

```bash
sudo apt update && sudo apt install -y nginx
```

`/etc/nginx/sites-available/rag`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 50m;      # 允许上传较大的 PDF

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用并签证书：

```bash
sudo ln -s /etc/nginx/sites-available/rag /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

同时把 `docker-compose.yml` 的端口改成 `"127.0.0.1:8000:8000"`，
只允许本机 Nginx 访问，避免绕过反代直连。

---

## 四、常见问题

| 现象 | 原因与处理 |
|---|---|
| 调用报「模型不存在」 | 模型名变了。改 `.env` 的 `CHAT_MODEL`（如 `glm-5.1`），重启容器 |
| 上传后提问说"没有检索到" | 看 `docker compose logs` 确认建库是否报错；多半是 API Key 未配置 |
| 容器反复重启、日志 OOM | 内存不足。换 2G 以上机型，或用 `docker compose up -d` 后在宿主加 swap |
| `storage/` 权限报错 | 容器以 root 运行，宿主机目录属主变了。`sudo chown -R $USER storage data` |
| 改了 `EMBED_MODEL` / `EMBED_DIM` 后检索不准 | 旧向量与新维度不匹配，需在界面点「重建索引」或调 `POST /ingest {"force": true}` |
| 忘了 `APP_PASSWORD` | 直接改 `.env` 后 `docker compose up -d`（会重建容器读取新环境变量） |

---

## 五、上线前安全清单

- [ ] `.env` 已加入 `.gitignore`，**没有**被提交到 GitHub
- [ ] `APP_PASSWORD` 已设置（公网必做）
- [ ] 仓库历史里没有出现过 API Key（`git log --all -- .env` 为空）
- [ ] 服务器防火墙只放行必要端口
- [ ] 已配 HTTPS（用域名访问时）
- [ ] 在智谱控制台给 Key 设了额度上限，兜底防刷
