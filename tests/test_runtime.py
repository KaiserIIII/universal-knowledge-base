"""Deployable entrypoint serves only public UI assets and authenticated APIs."""
import httpx
from tests.support import ApiTestCase
from app.main import create_runtime_app


class RuntimeTests(ApiTestCase):
    async def test_public_ui_and_authenticated_resources_have_security_headers(self):
        app = create_runtime_app(self.settings, retriever=self.retriever,
                                 llm=self.llm, payment=self.payment)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
                home=await client.get('/')
                self.assertEqual(home.status_code,200)
                self.assertIn('知序',home.text)
                self.assertIn("script-src 'self'",home.headers['Content-Security-Policy'])
                self.assertEqual(home.headers['X-Content-Type-Options'],'nosniff')
                script=await client.get('/web/app.js')
                self.assertEqual(script.status_code,200)
                self.assertEqual(script.headers.get('Cache-Control'),'no-cache')
                docs=await client.get('/docs')
                self.assertEqual(docs.status_code,200)
                self.assertIn('/web/api-docs.js',docs.text)
                self.assertNotIn('cdn.jsdelivr.net',docs.text)
                self.assertEqual((await client.get('/api/v1/organizations')).status_code,401)
                self.assertEqual((await client.get('/web/../backend/.env')).status_code,404)
                self.assertEqual((await client.get('/web/%2e%2e/backend/.env')).status_code,404)
