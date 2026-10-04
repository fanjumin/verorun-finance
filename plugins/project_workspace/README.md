# Project Workspace (project_workspace)

## Overview

Project Workspace is VeroRun's AI-powered collaboration plugin: project isolation, document RAG (retrieval-augmented generation), intelligent search, and a research assistant for organizations of all sizes. Documents are chunked and embedded for semantic search; uploads are stored under `data/project_workspace/`.

## Features

- **Project isolation**: per-project document namespaces and role-based access control (viewer / editor / owner)
- **Document RAG**: chunk + embed pipeline (configurable `chunk_size` / `chunk_overlap`), supports PDF / DOCX / TXT / MD / PPTX / XLSX / CSV
- **Semantic search**: vector search over project documents with optional rerank model
- **Research assistant**: sub-agents `workspace_assistant` and `researcher` for summarization, comparison, and QA with sources
- **Async document processing**: upload → worker pool processing (chunk + embed), with synchronous fallback
- **Citations**: every QA answer traces back to source documents and chunks
- **Hooks**: provides `project_workspace/search`

## Architecture

```
Admin UI (templates/project_workspace_admin.html)
        │
        ▼
Routes (/admin/project_workspace/*)
  projects CRUD / documents upload+list+delete / search / QA / citations / history
        │
        ├── auth.py          role guards (viewer / editor / owner)
        │
        ▼
Services
  doc_processor.py      document parsing, chunking, storage
  embedding.py          vector embedding via kernel gateway
  retriever.py          semantic search + optional rerank
  researcher.py         research assistant orchestration
        │
        ▼
Data layer — PG schema: project_workspace
  projects / project_members / documents / chunks / search_history
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `max_file_size_mb` | 50 | Max upload size (MB) |
| `allowed_extensions` | pdf/docx/txt/md/pptx/xlsx/csv | Accepted document types |
| `chunk_size` | 1000 | Document chunk size (chars) |
| `chunk_overlap` | 200 | Chunk overlap (chars) |
| `semantic_search_top_k` | 10 | Number of chunks returned by semantic search |
| `embedding_model` | `text-embedding-3-small` | Embedding model |
| `storage_dir` | `data/project_workspace/` | Upload storage directory |
| `rerank_model` | (empty) | Optional rerank model |
| `retention_days` | 730 | Data retention window |
| `max_projects_per_user` | 50 | Per-user project cap |
| `max_documents_per_project` | 5000 | Per-project document cap |
| `enable_cross_project_search` | false | Allow cross-project search |

## Python Dependencies

Required: `python-docx`, `PyMuPDF`, `python-pptx`

## Permissions Model

| Role | Capabilities |
|------|-------------|
| **viewer** | search, QA, document view, citations, history |
| **editor** | viewer + document upload, delete |
| **owner** | editor + project create/delete, member management |

## Recommended Dependencies

| Plugin | Version | Purpose |
|--------|---------|---------|
| memory_engine | >=1.0.0 | Long-term memory |
| chatbot | >=1.0.0 | AI assistant integration |
| analytics | >=1.0.0 | Data insights |

## Uninstall

Drops the entire `project_workspace` schema with `CASCADE` — zero residue.

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
