import os

from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI
from backend.bedrock_model import bedrock_configured, build_bedrock

NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com/v1'
AZURE_BASE_URL = 'https://project-t3-resource.services.ai.azure.com/openai/v1'
SARVAM_BASE_URL = 'https://api.sarvam.ai/v2'


def model_options():
    options = [
        {'id': 'groq', 'label': 'Groq', 'model': os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b'), 'configured': bool(os.getenv('GROQ_API_KEY', '').strip())},
        {'id': 'nvidia', 'label': 'NVIDIA', 'model': os.getenv('NVIDIA_MODEL', 'z-ai/glm-5.3'), 'configured': bool(os.getenv('NVIDIA_API_KEY', '').strip())},
        {'id': 'bedrock', 'label': 'Amazon Bedrock', 'model': os.getenv('BEDROCK_MODEL_ID', 'moonshotai.kimi-k2.5'), 'configured': bedrock_configured()},
        {'id': 'azure', 'label': 'Azure', 'model': os.getenv('AZURE_MODEL_ID', 'gpt-6.1-sol'), 'configured': bool(os.getenv('AZURE_API_KEY', '').strip())},
    ]
    if os.getenv('SARVAM_API_KEY', '').strip():
        options.append({'id': 'sarvam', 'label': 'Sarvam AI', 'model': os.getenv('SARVAM_MODEL', 'sarvam-105b'), 'configured': True})
    if os.getenv('OPENROUTER_API_KEY', '').strip():
        options.append({'id': 'openrouter', 'label': 'OpenRouter', 'model': os.getenv('OPENROUTER_MODEL', 'openai/gpt-oss-120b:free'), 'configured': True})
    return options


def _max_tokens(env_name):
    message = f'{env_name} must be an integer between 1 and 8192.'
    try:
        value = int(os.getenv(env_name, '4096'))
    except ValueError:
        raise ValueError(message) from None
    if not 1 <= value <= 8192:
        raise ValueError(message)
    return value


def build_model(provider, key, model_name):
    if provider == 'azure':
        return ChatOpenAI(model=model_name, api_key=key,
                          base_url=os.getenv('AZURE_BASE_URL', AZURE_BASE_URL).rstrip('/'),
                          default_headers={'api-key': key}, streaming=True,
                          max_retries=0, timeout=30, max_completion_tokens=4096,
                          stream_usage=False)
    if provider == 'bedrock':
        return build_bedrock(model_name)
    if provider == 'nvidia':
        return ChatOpenAI(model=model_name, api_key=key, base_url=NVIDIA_BASE_URL,
                          temperature=0.5, max_retries=0, timeout=20, streaming=True,
                          max_tokens=_max_tokens('NVIDIA_MAX_TOKENS'),
                          stream_usage=False)
    if provider == 'sarvam':
        return ChatOpenAI(model=model_name, api_key=key,
                          base_url=os.getenv('SARVAM_BASE_URL', SARVAM_BASE_URL).rstrip('/'),
                          temperature=0.3, max_retries=0, timeout=60, streaming=True,
                          max_tokens=_max_tokens('SARVAM_MAX_TOKENS'),
                          default_headers={'api-subscription-key': key},
                          stream_usage=False)
    if provider == 'openrouter':
        return ChatOpenAI(model=model_name, api_key=key,
                          base_url=os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/'),
                          temperature=0.5, max_retries=0, timeout=60, streaming=True,
                          max_tokens=_max_tokens('OPENROUTER_MAX_TOKENS'),
                          default_headers={'HTTP-Referer': 'https://wireup.space', 'X-Title': 'WireUp'},
                          stream_usage=False)
    options = {}
    if model_name in ('openai/gpt-oss-120b', 'openai/gpt-oss-20b'):
        options = {'model_kwargs': {'include_reasoning': True}}
    elif model_name.startswith('qwen/'):
        options = {'reasoning_format': 'parsed'}
    return init_chat_model(model_name, model_provider='groq', api_key=key,
                           temperature=0.5, max_retries=0, timeout=20,
                           max_tokens=_max_tokens('GROQ_MAX_TOKENS'), **options)
