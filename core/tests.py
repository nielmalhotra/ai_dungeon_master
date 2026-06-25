from django.test import SimpleTestCase
from django.urls import reverse


class HomeViewTests(SimpleTestCase):
    def test_home_returns_hello_world(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Hello, world.")
