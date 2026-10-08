"""SaaS entrypoint: explicit deployment settings, lazy adapters and local UI."""
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .saas.application import create_app
from .saas.config import AppSettings


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = BACKEND_DIR.parent
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
logger = logging.getLogger('knowledge.runtime')


def create_runtime_app(settings=None, **adapters):
    # Only this checkout's explicitly named configuration may be loaded.
    settings = settings if settings is not None else AppSettings(_env_file=BACKEND_DIR / '.env')
    app = create_app(settings, **adapters)
    if settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                           allow_credentials=True,
                           allow_methods=['GET','POST','PUT','PATCH','DELETE'],
                           allow_headers=['Authorization','Content-Type','X-Workspace-ID','X-CSRF-Token'])

    @app.middleware('http')
    async def response_security(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as error:
            # Do not log upstream exception text: it may contain keys or content.
            logger.error('Unhandled %s; request_id=%s', type(error).__name__, request_id)
            response = JSONResponse({'detail':'Internal server error','request_id':request_id},status_code=500)
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = CSP
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        elif request.url.path.startswith('/web/'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

    app.mount('/web',StaticFiles(directory=REPOSITORY_DIR/'web'),name='web')

    @app.get('/',include_in_schema=False)
    async def home():
        return FileResponse(REPOSITORY_DIR/'index.html',headers={'Cache-Control':'no-cache'})

    @app.get('/docs',include_in_schema=False)
    async def api_reference():
        return FileResponse(REPOSITORY_DIR/'web'/'api-docs.html',headers={'Cache-Control':'no-cache'})

    return app


app = create_runtime_app()
