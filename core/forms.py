from django import forms


class CreateGameForm(forms.Form):
    selected_templates = forms.MultipleChoiceField(
        choices=(),
        error_messages={"required": "Choose exactly three characters."},
    )

    def __init__(self, *args, character_templates, **kwargs):
        super().__init__(*args, **kwargs)
        self.character_templates = {
            template.template_key: template for template in character_templates
        }
        self.fields["selected_templates"].choices = [
            (template.template_key, template.character_template["class"])
            for template in character_templates
        ]

        for template_key, template in self.character_templates.items():
            character_class = template.character_template["class"]
            self.fields[f"name_{template_key}"] = forms.CharField(
                max_length=80,
                required=False,
                strip=True,
                label=f"{character_class} name",
            )

    def clean(self):
        cleaned_data = super().clean()
        selected_templates = cleaned_data.get("selected_templates", [])

        if len(selected_templates) != 3:
            self.add_error(
                "selected_templates",
                "Choose exactly three characters.",
            )

        selected_characters = []
        for template_key in selected_templates:
            name_field = f"name_{template_key}"
            name = cleaned_data.get(name_field, "")
            if not name:
                self.add_error(name_field, "Enter a name for this character.")
                continue
            selected_characters.append(
                (self.character_templates[template_key], name)
            )

        cleaned_data["selected_characters"] = selected_characters
        return cleaned_data
