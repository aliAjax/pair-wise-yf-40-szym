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

## 批次状态机与样本检测

批次状态：`declared`（已申报）→ `pending_disposition`（待处置）→ `released`（已放行）；
存在阳性样本可 `quarantine`（已隔离）→ `destroy`（已销毁）。

检测不再以一次抽检结论为准，而是拆成批次下的多条样本记录：

| 动作 | 角色 | 说明 |
| --- | --- | --- |
| `register_sample` | admin / inspector / lab | 登记样本号 `sample_no`、采样点 `sampling_point`、检测人 `tester`，可带初检 `conclusion`（默认 `pending`）。登记后批次进入待处置 |
| `record_result` | admin / lab | 实验室补出初检结论（`negative` / `positive`），阳性会拦住放行 |
| `recheck` | admin / lab | 复检/复核，`tester` 必须与初检检测人不是同一人；结论追加到该样本的 `rechecks` 历史 |
| `release` | admin / quarantine | 全部样本初检阴性且复核阴性才允许放行 |
| `quarantine` | admin / quarantine | 至少存在一条阳性样本才能隔离 |
| `destroy` | admin / quarantine | 隔离后销毁 |

规则：

- 只要存在**待检**或**阳性**样本，批次停在 `pending_disposition`；实体响应的
  `data.hold_reasons` 会逐条列出原因，尝试放行时错误信息同样列出原因。
- 放行后复检改出阳性，批次自动重新进入 `pending_disposition`；放行记录保留在
  `data.release_history`（历次放行的检测人与时间）。
- 设施追溯按批次编号（`code`）或实体ID关联批次，批次状态回退后仍可查到。
  另提供 `GET /api/trace?code=<批次编号>`（或 `?id=<实体ID>`）查询批次及其关联设施。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/trace?code=<批次编号>`：按批次编号追溯批次与关联设施。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
