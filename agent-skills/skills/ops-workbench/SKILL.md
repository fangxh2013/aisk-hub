---
name: ops-workbench
description: 以只读、可审计的方式定位服务、容器、配置、构建和运行故障。适用于部署核对、健康检查和发布前环境确认。勿用：写业务代码→backend-engineering、web-engineering，数据库建模→db-workbench，生产日志归因→prod-log-analysis。触发：运维、环境、服务在哪台机器、部署、部署核对、发版走哪个 Job、节点、命名空间、端口、网关、入口、镜像、镜像 tag、镜像体积、体积优化、Pod、起不来、CrashLoopBackOff、rollout、卡住、排查、容器、Docker、Dockerfile、多阶段构建、Deployment、YAML、评审 YAML、Jenkins、Jenkinsfile、流水线、Nacos、DataId、配置核对、只读查询、查数据、生产库、健康检查。
metadata:
  version: 1.0.0
  agent_created: true
---

# Operations Workbench

## 工作方式

先确认环境、时间窗、服务身份和观测来源，再读取配置、状态、日志和构建证据。区分“配置声明”“构建产物”和“线上运行事实”，不要用其中一类替代另一类。

默认只读；任何写操作、重启、发布、扩缩容或配置变更都必须得到明确授权，并保留操作者、目标、时间和结果。排障结论要给出证据、影响范围和安全的下一步。

## 按需参考

- 只读排障和发布核对见 [references/operations-workflow.md](references/operations-workflow.md)。
