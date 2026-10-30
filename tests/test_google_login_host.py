"""Login com Google comeca no mesmo endereco do callback (www x sem www tem cookies diferentes)."""
import os
import unittest
from unittest.mock import patch

_ENV = dict(os.environ)
import app  # noqa: E402  (o import carrega o .env; nao deixa vazar para os outros testes)
os.environ.clear()
os.environ.update(_ENV)


class GoogleLoginHostTests(unittest.TestCase):
    def setUp(self):
        for nome, valor in (("GOOGLE_CLIENT_ID", "cid"), ("GOOGLE_CLIENT_SECRET", "sec"),
                            ("GOOGLE_REDIRECT_URI", "https://drogariaspoupaqui.com.br/auth/google/callback")):
            self.enterContext(patch.object(app, nome, valor))
        self.client = app.app.test_client()

    def test_inicio_no_www_volta_para_o_endereco_do_callback(self):
        r = self.client.get("/auth/google?next=/carrinho", base_url="https://drogariaspoupaqui.com.br",
                            headers={"X-Forwarded-Host": "www.drogariaspoupaqui.com.br"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "https://drogariaspoupaqui.com.br/auth/google?next=/carrinho")

    def test_inicio_no_mesmo_endereco_vai_direto_ao_google(self):
        r = self.client.get("/auth/google?next=", base_url="https://drogariaspoupaqui.com.br",
                            headers={"X-Forwarded-Host": "drogariaspoupaqui.com.br"})
        self.assertTrue(r.headers["Location"].startswith("https://accounts.google.com/"))
        self.assertIn("state=", r.headers["Location"])
        self.assertIn("session=", r.headers.get("Set-Cookie", ""))   # state guardado no cookie deste endereco


if __name__ == "__main__":
    unittest.main()
