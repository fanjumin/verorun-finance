#!/usr/bin/env python3
"""Universal Agent Engine — dynamic instantiation from DB config.
   Supports OpenAI, DeepSeek, OpenRouter, Ollama, etc."""
import logging
from openai import OpenAI
from services.crypto import decrypt

logger = logging.getLogger(__name__)

class UniversalAgentEngine:
    def __init__(self, config: dict):
        self.alias = config.get('alias', 'Unnamed Agent')
        self.mission = config.get('mission', '')
        self.system_prompt = config.get('system_prompt', 'You are a helpful assistant.')
        self.model = config.get('model_name', 'gpt-3.5-turbo')
        self.capabilities = config.get('capabilities', 'text')
        
        raw_key = config.get('api_key_enc', '')
        api_key = decrypt(raw_key) if raw_key else ''
        base_url = config.get('base_url', 'https://api.openai.com/v1')
        
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def ask(self, user_query: str, temperature: float = 0.7) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_query}
                ],
                temperature=temperature
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Agent {self.alias} failed: {e}")
            return f"Error: Agent service temporarily unavailable"
