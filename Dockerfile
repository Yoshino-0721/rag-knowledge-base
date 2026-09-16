# 团队 RAG 知识库 —— 运行镜像
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用镜像层缓存：只要 requirements.txt 没变就不会重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 文档目录与向量库目录（建议用 volume 挂载持久化）
RUN mkdir -p /app/data /app/storage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
