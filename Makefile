.PHONY: install backend frontend test demo-v2

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && PYTHONPATH=. .venv/bin/pytest -q

# 数据契约 V2 迁移端到端演示（隔离示例数据：迁移 -> 模型上线 -> 回滚）
demo-v2:
	cd backend && PYTHONPATH=. .venv/bin/python -E -m app.services.contract_v2.demo_run
