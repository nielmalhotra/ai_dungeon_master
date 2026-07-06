from copy import deepcopy
from pathlib import Path

from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.shortcuts import redirect, render

from accounts.models import User as AppUser

from .forms import CreateGameForm
from .models import CharacterInstance, CharacterTemplate, DndSession

CURRENT_DOCUMENTATION_PATH = (
    Path(__file__).resolve().parent / "assets" / "CURRENT_DOCUMENTATION.txt"
)
ATTRIBUTE_ORDER = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)


def _character_options(character_templates, form):
    selected = set(form.data.getlist("selected_templates")) if form.is_bound else set()
    return [
        {
            "key": template.template_key,
            "character": template.character_template,
            "selected": template.template_key in selected,
            "name": form.data.get(f"name_{template.template_key}", ""),
            "name_errors": form.errors.get(f"name_{template.template_key}", []),
        }
        for template in character_templates
    ]


def _active_session_for(request):
    if not request.user.is_authenticated:
        return None
    return (
        DndSession.objects.filter(user__email=request.user.email, active=True)
        .prefetch_related("characters")
        .first()
    )


def _character_sheets(active_session):
    if active_session is None:
        return []

    sheets = []
    for instance in active_session.characters.all():
        attributes = instance.template_json["attributes"]
        sheets.append(
            {
                "instance": instance,
                "attributes": [
                    {
                        "label": attribute[:3].upper(),
                        "value": attributes[attribute],
                    }
                    for attribute in ATTRIBUTE_ORDER
                ],
            }
        )
    return sheets


def home(request):
    current_documentation = CURRENT_DOCUMENTATION_PATH.read_text()
    active_session = _active_session_for(request)
    character_templates = list(CharacterTemplate.objects.all())
    create_game_form = None

    if request.user.is_authenticated and active_session is None:
        create_game_form = CreateGameForm(
            request.POST or None,
            character_templates=character_templates,
        )

        if request.method == "POST" and create_game_form.is_valid():
            with transaction.atomic():
                app_user, _ = AppUser.objects.get_or_create(email=request.user.email)
                active_session = DndSession.objects.create(user=app_user, active=True)
                CharacterInstance.objects.bulk_create(
                    [
                        CharacterInstance(
                            dnd_session=active_session,
                            name=name,
                            template_json=deepcopy(template.character_template),
                        )
                        for template, name in create_game_form.cleaned_data[
                            "selected_characters"
                        ]
                    ]
                )
            return redirect("home")

    context = {
        "active_session": active_session,
        "character_sheets": _character_sheets(active_session),
        "character_options": (
            _character_options(character_templates, create_game_form)
            if create_game_form
            else []
        ),
        "create_game_form": create_game_form,
        "current_documentation": current_documentation,
    }
    return render(request, "core/home.html", context)


@login_required
@require_POST
def quit_session(request):
    DndSession.objects.filter(
        user__email=request.user.email,
        active=True,
    ).update(active=False)
    return redirect("home")
