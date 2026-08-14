from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import redirect, render
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from accounts.models import User as AppUser

from .campaigns import ScenarioNotReadyError, abandon_campaign, create_campaign
from .forms import CreateGameForm
from .models import (
    CharacterTemplate,
    DndSession,
)

CURRENT_DOCUMENTATION_PATH = (
    Path(__file__).resolve().parent / "assets" / "CURRENT_DOCUMENTATION.txt"
)
FUTURE_COMBAT_START = "[[future-combat]]"
FUTURE_COMBAT_END = "[[/future-combat]]"
ATTRIBUTE_ORDER = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)


def _render_current_documentation():
    documentation = CURRENT_DOCUMENTATION_PATH.read_text()
    if documentation.count(FUTURE_COMBAT_START) != documentation.count(
        FUTURE_COMBAT_END
    ):
        raise ValueError("Future combat documentation markers are unbalanced.")

    escaped_documentation = escape(documentation)
    rendered_documentation = escaped_documentation.replace(
        FUTURE_COMBAT_START,
        '<s class="future-combat">',
    ).replace(FUTURE_COMBAT_END, "</s>")
    return mark_safe(rendered_documentation)


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


def _current_session_for(request):
    if not request.user.is_authenticated:
        return None
    sessions = DndSession.objects.filter(
        user__email=request.user.email,
        status__in=(
            DndSession.Status.ACTIVE,
            DndSession.Status.COMPLETED,
        ),
    ).prefetch_related("characters")
    return sessions.order_by(
        models.Case(
            models.When(status=DndSession.Status.ACTIVE, then=0),
            default=1,
        ),
        "-updated_at",
    ).first()


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
    current_documentation = _render_current_documentation()
    active_session = _current_session_for(request)
    character_templates = list(CharacterTemplate.objects.all())
    create_game_form = None

    if request.user.is_authenticated and active_session is None:
        create_game_form = CreateGameForm(
            request.POST or None,
            character_templates=character_templates,
        )

        if request.method == "POST" and create_game_form.is_valid():
            app_user, _ = AppUser.objects.get_or_create(email=request.user.email)
            try:
                create_campaign(
                    user=app_user,
                    selected_characters=create_game_form.cleaned_data[
                        "selected_characters"
                    ],
                    scenario_key=settings.SCENARIO_KEY,
                )
            except ScenarioNotReadyError:
                create_game_form.add_error(
                    None,
                    "The adventure has not been prepared yet.",
                )
            else:
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
    campaign = DndSession.objects.filter(
        user__email=request.user.email,
        status=DndSession.Status.ACTIVE,
    ).first()
    if campaign:
        abandon_campaign(campaign)
    return redirect("home")
