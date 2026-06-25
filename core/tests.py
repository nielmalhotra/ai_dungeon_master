from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class HomeViewTests(TestCase):
    def test_home_prompts_anonymous_users_to_log_in(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Dungeon Master")
        self.assertContains(response, "Log in with Google")
        self.assertContains(response, "/accounts/google/login/")

    def test_home_shows_disabled_chat_for_authenticated_users(self):
        user = User.objects.create_user(
            username="player@example.com",
            email="player@example.com",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Adventure")
        self.assertContains(response, "textarea disabled")
        self.assertContains(response, "button")
        self.assertContains(response, "disabled")
