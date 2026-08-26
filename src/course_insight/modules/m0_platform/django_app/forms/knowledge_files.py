"""Small forms for the simplified teacher knowledge-file workflow."""

from django import forms


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        values = data if isinstance(data, (list, tuple)) else [data]
        return [super().clean(value, initial) for value in values if value]


class KnowledgeUploadForm(forms.Form):
    source_type = forms.ChoiceField(
        choices=(
            ("knowledge", "知识文件（md/txt/pdf/ppt/pptx/docx）"),
            ("question", "题目文件（固定格式 txt）"),
        ),
        label="文件用途",
    )
    files = MultipleFileField(
        label="选择一个或多个文件",
        widget=MultipleFileInput(attrs={"multiple": True}),
    )


class KnowledgeConfirmForm(forms.Form):
    confirm = forms.BooleanField(label="确认按当前文件列表重新解析并发布")


class QuestionTextEditForm(forms.Form):
    text = forms.CharField(
        label="题目 TXT 内容",
        max_length=2_000_000,
        widget=forms.Textarea(attrs={"rows": 24}),
    )
