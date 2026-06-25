from django.db import models


class User(models.Model):
    email = models.EmailField(unique=True)

    class Meta:
        db_table = "users"

    def __str__(self):
        return self.email
