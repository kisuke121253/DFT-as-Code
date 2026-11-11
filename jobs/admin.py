from django.contrib import admin
from django.utils.html import format_html

from .models import Artifact, CoverageResult, Job, JobStep, Project, Version


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "repo_url", "default_branch", "created_at")
    search_fields = ("name", "repo_url")


@admin.register(Version)
class VersionAdmin(admin.ModelAdmin):
    list_display = ("project", "short_sha", "branch", "message_first_line", "created_at")
    list_filter = ("project", "branch")
    search_fields = ("commit_sha", "message")

    @admin.display(description="commit")
    def short_sha(self, obj):
        return obj.commit_sha[:10] or f"(upload {obj.input_sha256[:8]})"

    @admin.display(description="mensagem")
    def message_first_line(self, obj):
        return obj.message.splitlines()[0][:80] if obj.message else ""


class StepInline(admin.TabularInline):
    model = JobStep
    extra = 0
    fields = ("order", "name", "status", "duration_s", "log_path")
    readonly_fields = fields
    can_delete = False


class CoverageInline(admin.TabularInline):
    model = CoverageResult
    extra = 0
    fields = ("generator", "primary", "coverage_pct", "detected", "total_faults", "vectors_final", "duration_s")
    readonly_fields = fields
    can_delete = False


class ArtifactInline(admin.TabularInline):
    model = Artifact
    extra = 0
    fields = ("name", "kind", "size", "download")
    readonly_fields = fields
    can_delete = False

    @admin.display(description="baixar")
    def download(self, obj):
        return format_html('<a href="/api/v1/jobs/{}/artifacts/{}">baixar</a>', obj.job_id, obj.name)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "commit", "branch", "status_badge", "coverage_pct", "vector_count",
                    "area_um2", "duration", "created_at")
    list_filter = ("status", "project", "source")
    search_fields = ("id", "version__commit_sha")
    readonly_fields = [f.name for f in Job._meta.fields]
    inlines = [StepInline, CoverageInline, ArtifactInline]

    @admin.display(description="commit")
    def commit(self, obj):
        return obj.version.commit_sha[:10]

    @admin.display(description="branch")
    def branch(self, obj):
        return obj.version.branch

    @admin.display(description="duração (s)")
    def duration(self, obj):
        return round(obj.run_seconds, 1) if obj.run_seconds else None

    @admin.display(description="status")
    def status_badge(self, obj):
        color = {"passed": "#1a7f37", "failed": "#cf222e", "error": "#9a6700", "running": "#0969da"}.get(obj.status, "#57606a")
        return format_html('<b style="color:{}">{}</b>', color, obj.get_status_display())
