#!/usr/bin/env python3
"""project_workspace/services/researcher.py — Workspace Assistant service.

Invokes the Workspace Assistant Agent for document summarization,
comparison, Q&A with sources, and content analysis.
All operations are scoped to a project_id.
"""

import logging
import json
import time

logger = logging.getLogger('project_workspace.researcher')


class ResearchService:
    """Orchestrates Workspace Assistant Agent calls for workspace operations."""

    def __init__(self, config: dict):
        self._config = config or {}

    def _call_agent(self, capability: str, context: dict) -> dict:
        """Invoke the Workspace Assistant Agent via the Agent Matrix."""
        try:
            from agent_matrix.engine import UnifiedLLM
            llm = UnifiedLLM()

            system_prompts = {
                'document.summarize': (
                    "You are a workspace assistant. Summarize the following document "
                    "in a structured format: key findings, methodology, conclusions. "
                    "Keep the summary under 500 words. Use markdown formatting."
                ),
                'document.compare': (
                    "You are a workspace assistant. Compare the following documents "
                    "highlighting similarities, differences, and complementary insights. "
                    "Format as a structured comparison table."
                ),
                'qa.with_sources': (
                    "You are a workspace assistant. Answer the user's question based "
                    "solely on the provided context. Cite specific sources. "
                    "If the context does not contain enough information, say so."
                ),
                'content.analyze': (
                    "You are a workspace assistant. Analyze the provided content and "
                    "extract key themes, entities, and relationships. "
                    "Present the analysis in a structured format."
                ),
            }

            system_prompt = system_prompts.get(capability,
                "You are a workspace assistant. Answer helpfully and accurately.")

            user_prompt = json.dumps(context, ensure_ascii=False)

            response = llm.chat(
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                module='project_workspace',
            )

            return {
                'ok': True,
                'answer': response.get('content', ''),
                'model_used': response.get('model', ''),
                'tokens_used': response.get('tokens', 0),
            }

        except Exception as e:
            logger.error('agent call failed for capability %s: %s', capability, e)
            return {
                'ok': False,
                'answer': 'Agent call failed: %s' % str(e),
                'model_used': '',
                'tokens_used': 0,
            }

    def summarize_document(self, doc_text: str, doc_title: str = '') -> dict:
        """Summarize a single document."""
        return self._call_agent('document.summarize', {
            'title': doc_title,
            'text': doc_text[:10000],
            'task': 'summarize',
        })

    def compare_documents(self, docs: list) -> dict:
        """Compare multiple documents."""
        context = []
        for i, d in enumerate(docs):
            context.append({
                'doc_id': i + 1,
                'title': d.get('title', ''),
                'text': d.get('text', '')[:5000],
            })
        return self._call_agent('document.compare', {
            'documents': context,
            'task': 'compare',
        })

    def answer_question(self, query: str, context_chunks: list) -> dict:
        """Answer a question with source-grounded context."""
        context_text = '\n\n---\n\n'.join(
            '[Source: %s (relevance: %.2f)]\n%s' % (
                c.get('original_name', c.get('filename', 'Unknown')),
                c.get('similarity', 0),
                c.get('content', '')[:2000]
            )
            for c in context_chunks
        )
        return self._call_agent('qa.with_sources', {
            'question': query,
            'context': context_text,
            'task': 'qa',
        })
