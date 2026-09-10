# tpdb/validators.py
from schematics.models import Model
from schematics.types import URLType, StringType, ListType

class SceneItem(Model):
    id = StringType(required=True)
    image = URLType(required=True)
    site = StringType(required=True)
    title = StringType(required=True)
    url = URLType(required=True)
