# 部署指南

从本机 Docker 到云服务器上线，按顺序做即可。

---

## 一、本地 Docker 验证（先确保能跑起来）

```bash
# 1. 准备配置（.env 不会被提交，必须在服务器上单独创建）
cp .env.example .env

# 2. 编辑 .env，至少填这几项：
#    ZHIPUAI_API_KEY=你的key
#    JWT_SECRET=一串随机长字符（**容器部署必填**：APP_ENV=production 缺它直接拒绝启动）
#    ADMIN_PASSWORD=管理员初始口令（建议显式设置；留空则生成随机口令并在启动横幅
#                    打印一次、强制首次登录改密；填弱口令会被拒绝启动）

# 3. 构建并启动
docker compose up -d --build

# 4. 看日志与状态
docker compose logs -f
docker compose ps
```

打开 <http://127.0.0.1:8000>，会跳到**应用自己的登录页**（不是浏览器的 Basic 认证弹框）。
用 `.env` 里指定的管理员账号登录；其它成员在登录页切到「注册」自己开账号即可。
**首次登录后请立刻在右上角「修改密码」改掉初始口令。**
如果 `ADMIN_PASSWORD` 留空，初始口令是**启动日志横幅里那串随机口令**（只出现一次）——
用 `docker compose logs | grep 口令` 找回来；该账号在改密前除 `/api/auth/me` 与改密接口
外一律 403。

停止 / 重建：

```bash
docker compose down          # 停止并删除容器（data/ 与 storage/ 保留在宿主机）
docker compose up -d --build # 改完代码后重建
```

> ⚠️ **用户、会话、口令哈希全在 `storage/app.db` 里**。这个文件连同
> `storage/secret.key`、向量库 `storage/chroma.sqlite3` 以及文档清单
> `storage/manifests/` 一起需要在宿主机上保留 —— 删掉 `app.db` 等于清空所有账号，
> 删掉 `secret.key` 会让所有人被强制重新登录（它们的父目录 `storage/` 已挂 volume，正常不会丢）。
>
> ⚠️ **清库时别只删账号和清单，漏掉向量库**：`storage/chroma.sqlite3` 里的分块如果
> 还在、而 `storage/manifests/` 已经没了，这些文档会变成**幽灵文档** —— 列表里看不到，
> 检索却仍会命中、引用来源仍会列出来。服务启动时会自动对账清理这类残留并打警告日志
> （见「清库清单」）。

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
# scp -r D:/代码项目/rag-knowledge-base/* root@服务器IP:/opt/rag/
# 注：上面的 * 不含隐藏文件，所以 .env 不会被传；请在服务器上按 .env.example 新建
```

### 5. 配置并启动

```bash
cp .env.example .env
vim .env            # 填 ZHIPUAI_API_KEY、JWT_SECRET（必填）、ADMIN_PASSWORD（强口令）
docker compose up -d --build
```

> **多实例 / 多容器部署时必须显式设置 `JWT_SECRET` 并保持一致**。否则每个实例各自
> 生成一份密钥，用户在 A 实例登录后请求落到 B 实例会被判为未登录。

### 6. 验证

```bash
curl -i http://127.0.0.1:8000/health       # 公开接口，应返回 200 {"status":"ok"}
curl -i http://127.0.0.1:8000/api/sessions # 需要登录态，应返回 401
docker compose logs --tail=50             # 首次启动应能看到默认管理员的引导/警告日志
```

手机浏览器访问 `http://服务器IP:8000`，用管理员登录，上传一份 PDF 试问一句。

### 7. 日常运维

```bash
# 备份（账号 + 会话 + 文档 + 向量库；storage/ 是唯一必须备份的目录）
docker compose stop
tar czf rag-backup-$(date +%F).tar.gz storage/ data/
docker compose start

# 加一个成员：进管理后台 →「用户管理」，或让他自己在登录页注册
# 忘记管理员口令：改 .env 的 ADMIN_PASSWORD 无效（引导只在 users 表为空时执行），
#   正确做法是管理员登录后用「重置密码」，或进管理后台给对应账号重置
```

### 清库清单（重置 / 轮换凭证时照这份删）

**整目录处理，别只删其中一个** —— 漏删任何一个都可能留下「列表里看不到、检索却仍命中」的**幽灵文档**：

| 路径 | 是什么 | 漏删的后果 |
|---|---|---|
| `storage/app.db` | 账号、会话、消息、文档登记 | 账号与会话残留 |
| `storage/secret.key` | JWT 签名密钥（未显式设 `JWT_SECRET` 时用它） | 旧 token 仍可用 |
| `storage/manifests/` | 每个用户的文档清单（`user_<id>.json`） | 文档列表与真实数据分叉 |
| `storage/chroma.sqlite3`（连同同目录下的 UUID 子目录） | **向量库本体**：分块、向量与 HNSW 索引 | **幽灵文档**：清单与磁盘上都没有它，检索仍命中、引用来源仍列出 |
| `data/users/` | 上传的原始文档 | 文件已不在、向量还在 → 同样是幽灵 |

> 服务启动时会自动对账：清掉「清单里没有、向量库里还有」的 `source` 并打警告日志
> （`src/reconcile.py`，日志关键字「已清理幽灵向量」）。**但这是事后补救** ——
> 它保证的是「不会再长期留着幽灵」，不能替代上面这份清单；
> 清库与轮换凭证时仍然要按表逐项删干净。
>
> ⚠️ 2026-09-16 的真实事故就是漏了 `storage/chroma.sqlite3`：账号、清单、`data/users/`
> 都删了，向量库没删，于是 `p1-upload.pdf` / `e2e-p1-upload.pdf` 的分块留在库里，
> 引用来源一直显示着这两份早已不存在的文件（README 效果图当时就带着这两个名字）。


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

> 登录凭据走的是 `Authorization: Bearer`，反代**不需要**为它开任何额外配置；
> 但要确保 `client_max_body_size` 足够大，否则上传大文档时 Nginx 会先返 413。

---

## 四、常见问题

| 现象 | 原因与处理 |
|---|---|
| 调用报「模型不存在」 | 模型名变了。改 `.env` 的 `CHAT_MODEL`（如 `glm-5.1`），重启容器 |
| 上传后提问说"没有检索到" | 看 `docker compose logs` 确认建库是否报错；多半是 API Key 未配置 |
| 容器反复重启、日志 OOM | 内存不足。换 2G 以上机型，或用 `docker compose up -d` 后在宿主加 swap |
| `storage/` 权限报错 | 容器以 root 运行，宿主机目录属主变了。`sudo chown -R $USER storage data` |
| 改了 `EMBED_MODEL` / `EMBED_DIM` 后检索不准 | 旧向量与新维度不匹配，需在界面点「重建索引」（管理员可对全站执行 `POST /api/admin/system/reindex`） |
| 忘记管理员口令 | 引导只在 `users` 表为空时执行，改 `ADMIN_PASSWORD` 无效。用另一个管理员在后台「重置密码」，或临时把 `storage/app.db` 移走重启（**会清空所有账号与会话**，慎用）；若同时删了 `storage/manifests/`，务必连 `storage/chroma.sqlite3` 一起删 —— 只删前两者会留下**幽灵文档**（见 §7「清库清单」） |
| 重启后所有人都要重新登录 | 找不到 `JWT_SECRET` 且 `storage/secret.key` 丢了。显式设置 `JWT_SECRET` 即可避免 |
| 普通用户看不到「设置」按钮 | 设计如此。站点配置（API Key / 模型 / 检索参数）只对管理员开放 |
| 上传 / 删除文档报 500 | 本机若挂了「安全删除钩子」会拦删除动作；代码已按「尽力而为 + 改名挪开」兜底，若仍失败请看日志里 `清理…失败` 的告警行 |

---

## 五、上线前安全清单

- [ ] `.env` 已加入 `.gitignore`，**没有**被提交到 GitHub
- [ ] `JWT_SECRET` 已设为随机长字符串（**容器部署必填，否则服务拒绝启动**；多实例保持一致）
- [ ] `ADMIN_PASSWORD` 已设为强口令（弱口令会被拒绝启动），或已用启动横幅里的随机口令登录并**完成首次改密**
- [ ] 启动日志里没有「缺 JWT_SECRET」「ADMIN_PASSWORD 不满足强度要求」这类告警
- [ ] 仓库历史里没有出现过 API Key（`git log --all -- .env` 为空）
- [ ] `storage/app.db` 与 `storage/secret.key` 未被提交（`git log --all -- storage/` 为空）
- [ ] 服务器防火墙只放行必要端口
- [ ] 已配 HTTPS（用域名访问时）
- [ ] 在智谱控制台给 Key 设了额度上限，兜底防刷
- [ ] 已确认成员都是「注册后由管理员审核/禁用」可控的（管理后台可随时禁用任意账号）
