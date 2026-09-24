-- 工作台任务增加结构化流程阶段字段。
-- 阶段由后端执行器显式写入（见 bosshunter.web.tasks.PIPELINE_STAGE_KEYS），
-- 前端据此渲染流程进度条，不再解析日志文案 —— 文案一改进度条就失效的问题由此消除。
ALTER TABLE workbench_tasks ADD COLUMN IF NOT EXISTS stage TEXT;
