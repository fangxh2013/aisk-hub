# ELK 端点与访问方式

> 2026-09-17 实测。**端点变更只改档案**（`~/.aisk/profiles/<项目>.yaml` 的 `envs.<env>.elk`），本文件与 `scripts/elk_query.py` 都不写地址。
> 凭据一律走 aisk sops 后端，**不要把口令写进本文件或技能正文**。

## PROD（2.0 业务中台）

| 项 | 值 |
|---|---|
| ES 集群 | 见档案 `envs.prod.elk.url`（集群其余节点记在该行注释里） |
| 认证 | Basic，账号见档案 `envs.prod.elk.user` |
| 凭据键 | 见档案 `envs.prod.elk.secret`（凭据后端另有 `prod/es-biz/pass`、`prod/kibana-log/pass`） |
| 跳板 | 见档案 `envs.prod.elk.jump`（`~/.ssh/config` 里的别名，已配置免密；实际地址与密钥记在该行注释里） |
| Kibana | 见档案 `envs.prod.elk` 段的注释（脚本不用） |
| 索引 | 前缀见档案 `envs.prod.elk.index_prefix`，按 UTC 日期拼成 `<前缀>-YYYY.MM.DD` |
| 命名空间 | 见档案 `envs.prod.k8s.ns`（elk 段不重复写） |

`aisk env --env prod` 可以直接看这些值（不在项目仓库里时用 `aisk --profile <项目> env --env prod`）。

**本机到 ES 不通，必须经跳板**。不要尝试直连，也不要从 `kubectl exec` 挖 Secret。

## 其他环境（未纳入脚本，需要时自行扩展）

| 环境 | 索引前缀 | 说明 |
|---|---|---|
| 预发 | `log-v2pre-*` | 同集群 |
| 1.0 老系统 | `log-v1prod-*` | 量极大（单日千万级），查询务必带服务与时间过滤 |
| 基础设施 | `log-infra-*` | Nacos / 网关等 |
| 其他 | `log-other-*` | — |

PRE 的 ELK 凭据键：`legacy/elk_service_pre_password`、`legacy/kibana_pre_user_*`。
PRE 的 ES 地址档案里还没有，需要时先核实，再补进档案 `envs.pre.elk`。

## 索引与时间

- 索引名按 **UTC** 分片，比北京时间**少 8 小时**。北京 09-17 08:00 = UTC 09-17 00:00。
- 查「最近 24 小时（北京时间）」跨 **两个**日索引；查「最近 2 小时」在北京时间 08:00 前后
  也会跨索引（UTC 00:00 是分界）。**用 `now-24h` 相对时间，脚本会自动拼索引名。**
- 2.0 生产索引单日文档量约 100 万（含全部级别），ERROR 约占 0.5%~2%。

## 字段速查

| 用途 | 字段 |
|---|---|
| 服务 | `kubernetes.deployment.name.keyword` |
| 镜像 | `container.image.name.keyword` |
| Pod | `kubernetes.pod.name.keyword` |
| 节点 | `host.name.keyword` |
| 命名空间 | `kubernetes.namespace` |
| 日志正文 | `message`（**JSON 字符串**，需解析） |

`message` 解析后可得：`@timestamp`（北京时间 +08:00）、`level`、`logger`、`thread`、
`message`、`service`、`traceId`、`stack_trace`。

## 常用过滤片段

```jsonc
// 粗筛 ERROR（本地再按解析出的 level 精确过滤）
{"match_phrase": {"message": "\"level\":\"ERROR\""}}

// 指定服务
{"term": {"kubernetes.deployment.name.keyword": "order"}}

// 含特定关键字
{"match_phrase": {"message": "Duplicate entry"}}
```

## 已知坑

1. `--config -` 与 `--data-binary @-` 抢同一个 stdin → 请求体丢失、查询退化成 match_all
   （表现为总数恰好 10000 且返回 INFO 日志）。整段脚本走 `ssh 'bash -s'` 的 stdin。
2. text 字段不能直接聚合，必须用 `.keyword` 子字段。
3. `track_total_hits` 默认 10000，不加 `true` 拿不到真实总数。
4. 本机若开着 Shadowrocket 等 TUN 代理访问跳板所在内网，SSH 与 HTTP 都可能异常，
   需把档案 `envs.prod.elk.tun_bypass` 的网段放行走 DIRECT。
