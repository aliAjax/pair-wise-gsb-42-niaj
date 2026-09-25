# 金融犯罪调查与制裁筛查系统

标准库实现的交易筛查、可疑线索、案件调查、实体合并、冻结、监管报告与**资金链路调查**原型，数据保存到 SQLite。

## 运行

要求 Python 3.11+（Python 3.9 亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8208`，默认数据库 `financial_crime.db`。可用 `--db`、`--host`、`--port` 修改。
`--seed` 会演示一条"客户 → 两层空壳（HK）→ 受限地区（KP）"的资金链路。

## 分层结构

存储、业务处理和页面分开，互不混入：

| 文件 | 层 | 职责 |
| --- | --- | --- |
| `domain.py` | 领域层 | 常量（高风险国家、角色）、异常、名称归一化/相似度等无状态规则 |
| `storage.py` | 存储层 | SQLite schema 与 `Repository`，只做数据读写，不含业务判断 |
| `service.py` | 业务处理层 | 筛查、线索、案件、实体合并、冻结与资金链路调查的全部领域规则 |
| `web.py` + `static/` | 页面层 | HTTP 路由、请求解析、JSON 响应与前端页面 |
| `app.py` | 入口 | 命令行解析与启动（历史导入路径仍可用） |

## 资金链路调查

- 账户是节点（`chain_accounts`，含持有人与国家，可挂靠 `entities` 复用冻结机制），转账是有向边（`chain_transfers`）。
- **登记转账**保留上下游账户与金额；账户不存在时按登记信息建档。**同一业务编号（`biz_ref`）重复提交只记一次**，重复请求返回首记内容（HTTP 200，`duplicate=true`），首次登记返回 201。
- 登记时实时筛查：上下游任一命中制裁名单或高风险国家（KP/IR/SY/CU/RU）→ `review`；任一挂靠实体已冻结 → `blocked`。
- **从任意一笔转账或任意账户向前后节点 BFS 展开**（支持 `upstream/downstream/both` 与层数限制），节点实时标出制裁名单、高风险国家、已冻结实体；标记按请求时数据计算，名单或冻结更新后立即生效。
- **主管冻结链路上的实体**后：经过其名下账户的后续登记转账即时 `blocked`；冻结时已处于 `review` 的在途转账转入**冻结复核队列**（`freeze_reviews`，状态 `frozen_review`），由其他主管/总监复核（四眼：冻结人不能自复核），`release` 放行、`uphold` 维持阻断。
- 案件可关联一笔或多笔链路转账（`POST /api/chain/link-case`，或登记时带 `case_id`）；**案件页** `GET /api/cases/{id}` 内联完整链路图（`chain.nodes/edges`，含命中与阻断统计），并遵守案件访问控制（被指派调查员或主管+）。

### 链路接口

请求头 `X-User`、`X-Role`。角色：`analyst`、`investigator`、`supervisor`、`director`、`auditor`。

- `POST /api/chain/transfers`：登记转账（幂等键 `biz_ref`）
- `GET /api/chain/expand?transfer_id=|account_id=&direction=both&max_depth=6`：前后向展开并标记风险节点
- `POST /api/chain/link-case`：把转账关联到案件（`case_id`、`transfer_id`）
- `GET /api/cases/{id}`：案件详情 + 资金链路；`GET /api/cases/{id}/chain`：仅链路
- `GET /api/freeze-reviews?status=pending`：冻结复核队列
- `POST /api/freeze-reviews/review`：复核裁决（`review_id`、`decision=release|uphold`、`reason`）

## 其他主要接口

- `GET /health`、`GET /api/state`、`GET /api/cases/{id}`
- `POST /api/entities`、`POST /api/entities/alias`、`POST /api/entities/merge`
- `POST /api/customers`
- `POST /api/watchlist`：新增或版本化更新名单
- `POST /api/transactions`：筛查并生成指纹去重后的线索
- `POST /api/alerts/triage`：误报关闭或形成案件
- `POST /api/cases/update`、`POST /api/cases/report`
- `POST /api/entities/freeze`、`POST /api/entities/unfreeze`

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：制裁命中到监管报告、冻结即时阻断、实体合并迁移（含链路账户）、线索降噪、案件保密和版本冲突；
资金链路：上下游与金额留存、业务编号幂等、前后向展开与制裁/高风险/冻结标记、冻结阻断后续交易、待复核转队列与四眼复核、案件页链路拼装与鉴权。

## 局限

名称筛查使用归一化和序列相似度，不替代专业名单供应商；冻结仅影响本系统内后续交易/链路转账；金额与风险规则是演示规则；身份依赖请求头，没有密钥管理、数字签名或真实监管报送通道。
