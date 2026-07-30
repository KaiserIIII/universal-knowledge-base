"""
PyInstaller 打包配置 — 零依赖嵌入式版本
将 FastAPI 后端打包为独立 exe，包含 BGE 模型

用法:
    pip install pyinstaller --break-system-packages
    cd backend
    pyinstaller build.spec

产物:
    dist/GenericKnowledgeBase.exe  (约 2-3GB, 包含模型文件)
"""
import os
import site

# -*- mode: python ; coding: utf-8 -*-

# 收集 sentence-transformers 模型路径
def _collect_model_data():
    """定位 BGE 模型的下载缓存路径"""
    paths = []
    # HuggingFace 缓存目录
    hf_home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    hub_dir = os.path.join(hf_home, "hub")
    if os.path.isdir(hub_dir):
        # 收集 models--BAAI--bge-small-zh-v1.5 等目录
        for name in os.listdir(hub_dir):
            if "bge" in name.lower() or "BAAI" in name:
                full = os.path.join(hub_dir, name)
                if os.path.isdir(full):
                    paths.append((full, os.path.join("huggingface_hub", name)))
    # sentence-transformers 安装目录
    try:
        import sentence_transformers
        st_dir = os.path.dirname(sentence_transformers.__file__)
        paths.append((st_dir, "sentence_transformers"))
    except ImportError:
        return paths
    return paths

model_datas = _collect_model_data()

a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('../.env.template', '.'),
        ('app/routers', 'app/routers'),
        ('app/__init__.py', 'app'),
        ('app/config.py', 'app'),
        ('app/database.py', 'app'),
        ('app/exceptions.py', 'app'),
        ('app/models.py', 'app'),
        ('app/schemas.py', 'app'),
        ('app/rag_engine.py', 'app'),
    ] + model_datas,
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'sqlalchemy',
        'sqlalchemy.ext.asyncio',
        'aiosqlite',
        'pydantic',
        'pydantic_settings',
        'httpx',
        'aiofiles',
        'multipart',
        'langchain_text_splitters',
        'chromadb',
        'chromadb.telemetry',
        'chromadb.telemetry.product',
        'sentence_transformers',
        'sentence_transformers.models',
        'jieba',
        'pypdf',
        'docx',
        'bs4',
        'starlette',
        'fastapi',
        'anyio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter','unittest','email','http.server',
        'torch.testing','torch.distributed','torch.cuda',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GenericKnowledgeBase',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
