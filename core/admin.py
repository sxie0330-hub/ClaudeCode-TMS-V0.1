from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Carrier, Customer, Location, MasterBL, Profile, Project, Shipment

for model in (Customer, Carrier, Location, Project, MasterBL, Shipment, Profile):
    admin.site.register(model, SimpleHistoryAdmin)
