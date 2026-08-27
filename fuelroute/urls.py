from django.urls import path

from fuelroute import views

app_name = "fuelroute"

urlpatterns = [
    path("route-plan/", views.route_plan, name="route-plan"),
    path("stations/", views.stations, name="stations"),
    path("health/", views.health, name="health"),
]
