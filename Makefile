# Spell 日志分析服务 — 便捷命令
# 用法：make <target>  ARGS="..."  （ARGS 透传给 python main.py）

PYTHON ?= python3
MAIN  := $(PYTHON) main.py

.PHONY: help install migrate migrate-import serve analyze cli test

help:  ## 显示可用命令
	@echo "可用目标："
	@echo "  make install         安装依赖 (pip install -r requirements.txt)"
	@echo "  make migrate ARGS=... 执行 MySQL 建表 (透传 --mysql-* / --import-json)"
	@echo "  make serve   ARGS=... 启动 FastAPI 服务 (默认 --persist-mode 读 config.yaml)"
	@echo "  make cli     ARGS=... 一次性 CLI 分析 (传入 --file/--save/--persist-mode 等)"
	@echo "  make test            运行测试 (pytest test_spell.py)"

install:  ## 安装依赖
	pip install -r requirements.txt

migrate:  ## 建表：make migrate ARGS="--mysql-host db --mysql-database spell_log"
	$(MAIN) --migrate $(ARGS)

serve:  ## 启动服务：make serve ARGS="--persist-mode mysql --port 8000"
	$(MAIN) --serve $(ARGS)

cli:  ## 一次性分析：make cli ARGS="--file demo/test_logs.jsonl --save spell_state.json"
	$(MAIN) $(ARGS)

test:  ## 运行测试
	$(PYTHON) -m pytest test_spell.py -q
