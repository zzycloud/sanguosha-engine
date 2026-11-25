"""接大模型打牌：OpenAI 兼容接口的客户端、提示词、输出解析和 LLMAgent。说明见 docs/agents.md。"""
from .agent import JsonlSink, LLMAgent, TRANSCRIPT_FORMAT  # noqa: F401
from .client import CLIENTS, ChatClient, ChatError, ChatResult, OpenAIChatClient, make_client, register_client  # noqa: F401
from .parse import parse_action  # noqa: F401
from .prompt import PROMPTS, PromptBuilder, make_prompt, register_prompt  # noqa: F401
