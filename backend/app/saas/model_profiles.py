"""Tenant model configuration and common profile-bound dispatch, without shared mutation."""
import asyncio
from contextlib import aclosing
import ipaddress
import os
import re
from time import monotonic
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from .dependencies import get_access, get_db, require_role, valid_id
from .llm import LLMNotConfigured, OpenAICompatibleLLM, UpstreamProtocolError
from .workflow_models import ModelProfile

router = APIRouter(prefix='/api/v1/model-profiles')


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    provider: str = 'openai-compatible'
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str | None = Field(default=None, max_length=100)
    temperature: float = Field(default=.2, ge=0, le=2, allow_inf_nan=False)
    top_p: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    timeout_seconds: float = Field(default=60, ge=.05, le=120, allow_inf_nan=False)
    system_prompt: str = Field(default='', max_length=8000)


def validate_profile(config, settings, *, resolve=False):
    # Deliberately do not return Pydantic input values in validation errors.
    try:
        value = ProfileConfig.model_validate(config).model_dump()
        url = urlsplit(value['base_url'])
        host = url.hostname
        if (value['provider'] != 'openai-compatible' or not host or url.username is not None or url.password is not None
                or url.query or url.fragment or '%' in url.netloc or '\\' in value['base_url']
                or any(ord(c) < 33 for c in value['base_url']) or host not in settings.model_allowed_hosts
                or url.scheme not in {'https', 'http'} or (url.scheme == 'http' and not settings.model_allow_http)):
            raise ValueError()
        _ = url.port
        private = host == 'localhost' or host.endswith(('.localhost', '.local'))
        try:
            private = private or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if private and host not in settings.model_private_hosts:
            raise ValueError()
        ref = value['api_key_env']
        if ref and (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', ref) or ref not in settings.model_allowed_secret_refs):
            raise ValueError()
        if resolve and ref and not os.environ.get(ref):
            raise LLMNotConfigured()
        value['base_url'] = value['base_url'].rstrip('/')
        return value
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(422, 'Unsupported or unauthorized model configuration') from None


async def scoped_profile(db, access, profile_id):
    row = await db.scalar(select(ModelProfile).where(ModelProfile.id == valid_id(profile_id),
        ModelProfile.workspace_id == access.workspace_id, ModelProfile.is_deleted.is_(False)))
    if row is None:
        raise HTTPException(404, 'Resource not found')
    return row


async def load_profile(db, access, profile_id, settings, *, resolve=False):
    row = await scoped_profile(db, access, profile_id)
    try:
        return {'id': row.id, **validate_profile(row.config, settings, resolve=resolve)}
    except LLMNotConfigured:
        raise HTTPException(422, 'Model credential reference is not configured') from None


def public_profile(row, settings):
    ref = row.config.get('api_key_env')
    return {'id': row.id, **row.config, 'configured': not ref or (ref in settings.model_allowed_secret_refs and bool(os.environ.get(ref))),
            'created_at': row.created_at}


class ModelDispatcher:
    def __init__(self, settings, llm):
        self.settings, self.llm = settings, llm

    def parameters(self, profile, overrides):
        if profile is None:
            return overrides
        config = validate_profile({k: v for k, v in profile.items() if k != 'id'}, self.settings, resolve=True)
        params = {key: config[key] for key in ('temperature', 'top_p', 'max_tokens', 'timeout_seconds')}
        params.update(overrides)
        params['profile_id'] = profile['id']
        params['profile'] = config
        return params

    async def complete(self, *, messages, profile=None, **overrides):
        params = self.parameters(profile, overrides)
        if profile and profile.get('system_prompt'):
            messages = [*messages[:1], {'role': 'system', 'content': profile['system_prompt']}, *messages[1:]]
        async with asyncio.timeout(params.get('timeout_seconds', self.settings.chat_timeout_seconds)):
            return await self.llm.complete(messages=messages, **params)

    async def stream(self, *, messages, profile=None, **overrides):
        params = self.parameters(profile, overrides)
        if profile and profile.get('system_prompt'):
            messages = [*messages[:1], {'role': 'system', 'content': profile['system_prompt']}, *messages[1:]]
        async with asyncio.timeout(params.get('timeout_seconds', self.settings.chat_timeout_seconds)):
            async with aclosing(self.llm.stream(messages=messages, **params)) as upstream:
                async for piece in upstream:
                    yield piece


@router.get('')
async def list_profiles(request: Request, access=Depends(get_access), db=Depends(get_db)):
    rows = (await db.scalars(select(ModelProfile).where(ModelProfile.workspace_id == access.workspace_id, ModelProfile.is_deleted.is_(False)))).all()
    return {'model_profiles': [public_profile(row, request.app.state.settings) for row in rows]}


@router.post('', status_code=201)
async def create_profile(body: dict, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin')
    config = validate_profile(body, request.app.state.settings)
    row = ModelProfile(workspace_id=access.workspace_id, config=config)
    db.add(row); await db.commit()
    return public_profile(row, request.app.state.settings)


@router.get('/{profile_id}')
async def get_profile(profile_id: str, request: Request, access=Depends(get_access), db=Depends(get_db)):
    return public_profile(await scoped_profile(db, access, profile_id), request.app.state.settings)


@router.patch('/{profile_id}')
async def update_profile(profile_id: str, body: dict, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin')
    row = await scoped_profile(db, access, profile_id)
    row.config = validate_profile({**row.config, **body}, request.app.state.settings)
    await db.commit()
    return public_profile(row, request.app.state.settings)


@router.delete('/{profile_id}')
async def delete_profile(profile_id: str, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin')
    row = await scoped_profile(db, access, profile_id)
    row.is_deleted = True; await db.commit()
    return {'ok': True}


@router.post('/{profile_id}/test')
async def test_profile(profile_id: str, request: Request, access=Depends(get_access), db=Depends(get_db)):
    require_role(access, 'owner', 'admin')
    profile = await load_profile(db, access, profile_id, request.app.state.settings)
    await db.rollback()
    started = monotonic()
    try:
        result = await request.app.state.model_dispatcher.complete(profile=profile,
            messages=[{'role': 'user', 'content': 'Reply OK.'}], max_tokens=8)
        if not isinstance(result, dict) or not isinstance(result.get('content'), str) or not result['content'].strip():
            raise UpstreamProtocolError()
        return {'ok': True, 'latency_ms': round((monotonic() - started) * 1000, 3)}
    except Exception as error:
        from .chat import error_category
        return {'ok': False, 'latency_ms': round((monotonic() - started) * 1000, 3), 'error': error_category(error)}
