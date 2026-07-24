# 云数据库博弈测试引擎

该模块只服务云数据库线路，不接入前端 Demo。

## 目录

- `database_game/models.py`：明牌、暗牌、叫价、Deal Box 和终局报告契约。
- `database_game/prompts.py`：Master、事实引擎、角色沙盒和红队提示词。
- `database_game/calculations.py`：区间交集、清算价值、ROI 和 IRR。
- `database_game/graph.py`：数据库博弈 LangGraph。
- `database_game/repository.py`：云数据库读取适配器。
- `database_game/service.py`：运行记录、报告入库和本地调试导出。
- `database_game/cli.py`：命令行入口。

## 数据流

1. Master 读取项目库存、事实、风险和主体摘要，生成三维动态检索计划。
2. 资产、经济财务、法律事实引擎并发检索，形成唯一 `PublicContext`。
3. 场景节点生成乐观、基准、对抗三套明确标注的模拟参数。
4. 清算、债务人、债权人、监管四个沙盒并发向 Deal Box 提交叫价。
5. Python 计算条款交集、清算价值、ROI 和 IRR。
6. Master 生成方案 A/B，Red Team 压测，Master 完成一次修订。
7. `complete_analysis_report` 将经过校验的四模块结论总结成完整中文 Markdown 报告，不重新检索或改数。
8. 结构化决策写入 `hongming_ai.ai_game_reports`，完整报告写入 `hongming_ai.ai_game_full_reports`，原始库 `hongming01` 保持只读。

## 初始化与运行

先执行：

```bash
python -m database_game.cli init-schema
```

校验投资授权：

```bash
python -m database_game.cli validate-mandate --mandate config/database_game_mandate.example.json
```

运行项目测试：

```bash
python -m database_game.cli run --project-id 1 --mandate config/database_game_mandate.example.json
```

每次成功运行会在 `output/` 生成三个文件：四模块决策 JSON、完整报告结构 JSON、可直接阅读的 Markdown 报告。

只调整报告写法时，可以复用已经落库的终局结果，不重新运行检索和角色 Agent：

```bash
python -m database_game.cli render-report --run-id 17
```

示例投资授权中的金额只是配置格式示例，正式测试前必须由项目负责人修改。
