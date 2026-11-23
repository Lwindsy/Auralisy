"""Controlled model and web adapters selected per deployment."""

from .deepseek import DeepSeekResponsesAdapter, DeepSeekSearchAdapter
from .gateway import ModelGateway
from .openai_responses import OpenAIResponsesAdapter
from .types import ModelBinding, ModelRequest, ReadResult, SearchHit
from .web import AiohttpPageFetcher, ToolGateway

__all__ = [
    "AiohttpPageFetcher",
    "DeepSeekResponsesAdapter",
    "DeepSeekSearchAdapter",
    "ModelBinding",
    "ModelGateway",
    "ModelRequest",
    "OpenAIResponsesAdapter",
    "ReadResult",
    "SearchHit",
    "ToolGateway",
]
