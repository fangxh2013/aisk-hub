# Contributing

提交前运行：

```text
./bin/aisk public verify
PYTHONPATH=agent-skills python3 -m unittest discover -s agent-skills/tests
```

内核变更必须保持四工具共享协议不变；工具特性放在 `agent-skills/adapters/<tool>/`。高风险动作必须保留工具归属、任务号、会话号和人工确认门禁。
