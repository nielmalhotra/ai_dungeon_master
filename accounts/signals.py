from allauth.account.signals import user_signed_up
from allauth.socialaccount.signals import social_account_updated
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import User


def sync_app_user(email):
    if email:
        User.objects.get_or_create(email=email)


@receiver(user_signed_up)
def sync_signed_up_user(request, user, **kwargs):
    sync_app_user(user.email)


@receiver(social_account_updated)
def sync_existing_social_user(request, sociallogin, **kwargs):
    sync_app_user(sociallogin.user.email)


@receiver(user_logged_in)
def sync_logged_in_user(request, user, **kwargs):
    sync_app_user(user.email)
