# syntax=docker/dockerfile:1.4
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com
COPY app /app/app
EXPOSE 8080 1502
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
