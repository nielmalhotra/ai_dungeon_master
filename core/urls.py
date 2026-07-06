from django.urls import path

from .views import home, quit_session


urlpatterns = [
    path("", home, name="home"),
    path("session/quit/", quit_session, name="quit_session"),
]
