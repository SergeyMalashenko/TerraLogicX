# TerraLogicX

Мета-репозиторий земельно-аналитического контура: головной сервис
`terralogic-engine` + четыре независимых сервиса-источника + общая база
нормативных документов. Все компоненты — репозитории одного аккаунта
(`SergeyMalashenko`), подключённые как git submodules и закреплённые
на проверенных коммитах.

## Иерархия зависимостей

Уровень кода (стрелки — uv path-зависимости, editable):

```text
                        ┌─────────────────────────────────────────┐
                        │         geodocs-store 0.2.0             │
                        │   (пакет "geodocs": общая SQLite-база   │
                        │    документов ПЗЗ/генпланов/ВРИ)        │
                        └───────────────┬─────────────┬───────────┘
                       editable ../geodocs-store      editable ../geodocs-store
                                        │             │
                                        ▼             ▼
┌──────────────────────┐      ┌──────────────────────────────────┐
│   pynspd-agents 0.4.0│      │      pyrgis-agents 0.6.0         │
│   (pynspd-mcp :8001) │      │      (pyrgis-mcp :8005)          │
│                      │      │                                  │
│  + geodocs (path)    │      │  + pyrgis 0.6.0 ← ../pyrgis      │
│  + pynspd>=1.1.13    │      │    (editable)                    │
│    **с PyPI**        │      │  + geodocs ← ../geodocs-store    │
└──────────────────────┘      │  + mcp                           │
                              └──────────────────────────────────┘

pyrgis 0.6.0 — библиотека клиента RGIS MO (зависит только от PyPI-пакетов)
pyosm-agents 0.4.1 (:8002) и py2gis-agents 0.1.0 (:8003) — внутренних зависимостей нет
pynspd — форк-референс: runtime резолвит пакет pynspd с PyPI, submodule нужен только для разработки
terralogic-engine 0.9.0 — НЕ импортирует ни один из агентов; связь только по HTTP MCP
```

Уровень runtime (после `make up`):

```text
Hermes / terralogic-collect
        │
        ▼
terralogic-mcp :8004 ──HTTP MCP──┬──> pynspd-mcp :8001 ─┐
        │                        ├──> pyosm-mcp  :8002   │  shared GEODOCS_HOME
        │                        ├──> py2gis-mcp :8003   │  (SQLite WAL — общий
        │                        └──> pyrgis-mcp :8005 ─┘   кэш документов)
        ▼
terralogic-view :8501 (read-only Streamlit поверх case-store)
```

## Развёртывание с нуля

Требования: `git`, `uv`, SSH-доступ к GitHub. Для полного стека нужен ключ
2GIS API (`PY2GIS_API_KEY` в окружении или `py2gis-agents/.env`) — без него
`py2gis-mcp` не поднимется, остальной контур работает, но без слоя 2GIS
(`make up`/`make stack` выводят предупреждение).

```bash
git clone --recurse-submodules git@github.com:SergeyMalashenko/TerraLogicX.git
cd TerraLogicX
make up        # submodules -> uv sync --all-extras по порядку зависимостей -> стек сервисов
```

Дальше во втором терминале:

```bash
make viewer    # http://127.0.0.1:8501 — карта кейса, слои, документы, аналитика
```

Hermes настраивается на `http://127.0.0.1:8004/mcp` (единственная точка входа).

## Цели Makefile

| Команда | Действие |
|---|---|
| `make up` | развёртывание целиком: init → sync → стек (foreground, Ctrl-C останавливает все сервисы) |
| `make sync` | только окружения: `uv sync --all-extras` по иерархии + smoke-check импортов |
| `make stack` | поднять стек без пересборки окружений |
| `make viewer` | viewer :8501 (`VIEWER_HOST=0.0.0.0` для доступа по сети) |
| `make test` | pytest во всех runtime-репозиториях |
| `make pull` | обновить submodules до origin/main и пересобрать (указатели потом `make bump`) |
| `make bump` | закоммитить обновлённые указатели submodules |
| `make status` | состояние submodules |

## Два неочевидных инварианта

1. **`uv sync --all-extras` обязателен.** Без extras optional-зависимости `mcp`
   (у агентов) и `viewer` (у engine) не попадают в venv, и точки входа `*-mcp` /
   `terralogic-view` падают с `ImportError`. `make sync` делает это всегда.
2. **`GEODOCS_HOME` — один общий каталог** для `pynspd-mcp` и `pyrgis-mcp`
   (по умолчанию `./geodocs-data` в корне workspace). Это общий кэш
   нормативных документов; если сервисы увидят разные пути, база раздвоится.

Обновление компонентов: `make pull && make bump` — submodules переезжают на
свежие `origin/main`, указатели фиксируются коммитом в этом репозитории.

Быстрая проверка полного контура для кадастрового номера (пример, Красногорск):

```bash
cd terralogic-engine && .venv/bin/terralogic-collect 50:11:0020310:49 \
  --case-id case-50-11-0020310-49 --store ./case-store \
  --nspd-url http://127.0.0.1:8001/mcp --osm-url http://127.0.0.1:8002/mcp \
  --dgis-url http://127.0.0.1:8003/mcp --rgis-url http://127.0.0.1:8005/mcp \
  --margin-m 1000
```
