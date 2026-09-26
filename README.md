# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。

## 传播链阻断与恢复

- 申报批次时可传`parent_id`登记直接来源批次；来源必须已存在，且不允许自引用或成环。
- 批次被隔离（`quarantine`）后，其全部下游批次自动转入`pending_review`（待复核），并在`blocked_by`/`blocked_by_code`/`block_reason`中显示阻断批次。
- 待复核批次仍可直接确诊隔离（`quarantine`），但不能执行其他动作。
- 源头复检解除隔离（`recheck`）、放行（`release`）或销毁（`destroy`）后，被其阻断的后代按层级逐级恢复为阻断前状态；恢复时若上游仍有隔离或待复核批次，则保持待复核并改挂到最近的阻断源头。
- 任何一层复检再次阳性并重新隔离后，已恢复的下游批次会再次转入待复核。
- 每次阻断（`block`）、改挂（`reblock`）和恢复（`restore`）都会写入审计时间线，包含原因、处理人和时间；可用`GET /api/audit?entity_id=<id>`查看单批次的完整时间线。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤单个对象。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
