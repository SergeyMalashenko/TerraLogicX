# TerraLogicX — мета-репозиторий земельно-аналитического контура.
#
# Все компоненты подключены как git submodules и закреплены на проверенных
# коммитах. Развёртывание целиком — одна команда:
#
#   git clone --recurse-submodules git@github.com:SergeyMalashenko/TerraLogicX.git
#   cd TerraLogicX
#   make up

GEODOCS_HOME  ?= $(CURDIR)/geodocs-data
VIEWER_HOST   ?= 127.0.0.1
VIEWER_PORT   ?= 8501
export GEODOCS_HOME

# Порядок = иерархия зависимостей (листья первыми):
#   geodocs-store -> pyrgis -> {pyrgis-agents, pynspd-agents} -> pyosm/py2gis -> engine
SYNC_REPOS  := geodocs-store pyrgis pyrgis-agents pynspd-agents pyosm-agents py2gis-agents terralogic-engine
# pynspd (форк) — reference-only: runtime-контур резолвит пакет pynspd с PyPI.
SUBMODULES  := $(SYNC_REPOS) pynspd

.PHONY: help init sync up stack viewer test pull bump status _warn-2gis

_warn-2gis:
	@[ -n "$$PY2GIS_API_KEY" ] || [ -f py2gis-agents/.env ] || \
		echo "WARN: PY2GIS_API_KEY не задан (env или py2gis-agents/.env) — py2gis-mcp :8003 не поднимется, контур деградирует без 2GIS"

help: ## список целей
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

init: ## поднять submodules на закреплённые коммиты
	git submodule update --init $(SUBMODULES)

sync: init ## uv sync --all-extras по порядку зависимостей + smoke-check импортов
	@for r in $(SYNC_REPOS); do echo "== sync $$r"; (cd $$r && uv sync --all-extras -q); done
	@geodocs-store/.venv/bin/python -c "import geodocs"
	@pyrgis/.venv/bin/python -c "import pyrgis"
	@pyrgis-agents/.venv/bin/python -c "import geodocs, pyrgis, pyrgis_agents, mcp"
	@pynspd-agents/.venv/bin/python -c "import geodocs, pynspd, pynspd_agents, mcp"
	@pyosm-agents/.venv/bin/python -c "import pyosm_agents, mcp"
	@py2gis-agents/.venv/bin/python -c "import py2gis_agents, mcp"
	@terralogic-engine/.venv/bin/python -c "import terralogic_engine, mcp, streamlit"
	@echo "OK: все окружения собраны и импортируются (GEODOCS_HOME=$(GEODOCS_HOME))"

up: sync _warn-2gis ## развернуть всё с нуля и поднять стек (engine :8004 + источники :8001/:8002/:8003/:8005)
	terralogic-engine/scripts/run-local-stack.sh --engine

stack: _warn-2gis ## поднять стек без пересборки окружений (предполагает выполненный make sync)
	terralogic-engine/scripts/run-local-stack.sh --engine

viewer: ## viewer :8501 (второй терминал; make viewer VIEWER_HOST=0.0.0.0 для сети)
	cd terralogic-engine && .venv/bin/terralogic-view --store case-store --host $(VIEWER_HOST) --port $(VIEWER_PORT)

test: sync ## pytest во всех runtime-репозиториях
	@for r in $(SYNC_REPOS); do echo "== test $$r"; (cd $$r && uv run pytest -q) || exit 1; done

pull: ## обновить submodules до origin/main и пересобрать окружения
	git submodule update --remote --merge $(SUBMODULES)
	$(MAKE) sync
	@echo "Указатели submodules обновлены; закрепите их командой make bump"

bump: ## закоммитить обновлённые указатели submodules
	git add $(SUBMODULES) && git commit -m "chore: bump submodules"

status: ## состояние всех submodules
	git submodule status $(SUBMODULES)
