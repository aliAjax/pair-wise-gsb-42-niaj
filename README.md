# 金融犯罪调查与制裁筛查系统

标准库实现的交易筛查、可疑线索、案件调查、实体合并、冻结和监管报告原型，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8208`，默认数据库 `financial_crime.db`。可用 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role`。角色有 `analyst`、`investigator`、`supervisor`、`director`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/cases/{id}`
- `POST /api/entities`、`POST /api/entities/alias`、`POST /api/entities/merge`
- `POST /api/customers`
- `POST /api/watchlist`：新增或版本化更新名单
- `POST /api/transactions`：筛查并生成指纹去重后的线索
- `POST /api/alerts/triage`：误报关闭或形成案件
- `POST /api/cases/update`、`POST /api/cases/report`
- `POST /api/entities/freeze`、`POST /api/entities/unfreeze`

## 资金链路调查

链路存储（`chain_store.py`）、业务处理（`chain_service.py`）和页面（`static/chain.html`，入口 `/chain.html`）分开，与主应用共用同一 SQLite 文件。

- `POST /api/chain/transfers`：登记转账，保留上下游账户（账号、持有人、国家、可关联实体）和金额；同一 `biz_ref` 重复提交只记一次，返回 `duplicate: true`。任一端账户被冻结则 `blocked`，命中名单或高风险国家则 `review`，否则 `recorded`。
- `GET /api/chain/transfers/{id}/expand?depth=n`：从任意一笔向前（资金来源）和向后（资金去向）展开 n 层（1–8），节点带 `sanctioned`、`high_risk`、`frozen` 标记。
- `GET /api/cases/{id}/chain`：案件页展示链路，按案件指派和角色控制访问。
- 主管冻结实体后，链路上关联账户同步冻结：后续转账 `blocked`，待复核转账转入 `frozen_review`；解冻后恢复 `review`。实体合并时链路账户随目标实体迁移。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖制裁命中到监管报告、冻结即时阻断、实体合并迁移、线索降噪、案件保密和版本冲突，以及链路登记幂等、前后展开标记、冻结传导阻断与冻结复核、案件链路访问控制。

## 局限

名称筛查使用归一化和序列相似度，不替代专业名单供应商；冻结仅影响本系统内后续交易；金额与风险规则是演示规则；身份依赖请求头，没有密钥管理、数字签名或真实监管报送通道。
