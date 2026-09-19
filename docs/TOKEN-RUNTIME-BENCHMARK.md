# Token Runtime 回归门禁与黄金任务基准

执行门禁：

```text
cd agent-skills && PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

`agent-skills/tests/test_token_runtime/` 固化六个黄金任务和以下行为：

- 高危输入硬触发 `deep`；
- 不确定、冲突、缺失证据统一 `fail-to-full` 并回退到 `emergency`；
- 预算只能裁剪可选上下文，不能裁掉 security、privacy、verification 保护规则；
- digest 与审计记录不含正文、凭据或真实环境值；
- Codex、Claude、Antigravity、WorkBuddy 四工具 token-policy 可解析；
- `standard` 默认模式继续兼容 `spec/token-efficiency.yaml` 旧静态 contract。

## 分数口径

`reports/token_runtime_baseline.json` 中的 `offline_contract_score` 只表示本地黄金任务、策略结构、旧静态 contract、四工具声明和元数据审计通过；它不是模型准确率、真实 tokenizer 用量或客户端联调证明。

当前离线契约分为 `100.0`。四个适配器均声明 `offline_contract`、`dry-run` 和
`not_integrated`，因此 `real_runtime_score` 必须保持 `null`，`runtime_status` 必须保持
`offline-only`。真实运行分只有在各客户端实际加载、执行并留下可审计证据后才能填写。

测试不访问网络、数据库、Nacos、Kubernetes、Jenkins 或凭据存储；未联调不得写成通过。
