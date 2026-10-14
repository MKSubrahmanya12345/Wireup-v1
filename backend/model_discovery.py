import asyncio
import hashlib
import os
import time

import httpx
from backend.models import model_options, NVIDIA_BASE_URL, AZURE_BASE_URL

_cache = {}
AZURE_ACCESSIBLE_MODELS = ('gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-astra', 'gpt-6-luna', 'gpt-6-sol', 'gpt-6.1-sol')


async def discover_models():
    providers = model_options()
    signature = hashlib.sha256(repr(sorted(os.environ.items())).encode()).hexdigest()
    cached = _cache.get(signature)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]

    async def discover(option):
        if not option['configured']:
            return [], None
        provider = option['id']
        try:
            if provider == 'azure':
                ids = list(AZURE_ACCESSIBLE_MODELS)
            elif provider == 'bedrock':
                def listing():
                    import boto3
                    from botocore.config import Config
                    if os.getenv('BEDROCK_AUTH_MODE', 'api_key') != 'iam':
                        raise ValueError('Model listing requires IAM control-plane access.')
                    client = boto3.client('bedrock', region_name=os.getenv('AWS_REGION', 'eu-north-1'), config=Config(connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 1}))
                    ids = [item['modelId'] for item in client.list_foundation_models(byOutputModality='TEXT')['modelSummaries'] if 'ON_DEMAND' in item.get('inferenceTypesSupported', [])]
                    paginator = client.get_paginator('list_inference_profiles')
                    ids += [item['inferenceProfileId'] for page in paginator.paginate() for item in page.get('inferenceProfileSummaries', [])]
                    return ids
                ids = await asyncio.wait_for(asyncio.to_thread(listing), 10)
            else:
                base = {'groq': 'https://api.groq.com/openai/v1', 'nvidia': NVIDIA_BASE_URL, 'azure': os.getenv('AZURE_BASE_URL', AZURE_BASE_URL)}[provider]
                key = os.getenv({'groq': 'GROQ_API_KEY', 'nvidia': 'NVIDIA_API_KEY', 'azure': 'AZURE_API_KEY'}[provider], '')
                headers = {'Authorization': f'Bearer {key}'}
                if provider == 'azure':
                    headers['api-key'] = key
                ids = []
                async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                    after = None
                    for _ in range(20):
                        response = await client.get(base.rstrip('/') + '/models', headers=headers, params={'after': after} if after else None)
                        response.raise_for_status()
                        data = response.json()
                        entries = data.get('data', [])
                        ids += [item['id'] for item in entries if isinstance(item.get('id'), str)]
                        if not data.get('has_more') or not entries:
                            break
                        next_after = entries[-1]['id']
                        if next_after == after:
                            break
                        after = next_after
            models = [{**option, 'model': identity} for identity in sorted(set(ids)) if identity.strip() and len(identity) <= 256]
            return models, None
        except Exception:
            return [], 'Model listing unavailable; the configured model remains available.'

    results = await asyncio.gather(*(discover(option) for option in providers))
    models = []
    errors = {}
    for option, (listed, error) in zip(providers, results):
        models.extend(listed)
        if not any(item['model'] == option['model'] for item in listed):
            models.append(option)
        if error:
            errors[option['id']] = error
    result = {'models': models, 'providers': providers, 'errors': errors}
    _cache.clear()
    _cache[signature] = (time.monotonic(), result)
    return result
