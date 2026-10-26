from django.contrib.staticfiles import finders
from django.test import TestCase

from payments import catalog


class PageTests(TestCase):
    def test_store_is_public_and_lists_the_catalog(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for p in catalog.PRODUCTS:
            self.assertIn(p.name, html)
            self.assertIn(p.brand, html)
            self.assertIn(f'data-price="{p.price_minor}"', html)

    def test_other_pages_render(self):
        for path in ("/checkout/", "/dashboard/", "/login/"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, catalog.STORE_NAME)

    def test_static_assets_exist(self):
        for path in ("payments/app.css", "payments/app.js"):
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))
