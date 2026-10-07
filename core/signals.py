from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Leg, Shipment, YardStay


@receiver(post_save, sender=Leg)
@receiver(post_save, sender=YardStay)
def refresh_shipment_status(sender, instance, **kwargs):
    shipment = Shipment.all_objects.filter(pk=instance.shipment_id).first()
    if shipment:
        shipment.refresh_status()
