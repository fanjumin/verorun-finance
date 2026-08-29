# VeroRun CLI 开发辅助
# 用法：make <target>
#   make install     安装（含测试依赖）
#   make test        运行离线单测
#   make lint        字节码编译检查
#   make help        查看命令树

PY ?= python
CLI = verorun_cli

install:
	$(PY) -m pip install -e ".[test]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall $(CLI)

help:
	$(PY) -m verorun_cli --help

.PHONY: install test lint help
