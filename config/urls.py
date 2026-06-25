from django.urls import include, path


urlpatterns = [
    path("accounts/", include("allauth.urls")),
    path("", include("core.urls")),
]
