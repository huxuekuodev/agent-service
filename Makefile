# Deer Agent Service — 常用开发命令
#
#   make             = make dev：同时启动后端 + Web
#   make dev         同时启动后端(8001) + Web(5173)，Ctrl-C 一键全部停止
#   make dev-api     仅启动后端（uvicorn --reload）
#   make dev-web     仅启动 Web（vite dev）
#   make lint        ruff 检查（check + format --check）
#   make test        pytest
#   make build-web   Web 生产构建（vite build）
#   make clean       清理缓存与构建产物

.PHONY: dev dev-api dev-web lint test build-web clean

# 后端端口（可用 make dev API_PORT=9000 覆盖）
API_PORT ?= 8001

# 默认目标：快速启动前后端
dev:
	@echo "==> 启动 API   : http://127.0.0.1:$(API_PORT)（uvicorn --reload）"
	@echo "==> 启动 Web    : http://127.0.0.1:5173（vite dev，/sessions /monitor /health 代理到 API）"
	@echo "==> Ctrl-C 停止全部"
	@trap 'kill 0' INT TERM EXIT; \
	uv run uvicorn app.main:app --reload --port $(API_PORT) & \
	cd web && npm run dev & \
	wait

dev-api:
	uv run uvicorn app.main:app --reload --port $(API_PORT)

dev-web:
	cd web && npm run dev

lint:
	uv run ruff check app/ tests/
	uv run ruff format --check app/ tests/

test:
	uv run pytest

build-web:
	cd web && npm run build

clean:
	rm -rf .ruff_cache .pytest_cache web/dist
