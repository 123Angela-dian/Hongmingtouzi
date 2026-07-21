# 云数据库 AI 派生流程

## 数据分层

- 原始库：hongming01，只读取，不修改。
- AI 派生库：hongming_ai，保存主体、事件角色、补充证据关系、审核队列、运行记录和报告。
- 本地 output：调试与导出，不作为正式数据源。

## 初始化结构

SQL 文件：sql/hongming_ai_schema.sql

数据库对象：

- ai_entities
- ai_entity_aliases
- ai_event_entities
- ai_event_evidences
- ai_review_queue
- ai_pipeline_runs
- ai_reports
- v_ai_event_all_evidences
- v_ai_entity_timeline
- v_ai_event_full_details

## 常用命令

查看状态：

    python database_ai_pipeline.py status --project-id 1

同步源库待审核项目：

    python database_ai_pipeline.py seed-reviews --project-id 1

识别标准主体和事件角色：

    python database_ai_pipeline.py enrich-entities --project-id 1 --batch-size 25

合并唯一匹配的主体简称：

    python database_ai_pipeline.py consolidate-aliases --project-id 1

识别 supporting/conflicting 证据关系：

    python database_ai_pipeline.py enrich-evidences --project-id 1 --batch-size 20 --per-event 2 --max-pairs 300

只生成现有 ProjectState：

    python database_ai_pipeline.py build-state --project-id 1 --max-events 120 --max-evidences 120

运行现有四 Agent 和 Closer：

    python database_ai_pipeline.py run-workflow --project-id 1 --max-events 120 --max-evidences 120

## 读取规则

- 原始 primary 关系始终从 hongming01.event_evidences 读取。
- AI supporting/conflicting 关系从 hongming_ai.ai_event_evidences 读取。
- 两类关系通过 hongming_ai.v_ai_event_all_evidences 联合展示。
- conflicting 和低置信度结果进入 ai_review_queue，不自动覆盖原始事实。
- 现有 events 不去重、不改写。

## 依赖

额外依赖 pymysql。其余依赖沿用项目 requirements.txt。


## LangSmith 可读分析流程

每个子 Agent 展示四个业务步骤：

1. 01_context_selection：输入范围、事件数、证据数、少量事实和风险预览。
2. 02_evidence_and_conflict_check：primary、supporting、conflicting 证据数量及审核提示。
3. 03_agent_analysis：任务、受限上下文摘要和模型分析预览。
4. 04_agent_conclusion：结构化风险发现、风险等级、事件 ID 和证据 ID。

Closer 展示：

1. 01_consolidate_findings：四维结论数量和高风险事项。
2. 02_calculate_pcs：各维度命中关键词、扣分和最终 PCS。
3. 03_investment_decision：最终投决报告。

展示窗口参数：

- TRACE_PREVIEW_ITEM_LIMIT：每个步骤最多展示的条目数，默认 15。
- TRACE_PREVIEW_CHARS：单条文段最大字符数，默认 1200。
- TRACE_TOTAL_PREVIEW_CHARS：单个步骤预览总字符数，默认 15000。

完整上下文仍参与模型分析，完整证据仍保存在数据库；这些参数只限制 LangSmith 中的展示内容。


## Master Agent动态全库分析

数据库流程现在执行：

1. Master Agent盘点全部文件、原文、证据、事件、主体和关系。
2. Master Agent生成资产、经济、法律、财务四维任务计划。
3. 每个子Agent对全部499个事件和2051条证据进行相关性评分扫描。
4. 每个子Agent使用专业关键词对全部raw_contents执行数据库条件扫描，并回查高相关原文。
5. 只有高相关事件、证据和原文进入模型，避免超过上下文窗口。
6. Closer生成覆盖率报告并保存到ai_reports.coverage_report。

模型上下文参数：

- RAG_MAX_EVENTS：每个Agent最多送入模型的事件数，默认40。
- RAG_MAX_EVIDENCES：每个Agent最多送入模型的证据数，默认50。
- RAG_MAX_RAW_CONTENTS：每个Agent最多送入模型的原文数，默认8。

这些限制只控制送入模型的数量，不影响全量扫描统计。
