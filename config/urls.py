from django.urls import include, path

from fuelroute import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/v1/", include("fuelroute.urls")),
    path("map/", views.map_view, name="map"),
]
