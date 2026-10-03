# Project Workspace (project_workspace)

## Overview

Project Workspace is an AI-powered workspace with project isolation, document RAG (retrieval-augmented generation), intelligent search, and a research assistant. Documents are chunked and embedded for semantic search; uploads are stored under `data/project_workspace/`.

## Features

- **Project isolation**: per-project document namespaces and access control
- **Document RAG**: chunk + embed pipeline (configurable `chunk_size` / `chunk_overlap`)
- **Semantic search**: vector search over project documents (`semantic_search_top_k`)
- **Research assistant**: sub-agent `workspace_assistant` (summarize / compare / QA with sources)
- **Hooks**: provides `project_workspace/search`

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `max_file_size_mb` | `50` | Max upload size |
| `allowed_extensions` | pdf/docx/txt/md/pptx/xlsx/csv | Accepted document types |
| `chunk_size` / `chunk_overlap` | `1000` / `200` | Document chunking |
| `embedding_model` | `text-embedding-3-small` | Embedding model |
| `storage_dir` | `data/project_workspace/` | Upload storage directory |
| `retention_days` | `730` | Data retention window |
| `max_projects_per_user` | `50` | Per-user project cap |
| `enable_cross_project_search` | `false` | Allow cross-project search |

## Recommendations

Works best with `memory_engine`, `chatbot`, and `analytics` (see `recommends`).
