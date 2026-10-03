# Project Workspace (project_workspace)

## 概述

Project Workspace（项目工作空间）是 VeroRun 的 AI 项目协作插件，提供项目隔离、文档 RAG（检索增强生成）、智能搜索与研究助手能力，适用于各类规模的组织。文档会被分块（chunk）并向量化以支持语义搜索；上传文件存储于 `data/project_workspace/` 目录。

## 功能特性

- **项目隔离**：每个项目拥有独立的数据与权限边界
- **文档 RAG**：文档自动分块并向量化，支持语义检索与带来源问答
- **智能搜索**：语义搜索 + 可选重排（rerank），返回最相关片段
- **研究助手**：面向 Agent 的研究辅助能力（`workspace_assistant` / `researcher`）
- **多格式支持**：PDF / DOCX / TXT / MD / PPTX / XLSX / CSV
- **文档摘要与对比**：支持文档摘要生成、文档对比分析
- **容量与保留策略**：可配置文件大小上限、保留天数、项目与文档数量上限

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- PostgreSQL 数据库（向量与元数据存储）

### 安装步骤

1. 将 `project_workspace` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 重启应用，插件自动初始化数据表
4. 在管理后台 "System" > "Project Workspace" 中使用

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_file_size_mb` | integer | 50 | 单文件大小上限（MB） |
| `allowed_extensions` | array | pdf/docx/txt/md/pptx/xlsx/csv | 允许的上传格式 |
| `chunk_size` | integer | 1000 | 文档分块大小 |
| `chunk_overlap` | integer | 200 | 分块重叠 |
| `semantic_search_top_k` | integer | 10 | 语义搜索返回条数 |
| `embedding_model` | string | text-embedding-3-small | 向量模型 |
| `storage_dir` | string | data/project_workspace/ | 文件存储目录 |
| `retention_days` | integer | 730 | 数据保留天数 |
| `max_projects_per_user` | integer | 50 | 每用户最大项目数 |
| `max_documents_per_project` | integer | 5000 | 每项目最大文档数 |
| `enable_cross_project_search` | boolean | false | 是否启用跨项目搜索 |

## Hook 接口

| Hook 标识符 | 说明 |
|-------------|------|
| `project_workspace/search` | 项目工作空间语义搜索 |

## 依赖关系

### 推荐依赖

| 依赖插件 | 版本约束 | 用途 |
|--------|--------|------|
| memory_engine | >=1.0.0 | 长期记忆 |
| chatbot | >=1.0.0 | AI 顾问联动 |
| analytics | >=1.0.0 | 数据洞察 |

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。
