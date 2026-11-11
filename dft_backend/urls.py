from django.contrib import admin
from django.urls import path

from jobs.api import api

admin.site.site_header = "DFT as Code"
admin.site.site_title = "DFT as Code"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
]
