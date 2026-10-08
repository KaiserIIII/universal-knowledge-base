"""Local synthetic UI integration; never connects to model providers."""
import sys
import os
import tempfile
import asyncio
import json
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
sys.path.insert(0,str(root/'backend'))
from app.main import create_runtime_app
from app.saas.config import AppSettings
from tests.support import FakeRetriever,FakeLLM
from sqlalchemy import select
from app.models import Document,DocumentChunk,DocStatus

class PreviewRetriever(FakeRetriever):
    async def search(self,**kwargs):
        self.search_calls.append(kwargs)
        if '未知产品' in kwargs['query']:
            return []
        async with app.state.session_factory() as db:
            rows=(await db.execute(select(DocumentChunk,Document).join(Document,Document.id==DocumentChunk.doc_id).where(Document.kb_id.in_(kwargs['kb_ids']),Document.status==DocStatus.COMPLETED))).all()
            return [{'chunk_id':chunk.id,'doc_id':doc.id,'content':chunk.content,'filename':doc.filename,'score':.9,'metadata':{'kb_id':doc.kb_id}} for chunk,doc in rows][:kwargs['top_k']]
    async def parse(self,**kwargs):
        from app.parsing import ParserOptions, decode_text, parse_tabular
        options=ParserOptions.model_validate(kwargs.get('parser_config') or {})
        path=Path(kwargs['file_path'])
        text=parse_tabular(path,path.suffix,options) if path.suffix in {'.csv','.tsv'} and options.mode!='text' else decode_text(path.read_bytes(),options.text_encoding)
        return [{'content':text,'metadata':{}}]
    async def upsert(self,chunks):
        self.results.extend(dict(row,score=.9) for row in chunks)
        return len(chunks)
    async def delete_document(self,doc_id):
        self.results=[row for row in self.results if row.get('doc_id')!=doc_id]
        return 0
    async def rerank(self,query,results,top_k):
        return results[:top_k]

class PreviewLLM(FakeLLM):
    async def complete(self,**kwargs):
        self.calls.append(kwargs)
        model=(kwargs.get('profile') or {}).get('model','synthetic-default')
        try:question=json.loads(kwargs['messages'][-1]['content']).get('question','')
        except (ValueError,TypeError):question=''
        if '取消' in question:await asyncio.sleep(5)
        else:await asyncio.sleep(.2)
        if model=='synthetic-failure':raise ConnectionError('Synthetic disconnected upstream')
        return {'content':f'【{model} · 合成适配器】标准保修期为 12 个月。申请维修需提供购买凭证。[1]','usage':{'total_tokens':24}}
    async def stream(self,**kwargs):
        self.calls.append(kwargs)
        question=json.loads(kwargs['messages'][-1]['content']).get('question','')
        for piece in ['合成政策规定：','标准保修期为 12 个月。','申请维修需提供购买凭证。','[1]']:
            if '取消' in question:await asyncio.sleep(5)
            else:await asyncio.sleep(.15)
            yield piece
            if '故障' in question:raise ConnectionError('Synthetic disconnected upstream')

temporary = tempfile.TemporaryDirectory(prefix='zhixu-browser-')
folder=Path(temporary.name)
port=int(os.environ.get('ZHIXU_BROWSER_PORT', '8788'))
folder.mkdir(parents=True,exist_ok=True)
settings=AppSettings(_env_file=None,_env_prefix='KNOWLEDGE_PREVIEW_ISOLATED_',database_url=f"sqlite+aiosqlite:///{(folder/'browser-test.db').as_posix()}",chroma_persist_dir=str(folder/'chroma'),upload_temp_dir=str(folder/'uploads'),cookie_secure=False,public_origin=f'http://127.0.0.1:{port}',model_allowed_hosts=['api.synthetic.example'],model_allowed_secret_refs=['PREVIEW_ABSENT_KEY'],use_reranker=True)
app=create_runtime_app(settings,retriever=PreviewRetriever(),llm=PreviewLLM())
@app.get('/__test__/fixture', include_in_schema=False)
async def fixture_marker():
    return {'fixture': 'zhixu-browser-synthetic'}
if __name__=='__main__':
    import uvicorn
    try:
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')
    finally:
        temporary.cleanup()
