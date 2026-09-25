# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/entities/<id>/actions`（`confirm_cluster`）：按规则核实并确认聚集事件，
  通过后把事件编号写回每份关联观察记录（`data.cluster_id`）；失败时 400 响应带
  `issues`，逐条给出问题记录编号与原因，原数据不变。事件已确认时重复提交沿用第一次结果。
- `GET /api/clusters/<id>/preview?observation_ids=a,b,c`：只核实不落库，返回 `valid`、
  `issues` 与合格成员；不传参数时用事件已存的 `observation_ids`。
- `GET /api/clusters/<id>/members`：查看事件与观察记录的关联情况。

聚集事件核实标准：同一区域（cluster 的 `region` 与观察记录的 `region`/`location`）、
采样时间跨度不超过 14 天、记录两两相距不超过 10 公里（Haversine），且至少 3 份
`submitted` 记录。已归入其他已确认事件、缺坐标、状态非 submitted、跨区域或不满足
时空条件的记录都会逐条指出。确认操作在单个 SQLite 事务内完成（事件置 confirmed +
成员写回 cluster_id）。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
